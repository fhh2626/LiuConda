import base64
import json
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from unit_test.test_support import load_liu_conda, load_module
from unit_test.test_liu_conda import FakeCommandView, FakeWindow


PREFIX = r'E:\中文 环境\science'
BACKEND = r'E:\micromamba\condabin\micromamba.bat'
SCRIPT = r'E:\课程 空格\中文脚本.py'
ARGUMENTS = ['中文', 'two words', "single'quote", 'double"quote', '&|<>^%', '', 'trailing\\']
ACTIVATION = b'$Env:CONDA_PREFIX = "test"\n'


class WindowsCommandRoutingTests(unittest.TestCase):
    def setUp(self):
        self.module = load_liu_conda()
        settings = self.module._settings()
        settings.set('backend', 'micromamba')
        settings.set('selected_env', PREFIX)
        self.stack = __import__('contextlib').ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(self.module.os, 'name', 'nt'))
        self.stack.enter_context(mock.patch.object(self.module, '_backend_executable', return_value=BACKEND))
        self.stack.enter_context(mock.patch.object(self.module, '_env_path', return_value=PREFIX))
        self.stack.enter_context(mock.patch.object(self.module.os.path, 'isfile', return_value=True))
        self.stack.enter_context(mock.patch.object(self.module, '_startupinfo', return_value=None))

    def expected(self, command='python'):
        return [os.path.join(PREFIX, 'python.exe'), '-I', '-u', self.module.WINDOWS_LAUNCHER_FILE,
                BACKEND, PREFIX, '--', command]

    def test_windows_micromamba_uses_launcher_not_cmd_or_run(self):
        self.assertEqual(self.module._environment_command('python'), self.expected())

    def test_python_probe_preserves_unicode_and_shell_characters(self):
        with mock.patch.object(self.module, '_run_process', return_value='ok') as run:
            self.assertEqual(self.module._run_python(['-c', 'print("中文")'] + ARGUMENTS), 'ok')
        self.assertEqual(run.call_args.args[0], self.expected() + ['-c', 'print("中文")'] + ARGUMENTS)

    def test_sublime_exec_still_receives_full_path_and_cwd(self):
        window = FakeWindow(FakeCommandView(file_name=SCRIPT))
        self.module.LiuCondaRunCommand(window).run()
        name, args = window.commands[-1]
        self.assertEqual(name, 'exec')
        self.assertEqual(args['cmd'], self.expected() + [SCRIPT])
        self.assertEqual(args['working_dir'], os.path.dirname(SCRIPT))

    def test_jedi_worker_uses_same_launcher_with_piped_stdio(self):
        with mock.patch.object(self.module.subprocess, 'Popen') as popen, mock.patch.object(
            self.module.threading, 'Thread'
        ), mock.patch.object(self.module, '_register_process_job'):
            self.module._JediWorker(PREFIX).start()
        self.assertEqual(popen.call_args.args[0][:8], self.expected())
        self.assertIn(self.module.JEDI_WORKER_FILE, popen.call_args.args[0])
        self.assertEqual(popen.call_args.kwargs['stdin'], subprocess.PIPE)
        self.assertEqual(popen.call_args.kwargs['encoding'], 'utf-8')

    def test_zuban_worker_uses_same_launcher_with_binary_stdio(self):
        with mock.patch.object(self.module.subprocess, 'Popen') as popen, mock.patch.object(
            self.module.threading, 'Thread'
        ), mock.patch.object(self.module, '_register_process_job'), mock.patch.object(
            self.module._ZubanWorker, '_initialize'
        ), mock.patch.object(self.module, '_zuban_target_command', return_value=r'E:\中文 环境\zuban.exe'):
            self.module._ZubanWorker(PREFIX).start()
        self.assertEqual(popen.call_args.args[0], self.expected(r'E:\中文 环境\zuban.exe') + ['server'])
        self.assertNotIn('text', popen.call_args.kwargs)

    def test_missing_environment_path_fails_instead_of_unsafe_run_fallback(self):
        with mock.patch.object(self.module, '_env_path', return_value=''):
            with self.assertRaisesRegex(FileNotFoundError, 'refresh environments'):
                self.module._python_command()

    def test_missing_python_is_actionable(self):
        with mock.patch.object(self.module.os.path, 'isfile', return_value=False):
            with self.assertRaisesRegex(FileNotFoundError, 'no python.exe'):
                self.module._python_command()

    def test_windows_conda_is_unchanged(self):
        self.module._settings().set('backend', 'conda')
        self.assertEqual(self.module._python_command(), [BACKEND, 'run', '-p', PREFIX, 'python'])

    def test_non_windows_micromamba_is_unchanged(self):
        with mock.patch.object(self.module.os, 'name', 'posix'):
            self.assertEqual(self.module._python_command(), [BACKEND, 'run', '-p', PREFIX, 'python'])

    def test_timeout_kills_descendant_tree(self):
        process = mock.Mock()
        process.communicate.side_effect = [subprocess.TimeoutExpired('test', 1), (b'', None)]
        with mock.patch.object(self.module.subprocess, 'Popen', return_value=process), mock.patch.object(
            self.module, '_kill_process_tree'
        ) as kill:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.module._run_process(self.expected(), timeout=1)
        kill.assert_called_once_with(process)


class WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module('liu_conda_launcher_under_test', 'LiuCondaWindowsLauncher.py')

    @unittest.skipUnless(os.name == 'nt', 'Windows path layout')
    def test_standard_batch_wrapper_resolves_native_executable(self):
        native = r'E:\micromamba\micromamba.exe'
        with mock.patch.object(self.module.os.path, 'isfile', side_effect=lambda p: p == native):
            self.assertEqual(self.module.native_micromamba(BACKEND), native)

    def test_native_executable_is_preserved(self):
        with mock.patch.object(self.module.os.path, 'isfile', return_value=True):
            self.assertEqual(self.module.native_micromamba(r'E:\中文\micromamba.exe'), r'E:\中文\micromamba.exe')

    def test_target_native_argv_is_not_quoted_or_interpolated(self):
        with mock.patch.object(self.module.shutil, 'which', return_value=r'E:\中文 环境\python.exe'):
            value = self.module.resolved_command(['python', SCRIPT] + ARGUMENTS, {'PATH': 'activated'})
        self.assertEqual(value, [r'E:\中文 环境\python.exe', SCRIPT] + ARGUMENTS)

    def test_activated_path_does_not_use_implicit_project_executables(self):
        with mock.patch.object(self.module.shutil, 'which', return_value=os.path.join(PREFIX, 'python.exe')) as which:
            self.module.resolved_command(['python', SCRIPT], {'PATH': PREFIX})
        which.assert_called_once_with(os.path.join(PREFIX, 'python'))

    def test_batch_target_is_rejected_instead_of_silently_using_cmd(self):
        with mock.patch.object(self.module.shutil, 'which', return_value='custom.bat'):
            with self.assertRaisesRegex(RuntimeError, 'native .exe'):
                self.module.resolved_command(['custom'], {'PATH': 'activated'})

    def test_activation_uses_backend_ps_hooks_and_memory_only_json(self):
        source = ACTIVATION
        activated = {'CONDA_PREFIX': PREFIX, 'Path': 'activated path', 'PACKAGE_HOOK': '中文'}
        calls = [SimpleNamespace(returncode=0, stdout=source, stderr=b''),
                 SimpleNamespace(returncode=0, stdout=json.dumps(activated).encode('utf-8'), stderr=b'')]
        with mock.patch.object(self.module, 'native_micromamba', return_value='native.exe'), mock.patch.object(
            self.module, 'powershell_executable', return_value='powershell.exe'
        ), mock.patch.object(self.module.subprocess, 'run', side_effect=calls) as run:
            environment = self.module.activated_environment(BACKEND, PREFIX)
        first, second = run.call_args_list
        self.assertEqual(first.args[0], ['native.exe', 'shell', 'activate', '--shell', 'powershell', '--prefix', PREFIX])
        self.assertEqual(json.loads(second.kwargs['input']),
                         [{'kind': 'set', 'name': 'CONDA_PREFIX', 'value': 'test'}])
        script = base64.b64decode(second.args[0][-1]).decode('utf-16-le')
        self.assertNotIn(PREFIX, script)
        self.assertNotIn('Invoke-Expression', script)
        self.assertIn('SetEnvironmentVariable', script)
        self.assertEqual(environment['PATH'], 'activated path')
        self.assertEqual(environment['PACKAGE_HOOK'], '中文')
        self.assertEqual(environment['PYTHONIOENCODING'], 'utf-8')
        self.assertNotIn('shell', first.kwargs)
        self.assertNotIn('shell', second.kwargs)

    def test_invalid_environment_never_leaks_snapshot_in_error(self):
        calls = [SimpleNamespace(returncode=0, stdout=ACTIVATION, stderr=b''),
                 SimpleNamespace(returncode=0, stdout=b'SECRET_TOKEN=do-not-display', stderr=b'')]
        with mock.patch.object(self.module, 'native_micromamba', return_value='native.exe'), mock.patch.object(
            self.module, 'powershell_executable', return_value='powershell.exe'
        ), mock.patch.object(self.module.subprocess, 'run', side_effect=calls):
            with self.assertRaises(RuntimeError) as caught:
                self.module.activated_environment(BACKEND, PREFIX)
        self.assertNotIn('SECRET_TOKEN', str(caught.exception))
        self.assertNotIn('do-not-display', str(caught.exception))

    def test_wrong_activated_prefix_is_not_silently_used(self):
        calls = [SimpleNamespace(returncode=0, stdout=ACTIVATION, stderr=b''),
                 SimpleNamespace(returncode=0, stdout=b'{"CONDA_PREFIX":"wrong"}', stderr=b'')]
        with mock.patch.object(self.module, 'native_micromamba', return_value='native.exe'), mock.patch.object(
            self.module, 'powershell_executable', return_value='powershell.exe'
        ), mock.patch.object(self.module.subprocess, 'run', side_effect=calls):
            with self.assertRaisesRegex(RuntimeError, 'requested environment'):
                self.module.activated_environment(BACKEND, PREFIX)

    def test_powershell_failure_does_not_leak_activation_secrets(self):
        calls = [SimpleNamespace(returncode=0, stdout=ACTIVATION, stderr=b''),
                 SimpleNamespace(returncode=1, stdout=b'SECRET=token', stderr=b'error at $env:SECRET=token')]
        with mock.patch.object(self.module, 'native_micromamba', return_value='native.exe'), mock.patch.object(
            self.module, 'powershell_executable', return_value='powershell.exe'
        ), mock.patch.object(self.module.subprocess, 'run', side_effect=calls):
            with self.assertRaises(RuntimeError) as caught:
                self.module.activated_environment(BACKEND, PREFIX)
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertNotIn('token', str(caught.exception))

    def test_canonicalized_junction_prefix_is_accepted(self):
        calls = [SimpleNamespace(returncode=0, stdout=ACTIVATION, stderr=b''),
                 SimpleNamespace(returncode=0, stdout=b'{"CONDA_PREFIX":"canonical"}', stderr=b'')]
        with mock.patch.object(self.module, 'native_micromamba', return_value='native.exe'), mock.patch.object(
            self.module, 'powershell_executable', return_value='powershell.exe'
        ), mock.patch.object(self.module.subprocess, 'run', side_effect=calls), mock.patch.object(
            self.module.os.path, 'realpath', return_value='canonical'
        ):
            environment = self.module.activated_environment(BACKEND, PREFIX)
        self.assertEqual(environment['CONDA_PREFIX'], 'canonical')

    def test_activation_values_preserve_dollars_backticks_quotes_and_unicode(self):
        value = 'E:\\中文 & $(Write-Output NEVER_EXECUTE);`n;$env:PATH;double"quote'
        source = ('$Env:PATH = "' + value + '"\n').encode('utf-8')
        self.assertEqual(self.module.activation_actions(source, []),
                         [{'kind': 'set', 'name': 'PATH', 'value': value}])

    def test_unrecognized_activation_source_fails_without_leaking_values(self):
        source = b'$Env:TOKEN = "secret-value"\nWrite-Output secret-value\n'
        with self.assertRaises(RuntimeError) as caught:
            self.module.activation_actions(source, [])
        self.assertNotIn('secret-value', str(caught.exception))

    def test_hooks_are_literal_paths_from_installed_activation_directories(self):
        directory = os.path.join(PREFIX, 'etc', 'conda', 'activate.d')
        path = os.path.join(directory, '中文 & $(literal)`hook.ps1')
        with mock.patch.object(self.module.os.path, 'isdir', side_effect=lambda p: p == directory), mock.patch.object(
            self.module.os, 'listdir', return_value=[os.path.basename(path)]
        ), mock.patch.object(self.module.os.path, 'isfile', return_value=True):
            actions = self.module.activation_actions(('. ' + path + '\n').encode('utf-8'), [PREFIX])
        self.assertEqual(actions, [{'kind': 'hook', 'path': path}])

    def test_unknown_hook_cannot_turn_environment_text_into_executable_code(self):
        with self.assertRaisesRegex(RuntimeError, 'No generated code was executed'):
            self.module.activation_actions(b'. C:\\untrusted.ps1\n', [])

    def test_generation_errors_do_not_echo_backend_environment_values(self):
        generated = SimpleNamespace(returncode=1, stdout=b'', stderr=b'SECRET_TOKEN=do-not-display')
        with mock.patch.object(self.module, 'native_micromamba', return_value='native.exe'), mock.patch.object(
            self.module.subprocess, 'run', return_value=generated
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.module.activated_environment(BACKEND, PREFIX)
        self.assertNotIn('SECRET_TOKEN', str(caught.exception))
        self.assertNotIn('do-not-display', str(caught.exception))

    def test_target_exit_code_and_inherited_binary_stdio_are_preserved(self):
        environment = {'PATH': 'activated'}
        target = ['python.exe', SCRIPT] + ARGUMENTS
        with mock.patch.object(self.module, 'activated_environment', return_value=environment), mock.patch.object(
            self.module, 'resolved_command', return_value=target
        ), mock.patch.object(self.module.subprocess, 'call', return_value=37) as call:
            self.assertEqual(self.module.main([BACKEND, PREFIX, '--', 'python', SCRIPT] + ARGUMENTS), 37)
        call.assert_called_once_with(target, env=environment)


if __name__ == '__main__':
    unittest.main()
