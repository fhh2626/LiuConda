import os
import unittest
from types import SimpleNamespace
from unittest import mock

from unit_test.test_support import load_liu_conda


class ImmediateThread:
    def __init__(self, target=None, daemon=None):
        self.target = target
        self.daemon = daemon

    def start(self):
        if self.target:
            self.target()


class FakeWorker:
    def __init__(self):
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1


class FakeView:
    def __init__(self, text, buffer_id=1, change_count=1):
        self.text = text
        self._buffer_id = buffer_id
        self._change_count = change_count

    def buffer_id(self):
        return self._buffer_id

    def change_count(self):
        return self._change_count

    def size(self):
        return len(self.text)

    def substr(self, region):
        return self.text[region.a:region.b]

    def score_selector(self, point, selector):
        return 0

    def word(self, point):
        start = point
        while start > 0 and (self.text[start - 1].isalnum() or self.text[start - 1] == "_"):
            start -= 1

        end = point
        while end < len(self.text) and (self.text[end].isalnum() or self.text[end] == "_"):
            end += 1

        return self.module.sublime.Region(start, end)


class FakeCommandView:
    _next_id = 1

    def __init__(self, file_name="", dirty=False):
        self._file_name = file_name
        self._dirty = dirty
        self.commands = []
        self._window = None
        self._id = FakeCommandView._next_id
        FakeCommandView._next_id += 1

    def file_name(self):
        return self._file_name

    def is_dirty(self):
        return self._dirty

    def id(self):
        return self._id

    def window(self):
        return self._window

    def run_command(self, name, args=None):
        self.commands.append((name, args))
        if name == "save":
            self._dirty = False


class FakeWindow:
    def __init__(self, view=None):
        self._view = view
        self.commands = []
        if view is not None:
            view._window = self

    def active_view(self):
        return self._view

    def run_command(self, name, args=None):
        self.commands.append((name, args))


class LiuCondaEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.module = load_liu_conda()
        FakeView.module = self.module
        settings = self.module._settings()
        settings.set("backend", "conda")
        settings.set("selected_env", "")
        settings.set("envs", [])
        settings.set("runtime_documentation_import_whitelist", [])
        self.module._clear_env_path_cache()
        self.module._clear_backend_executable_cache()
        self.module._source_cache.clear()

    def test_environment_command_prefers_selected_env_path(self):
        env_path = os.path.abspath(r"C:\envs\science")
        settings = self.module._settings()
        settings.set("selected_env", env_path)
        settings.set("envs", [{"name": "science", "path": env_path}])

        with mock.patch.object(self.module, "_backend_executable", return_value="conda"):
            command = self.module._environment_command("python")

        self.assertEqual(command, ["conda", "run", "-p", env_path, "python"])

    def test_environment_command_falls_back_to_env_name(self):
        settings = self.module._settings()
        settings.set("selected_env", "science")
        settings.set("envs", [])

        with mock.patch.object(self.module, "_backend_executable", return_value="conda"):
            command = self.module._environment_command("python")

        self.assertEqual(command, ["conda", "run", "-n", "science", "python"])

    def test_set_envs_migrates_legacy_selected_name_to_path(self):
        env_path = os.path.abspath(r"C:\envs\science")
        settings = self.module._settings()
        settings.set("selected_env", "science")

        self.module._set_envs([
            {"name": "science", "path": env_path},
            {"name": "other", "path": os.path.abspath(r"C:\envs\other")},
        ])

        self.assertEqual(settings.get("selected_env"), env_path)

    def test_find_env_record_supports_path_and_legacy_name(self):
        env_path = os.path.abspath(r"C:\envs\science")
        envs = [{"name": "science", "path": env_path}]

        by_path = self.module._find_env_record(env_path, envs)
        by_name = self.module._find_env_record("science", envs)

        self.assertEqual(by_path, envs[0])
        self.assertEqual(by_name, envs[0])

    def test_env_menu_items_disambiguate_duplicate_names(self):
        first = os.path.abspath(r"C:\envs\science")
        second = os.path.abspath(r"D:\envs\science")
        items = self.module._env_menu_items(
            [
                {"name": "science", "path": first},
                {"name": "science", "path": second},
            ]
        )

        self.assertEqual(items[0]["args"]["env"], first)
        self.assertEqual(items[1]["args"]["env"], second)
        self.assertIn(first, items[0]["caption"])
        self.assertIn(second, items[1]["caption"])

    def test_runtime_documentation_whitelist_filters_blank_items(self):
        settings = self.module._settings()
        settings.set("runtime_documentation_import_whitelist", ["numpy", "", "  ", 123])

        whitelist = self.module._runtime_documentation_import_whitelist()

        self.assertEqual(whitelist, ["numpy", "123"])

    def test_discover_envs_keeps_duplicate_names_for_distinct_paths(self):
        first = os.path.abspath(r"C:\envs\science")
        second = os.path.abspath(r"D:\envs\science")

        def fake_run_json(cmd):
            if cmd[1:] == ["info", "--json"]:
                return {"root_prefix": os.path.abspath(r"C:\envs\base")}
            if cmd[1:] == ["env", "list", "--json"]:
                return {"envs": [first, second]}
            raise AssertionError("unexpected command: {}".format(cmd))

        with mock.patch.object(self.module, "_backend_executable", return_value="conda"), mock.patch.object(
            self.module, "_run_json", side_effect=fake_run_json
        ):
            envs = self.module._discover_envs("conda")

        self.assertEqual(envs, [{"name": "science", "path": first}, {"name": "science", "path": second}])

    def test_discover_envs_deduplicates_same_path(self):
        env_path = os.path.abspath(r"C:\envs\science")

        def fake_run_json(cmd):
            if cmd[1:] == ["info", "--json"]:
                return {}
            if cmd[1:] == ["env", "list", "--json"]:
                return {"envs": [env_path, env_path]}
            raise AssertionError("unexpected command: {}".format(cmd))

        with mock.patch.object(self.module, "_backend_executable", return_value="conda"), mock.patch.object(
            self.module, "_run_json", side_effect=fake_run_json
        ):
            envs = self.module._discover_envs("conda")

        self.assertEqual(envs, [{"name": "science", "path": env_path}])

    def test_discover_envs_skips_conda_build_internal_envs(self):
        env_path = os.path.abspath(r"C:\envs\science")
        host_env = os.path.abspath(r"C:\envs\science\conda-bld\pkg_123\_h_env")

        def fake_run_json(cmd):
            if cmd[1:] == ["info", "--json"]:
                return {}
            if cmd[1:] == ["env", "list", "--json"]:
                return {"envs": [env_path, host_env]}
            raise AssertionError("unexpected command: {}".format(cmd))

        with mock.patch.object(self.module, "_backend_executable", return_value="conda"), mock.patch.object(
            self.module, "_run_json", side_effect=fake_run_json
        ):
            envs = self.module._discover_envs("conda")

        self.assertEqual(envs, [{"name": "science", "path": env_path}])

    def test_discover_envs_raises_last_error_when_all_queries_fail(self):
        primary_error = RuntimeError("primary failed")
        fallback_error = RuntimeError("fallback failed")

        def fake_run_json(cmd):
            if cmd[1:] == ["info", "--json"]:
                return {}
            if cmd[1:] == ["env", "list", "--json"]:
                raise primary_error
            if cmd[1:] == ["info", "--envs", "--json"]:
                raise fallback_error
            raise AssertionError("unexpected command: {}".format(cmd))

        with mock.patch.object(self.module, "_backend_executable", return_value="conda"), mock.patch.object(
            self.module, "_run_json", side_effect=fake_run_json
        ):
            with self.assertRaisesRegex(RuntimeError, "fallback failed"):
                self.module._discover_envs("conda")

    def test_program_files_roots_scans_c_to_f_when_global_search_disabled(self):
        settings = self.module._settings()
        settings.set("global_path_search", False)
        seen_paths = []

        def fake_isdir(path):
            seen_paths.append(path)
            return path == os.path.join("D:\\", "Program Files")

        with mock.patch.dict(os.environ, {"PROGRAMFILES": r"C:\Program Files"}, clear=False), mock.patch.object(
            self.module, "_windows_drive_roots"
        ) as drive_roots, mock.patch.object(self.module.os.path, "isdir", side_effect=fake_isdir):
            roots = self.module._program_files_roots()

        drive_roots.assert_not_called()
        self.assertIn(r"C:\Program Files", roots)
        self.assertIn(os.path.join("D:\\", "Program Files"), roots)
        self.assertIn(os.path.join("F:\\", "Program Files (x86)"), seen_paths)

    def test_program_files_roots_uses_drive_scan_when_global_search_enabled(self):
        settings = self.module._settings()
        settings.set("global_path_search", True)
        drive = "D:\\"

        def fake_isdir(path):
            return path == os.path.join(drive, "Program Files")

        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(
            self.module, "_windows_drive_roots", return_value=[drive]
        ) as drive_roots, mock.patch.object(self.module.os.path, "isdir", side_effect=fake_isdir):
            roots = self.module._program_files_roots()

        drive_roots.assert_called_once_with()
        self.assertIn(os.path.join(drive, "Program Files"), roots)

    def test_env_name_marks_root_prefix_as_base(self):
        root_prefix = os.path.abspath(r"C:\envs\base")
        self.assertEqual(self.module._env_name(root_prefix, root_prefix), "base")

    def test_same_path_normalizes_case(self):
        path = os.path.abspath(r"C:\envs\science")
        self.assertTrue(self.module._same_path(path, path.upper()))

    def test_path_uri_round_trip(self):
        path = os.path.abspath(r"C:\work\demo.py")
        uri = self.module._path_to_uri(path)
        round_trip = self.module._uri_to_path(uri)

        self.assertEqual(round_trip, path)

    def test_zuban_diagnostic_item_converts_zero_based_positions(self):
        uri = self.module._path_to_uri(os.path.abspath(r"C:\work\demo.py"))
        diagnostic = self.module._zuban_diagnostic_item(
            uri,
            {
                "range": {
                    "start": {"line": 2, "character": 4},
                    "end": {"line": 2, "character": 7},
                },
                "severity": 2,
                "code": "X1",
                "source": "zuban",
                "message": "problem",
            },
        )

        self.assertEqual(diagnostic["line"], 3)
        self.assertEqual(diagnostic["column"], 5)
        self.assertEqual(diagnostic["end_column"], 8)
        self.assertEqual(diagnostic["code"], "X1")

    def test_format_zuban_diagnostic_indents_multiline_message(self):
        text = self.module._format_zuban_diagnostic(
            {
                "path": "demo.py",
                "line": 3,
                "column": 5,
                "severity": 2,
                "code": "Z100",
                "message": "first line\nsecond line",
            }
        )

        self.assertIn("warning [Z100]", text)
        self.assertIn("first line\n    second line", text)

    def test_view_source_evicts_only_oldest_cache_entry(self):
        first_view = FakeView("source-0", buffer_id=0, change_count=0)
        self.module._view_source(first_view)

        for index in range(1, self.module.SOURCE_CACHE_LIMIT + 1):
            view = FakeView("source-{}".format(index), buffer_id=index, change_count=index)
            self.module._view_source(view)

        self.assertEqual(len(self.module._source_cache), self.module.SOURCE_CACHE_LIMIT)
        self.assertNotIn((0, 0), self.module._source_cache)
        self.assertIn((1, 1), self.module._source_cache)

    def test_should_query_signature_handles_multiline_calls(self):
        text = "func(\n    first,\n    second,\n)"
        view = FakeView(text)
        point = text.index("second,") + len("second,")

        self.assertTrue(self.module._should_query_signature(view, point, ","))

    def test_should_query_signature_does_not_cross_unrelated_statements(self):
        text = "done()\nvalue,"
        view = FakeView(text)
        point = len(text)

        self.assertFalse(self.module._should_query_signature(view, point, ","))


class LiuCondaWorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.module = load_liu_conda()

    def test_stop_jedi_worker_wait_false_stops_via_async_helper(self):
        worker = FakeWorker()
        self.module._jedi_worker = worker

        with mock.patch.object(self.module.threading, "Thread", ImmediateThread):
            self.module._stop_jedi_worker(wait=False)

        self.assertIsNone(self.module._jedi_worker)
        self.assertEqual(worker.stop_calls, 1)
        self.assertNotIn(worker, self.module._detached_workers)

    def test_stop_jedi_worker_wait_true_stops_immediately(self):
        worker = FakeWorker()
        self.module._jedi_worker = worker

        self.module._stop_jedi_worker(wait=True)

        self.assertIsNone(self.module._jedi_worker)
        self.assertEqual(worker.stop_calls, 1)

    def test_stop_detached_workers_drains_all_pending_workers(self):
        first = FakeWorker()
        second = FakeWorker()
        self.module._track_detached_worker(first)
        self.module._track_detached_worker(second)

        self.module._stop_detached_workers()

        self.assertEqual(first.stop_calls, 1)
        self.assertEqual(second.stop_calls, 1)

    def test_check_zuban_async_uses_lightweight_python_probe(self):
        env_path = os.path.abspath(r"C:\envs\science")
        settings = self.module._settings()
        settings.set("selected_env", env_path)
        settings.set("zuban", "zuban")

        with mock.patch.object(self.module.threading, "Thread", ImmediateThread), mock.patch.object(
            self.module, "_cached_zuban_status"
        ), mock.patch.object(self.module, "_zuban_status_cache_key", return_value=(env_path, "zuban", 0)), mock.patch.object(
            self.module, "_run_python", return_value="1.2.3"
        ) as run_python, mock.patch.object(self.module, "_ZubanWorker") as zuban_worker, mock.patch.object(
            self.module, "_set_zuban_available"
        ) as set_available, mock.patch.object(self.module, "_store_zuban_status") as store_status:
            self.module._check_zuban_async()

        run_python.assert_called_once()
        zuban_worker.assert_not_called()
        store_status.assert_not_called()
        self.assertEqual(set_available.call_args_list[-1], mock.call(True, "Zuban 1.2.3", env=env_path))

    def test_check_zuban_async_uses_worker_for_custom_command(self):
        env_path = os.path.abspath(r"C:\envs\science")
        settings = self.module._settings()
        settings.set("selected_env", env_path)
        settings.set("zuban", "custom-zuban")
        worker = mock.Mock()
        worker.server_info_message.return_value = "Custom Zuban"

        with mock.patch.object(self.module.threading, "Thread", ImmediateThread), mock.patch.object(
            self.module, "_cached_zuban_status", return_value=""
        ), mock.patch.object(self.module, "_zuban_status_cache_key", return_value=(env_path, "custom-zuban", 0)), mock.patch.object(
            self.module, "_ZubanWorker", return_value=worker
        ) as zuban_worker, mock.patch.object(self.module, "_run_python") as run_python, mock.patch.object(
            self.module, "_set_zuban_available"
        ) as set_available, mock.patch.object(self.module, "_store_zuban_status") as store_status:
            self.module._check_zuban_async()

        run_python.assert_not_called()
        zuban_worker.assert_called_once_with(env_path, "")
        worker.start.assert_called_once_with()
        worker.wait_until_settled.assert_called_once_with(1.0)
        worker.stop.assert_called_once_with()
        store_status.assert_called_once_with((env_path, "custom-zuban", 0), "Custom Zuban")
        self.assertEqual(set_available.call_args_list[-1], mock.call(True, "Custom Zuban", env=env_path))

    def test_check_zuban_async_uses_cached_custom_status(self):
        env_path = os.path.abspath(r"C:\envs\science")
        settings = self.module._settings()
        settings.set("selected_env", env_path)
        settings.set("zuban", "custom-zuban")

        with mock.patch.object(self.module, "_zuban_status_cache_key", return_value=(env_path, "custom-zuban", 0)), mock.patch.object(
            self.module, "_cached_zuban_status", return_value="Cached Zuban"
        ) as cached_status, mock.patch.object(self.module, "_set_zuban_available") as set_available, mock.patch.object(
            self.module, "_run_python"
        ) as run_python, mock.patch.object(self.module, "_ZubanWorker") as zuban_worker:
            self.module._check_zuban_async()

        cached_status.assert_called_once_with((env_path, "custom-zuban", 0))
        set_available.assert_called_once_with(True, "Cached Zuban", env=env_path, persist=True, stop_worker=False)
        run_python.assert_not_called()
        zuban_worker.assert_not_called()

    def test_set_jedi_available_skips_save_when_state_is_unchanged(self):
        env_path = os.path.abspath(r"C:\envs\science")
        settings = self.module._settings()
        settings.set("selected_env", env_path)
        settings.set("jedi_available", True)
        settings.set("jedi_checked_env", env_path)
        settings.set("jedi_message", "Jedi 1.0")

        with mock.patch.object(self.module, "_save_settings") as save_settings, mock.patch.object(
            self.module, "_write_menu"
        ) as write_menu, mock.patch.object(self.module, "_prewarm_jedi_worker"):
            self.module._set_jedi_available(True, "Jedi 1.0", env=env_path)

        save_settings.assert_not_called()
        write_menu.assert_not_called()

    def test_set_jedi_available_prewarms_worker_when_available(self):
        env_path = os.path.abspath(r"C:\envs\science")
        settings = self.module._settings()
        settings.set("selected_env", env_path)

        with mock.patch.object(self.module, "_save_settings") as save_settings, mock.patch.object(
            self.module, "_write_menu"
        ) as write_menu, mock.patch.object(self.module, "_prewarm_jedi_worker") as prewarm:
            self.module._set_jedi_available(True, "Jedi 1.2.3", env=env_path)

        save_settings.assert_called_once()
        write_menu.assert_called_once()
        prewarm.assert_called_once_with(env_path)


