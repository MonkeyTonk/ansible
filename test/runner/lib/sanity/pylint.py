"""Sanity test using pylint."""
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import itertools
import json
import os
import datetime

import lib.types as t

from lib.sanity import (
    SanitySingleVersion,
    SanityMessage,
    SanityFailure,
    SanitySuccess,
    SanitySkipped,
)

from lib.util import (
    SubprocessError,
    display,
    ConfigParser,
    ANSIBLE_ROOT,
    is_subdir,
)

from lib.util_common import (
    run_command,
)

from lib.ansible_util import (
    ansible_environment,
)

from lib.config import (
    SanityConfig,
)

from lib.data import (
    data_context,
)


UNSUPPORTED_PYTHON_VERSIONS = (
    '2.6',
    '2.7',
)


class PylintTest(SanitySingleVersion):
    """Sanity test using pylint."""
    def error_code(self):  # type: () -> t.Optional[str]
        """Error code for ansible-test matching the format used by the underlying test program, or None if the program does not use error codes."""
        return 'ansible-test'

    def test(self, args, targets):
        """
        :type args: SanityConfig
        :type targets: SanityTargets
        :rtype: TestResult
        """
        if args.python_version in UNSUPPORTED_PYTHON_VERSIONS:
            display.warning('Skipping pylint on unsupported Python version %s.' % args.python_version)
            return SanitySkipped(self.name)

        plugin_dir = os.path.join(ANSIBLE_ROOT, 'test/sanity/pylint/plugins')
        plugin_names = sorted(p[0] for p in [
            os.path.splitext(p) for p in os.listdir(plugin_dir)] if p[1] == '.py' and p[0] != '__init__')

        settings = self.load_processor(args)

        paths = sorted(i.path for i in targets.include if os.path.splitext(i.path)[1] == '.py' or is_subdir(i.path, 'bin/'))
        paths = settings.filter_skipped_paths(paths)

        module_paths = [os.path.relpath(p, data_context().content.module_path).split(os.path.sep) for p in
                        paths if is_subdir(p, data_context().content.module_path)]
        module_dirs = sorted(set([p[0] for p in module_paths if len(p) > 1]))

        large_module_group_threshold = 500
        large_module_groups = [key for key, value in
                               itertools.groupby(module_paths, lambda p: p[0] if len(p) > 1 else '') if len(list(value)) > large_module_group_threshold]

        large_module_group_paths = [os.path.relpath(p, data_context().content.module_path).split(os.path.sep) for p in paths
                                    if any(is_subdir(p, os.path.join(data_context().content.module_path, g)) for g in large_module_groups)]
        large_module_group_dirs = sorted(set([os.path.sep.join(p[:2]) for p in large_module_group_paths if len(p) > 2]))

        contexts = []
        remaining_paths = set(paths)

        def add_context(available_paths, context_name, context_filter):
            """
            :type available_paths: set[str]
            :type context_name: str
            :type context_filter: (str) -> bool
            """
            filtered_paths = set(p for p in available_paths if context_filter(p))
            contexts.append((context_name, sorted(filtered_paths)))
            available_paths -= filtered_paths

        def filter_path(path_filter=None):
            """
            :type path_filter: str
            :rtype: (str) -> bool
            """
            def context_filter(path_to_filter):
                """
                :type path_to_filter: str
                :rtype: bool
                """
                return is_subdir(path_to_filter, path_filter)

            return context_filter

        for large_module_dir in large_module_group_dirs:
            add_context(remaining_paths, 'modules/%s' % large_module_dir, filter_path(os.path.join(data_context().content.module_path, large_module_dir)))

        for module_dir in module_dirs:
            add_context(remaining_paths, 'modules/%s' % module_dir, filter_path(os.path.join(data_context().content.module_path, module_dir)))

        add_context(remaining_paths, 'modules', filter_path(data_context().content.module_path))
        add_context(remaining_paths, 'module_utils', filter_path(data_context().content.module_utils_path))

        add_context(remaining_paths, 'units', filter_path(data_context().content.unit_path))

        if data_context().content.collection:
            add_context(remaining_paths, 'collection', lambda p: True)
        else:
            add_context(remaining_paths, 'validate-modules', filter_path('test/sanity/validate-modules/'))
            add_context(remaining_paths, 'sanity', filter_path('test/sanity/'))
            add_context(remaining_paths, 'ansible-test', filter_path('test/runner/'))
            add_context(remaining_paths, 'test', filter_path('test/'))
            add_context(remaining_paths, 'hacking', filter_path('hacking/'))
            add_context(remaining_paths, 'ansible', lambda p: True)

        messages = []
        context_times = []

        test_start = datetime.datetime.utcnow()

        for context, context_paths in sorted(contexts):
            if not context_paths:
                continue

            context_start = datetime.datetime.utcnow()
            messages += self.pylint(args, context, context_paths, plugin_dir, plugin_names)
            context_end = datetime.datetime.utcnow()

            context_times.append('%s: %d (%s)' % (context, len(context_paths), context_end - context_start))

        test_end = datetime.datetime.utcnow()

        for context_time in context_times:
            display.info(context_time, verbosity=4)

        display.info('total: %d (%s)' % (len(paths), test_end - test_start), verbosity=4)

        errors = [SanityMessage(
            message=m['message'].replace('\n', ' '),
            path=m['path'],
            line=int(m['line']),
            column=int(m['column']),
            level=m['type'],
            code=m['symbol'],
        ) for m in messages]

        if args.explain:
            return SanitySuccess(self.name)

        errors = settings.process_errors(errors, paths)

        if errors:
            return SanityFailure(self.name, messages=errors)

        return SanitySuccess(self.name)

    @staticmethod
    def pylint(args, context, paths, plugin_dir, plugin_names):  # type: (SanityConfig, str, t.List[str], str, t.List[str]) -> t.List[t.Dict[str, str]]
        """Run pylint using the config specified by the context on the specified paths."""
        rcfile = os.path.join(ANSIBLE_ROOT, 'test/sanity/pylint/config/%s' % context.split('/')[0])

        if not os.path.exists(rcfile):
            if data_context().content.collection:
                rcfile = os.path.join(ANSIBLE_ROOT, 'test/sanity/pylint/config/collection')
            else:
                rcfile = os.path.join(ANSIBLE_ROOT, 'test/sanity/pylint/config/default')

        parser = ConfigParser()
        parser.read(rcfile)

        if parser.has_section('ansible-test'):
            config = dict(parser.items('ansible-test'))
        else:
            config = dict()

        disable_plugins = set(i.strip() for i in config.get('disable-plugins', '').split(',') if i)
        load_plugins = set(plugin_names) - disable_plugins

        cmd = [
            args.python_executable,
            '-m', 'pylint',
            '--jobs', '0',
            '--reports', 'n',
            '--max-line-length', '160',
            '--rcfile', rcfile,
            '--output-format', 'json',
            '--load-plugins', ','.join(load_plugins),
        ] + paths

        append_python_path = [plugin_dir]

        if data_context().content.collection:
            append_python_path.append(data_context().content.collection.root)

        env = ansible_environment(args)
        env['PYTHONPATH'] += os.path.pathsep + os.path.pathsep.join(append_python_path)

        if paths:
            display.info('Checking %d file(s) in context "%s" with config: %s' % (len(paths), context, rcfile), verbosity=1)

            try:
                stdout, stderr = run_command(args, cmd, env=env, capture=True)
                status = 0
            except SubprocessError as ex:
                stdout = ex.stdout
                stderr = ex.stderr
                status = ex.status

            if stderr or status >= 32:
                raise SubprocessError(cmd=cmd, status=status, stderr=stderr, stdout=stdout)
        else:
            stdout = None

        if not args.explain and stdout:
            messages = json.loads(stdout)
        else:
            messages = []

        return messages