class LiuCondaCommandFlowTests(unittest.TestCase):
    def setUp(self):
        self.module = load_liu_conda()
        settings = self.module._settings()
        settings.set("backend", "conda")
        settings.set("selected_env", "")
        settings.set("envs", [])

    def test_set_backend_command_resets_selection_and_triggers_refresh(self):
        settings = self.module._settings()
        settings.set("selected_env", os.path.abspath(r"C:\envs\science"))
        window = FakeWindow()
        command = self.module.LiuCondaSetBackendCommand(window)

        with mock.patch.object(self.module, "_stop_jedi_worker") as stop_jedi, mock.patch.object(
            self.module, "_stop_zuban_worker"
        ) as stop_zuban, mock.patch.object(self.module, "_clear_backend_executable_cache") as clear_cache, mock.patch.object(
            self.module, "_set_jedi_available"
        ) as set_jedi, mock.patch.object(self.module, "_set_zuban_available") as set_zuban:
            command.run("micromamba")

        self.assertEqual(settings.get("backend"), "micromamba")
        self.assertEqual(settings.get("selected_env"), "")
        clear_cache.assert_called_once_with()
        stop_jedi.assert_called_once_with(wait=False)
        stop_zuban.assert_called_once_with(wait=False)
        set_jedi.assert_called_once_with(False, "No environment selected")
        set_zuban.assert_called_once_with(False, "No environment selected")
        self.assertEqual(window.commands[-1], ("liu_conda_refresh_env", None))

    def test_refresh_env_command_success_updates_state_and_rechecks_features(self):
        old_env = os.path.abspath(r"C:\envs\old")
        new_env = os.path.abspath(r"C:\envs\new")
        settings = self.module._settings()
        settings.set("selected_env", old_env)
        window = FakeWindow()
        command = self.module.LiuCondaRefreshEnvCommand(window)
        envs = [{"name": "new", "path": new_env}]

        with mock.patch.object(self.module.threading, "Thread", ImmediateThread), mock.patch.object(
            self.module, "_discover_envs", return_value=envs
        ), mock.patch.object(self.module, "_write_menu") as write_menu, mock.patch.object(
            self.module, "_check_jedi_async"
        ) as check_jedi, mock.patch.object(self.module, "_check_zuban_async") as check_zuban, mock.patch.object(
            self.module, "_stop_jedi_worker"
        ) as stop_jedi, mock.patch.object(self.module, "_stop_zuban_worker") as stop_zuban:
            command.run()

        self.assertEqual(settings.get("selected_env"), new_env)
        write_menu.assert_called_once_with(envs)
        check_jedi.assert_called_once_with()
        check_zuban.assert_called_once_with()
        stop_jedi.assert_called_once_with(wait=False)
        stop_zuban.assert_called_once_with(wait=False)

    def test_refresh_env_command_reports_backend_errors(self):
        window = FakeWindow()
        command = self.module.LiuCondaRefreshEnvCommand(window)

        with mock.patch.object(self.module.threading, "Thread", ImmediateThread), mock.patch.object(
            self.module, "_discover_envs", side_effect=FileNotFoundError("conda missing")
        ), mock.patch.object(self.module.sublime, "error_message") as error_message:
            command.run()

        error_message.assert_called_once_with("conda missing")

    def test_select_env_command_updates_selection_and_triggers_checks(self):
        env_path = os.path.abspath(r"C:\envs\science")
        settings = self.module._settings()
        settings.set("envs", [{"name": "science", "path": env_path}])
        window = FakeWindow()
        command = self.module.LiuCondaSelectEnvCommand(window)

        with mock.patch.object(self.module, "_stop_jedi_worker") as stop_jedi, mock.patch.object(
            self.module, "_stop_zuban_worker"
        ) as stop_zuban, mock.patch.object(self.module, "_check_jedi_async") as check_jedi, mock.patch.object(
            self.module, "_check_zuban_async"
        ) as check_zuban, mock.patch.object(self.module.sublime, "status_message") as status_message:
            command.run(env_path)

        self.assertEqual(settings.get("selected_env"), env_path)
        stop_jedi.assert_called_once_with(wait=False)
        stop_zuban.assert_called_once_with(wait=False)
        check_jedi.assert_called_once_with()
        check_zuban.assert_called_once_with()
        status_message.assert_called_once()

    def test_run_command_executes_current_file_in_selected_env(self):
        env_path = os.path.abspath(r"C:\envs\science")
        file_path = os.path.abspath(r"C:\work\demo.py")
        settings = self.module._settings()
        settings.set("selected_env", env_path)
        view = FakeCommandView(file_name=file_path, dirty=False)
        window = FakeWindow(view)
        command = self.module.LiuCondaRunCommand(window)

        with mock.patch.object(self.module, "_python_command", return_value=["conda", "run"]):
            command.run()

        self.assertEqual(
            window.commands[-1],
            (
                "exec",
                {
                    "cmd": ["conda", "run", file_path],
                    "working_dir": os.path.dirname(file_path),
                    "file_regex": r"^  File \"(...*?)\", line ([0-9]*)",
                },
            ),
        )

    def test_run_command_saves_dirty_file_before_exec(self):
        env_path = os.path.abspath(r"C:\envs\science")
        file_path = os.path.abspath(r"C:\work\demo.py")
        settings = self.module._settings()
        settings.set("selected_env", env_path)
        view = FakeCommandView(file_name=file_path, dirty=True)
        window = FakeWindow(view)
        command = self.module.LiuCondaRunCommand(window)
        listener = self.module.LiuCondaJediEventListener()

        with mock.patch.object(self.module, "_python_command", return_value=["conda", "run"]):
            command.run()
            self.assertEqual(view.commands[0], ("save", None))
            self.assertEqual(window.commands, [])
            listener.on_post_save_async(view)

        self.assertEqual(window.commands[-1][0], "exec")

    def test_post_save_only_runs_when_view_was_queued(self):
        file_path = os.path.abspath(r"C:\work\demo.py")
        view = FakeCommandView(file_name=file_path, dirty=False)
        window = FakeWindow(view)
        listener = self.module.LiuCondaJediEventListener()

        with mock.patch.object(self.module, "_python_command", return_value=["conda", "run"]):
            listener.on_post_save_async(view)

        self.assertEqual(window.commands, [])

    def test_run_command_reports_missing_selected_env(self):
        file_path = os.path.abspath(r"C:\work\demo.py")
        view = FakeCommandView(file_name=file_path, dirty=False)
        window = FakeWindow(view)
        command = self.module.LiuCondaRunCommand(window)

        with mock.patch.object(self.module.sublime, "error_message") as error_message:
            command.run()

        error_message.assert_called_once()
        self.assertEqual(window.commands, [])


if __name__ == "__main__":
    unittest.main()
