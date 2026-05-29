import ast
import json
import os
import pickle
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from unit_test.test_support import load_jedi_worker


class JediWorkerUtilityTests(unittest.TestCase):
    def setUp(self):
        self.module = load_jedi_worker()

    def test_dotted_expression_at_returns_expression_under_cursor(self):
        source = "value = np.random.rand\n"
        expression = self.module.dotted_expression_at(source, 1, len("value = np.random.rand"))

        self.assertEqual(expression, "np.random.rand")

    def test_dotted_expression_at_rejects_invalid_expression(self):
        source = "value = np..rand\n"
        expression = self.module.dotted_expression_at(source, 1, len("value = np..rand"))

        self.assertEqual(expression, "")

    def test_import_allowed_matches_prefix_entries(self):
        self.assertTrue(self.module.import_allowed(["numpy", "random", "rand"], ["numpy"]))
        self.assertFalse(self.module.import_allowed(["pandas", "DataFrame"], ["numpy"]))

    def test_collect_import_alias_entries_collects_top_level_imports(self):
        tree = ast.parse("import numpy as np\nfrom pandas import DataFrame\n")

        entries = self.module.collect_import_alias_entries(tree)

        self.assertEqual(entries, [(1, "np", "numpy"), (2, "DataFrame", "pandas.DataFrame")])

    def test_collect_import_alias_entries_ignores_function_scoped_imports(self):
        tree = ast.parse("def build():\n    import numpy as np\n")

        self.assertEqual(self.module.collect_import_alias_entries(tree), [])

    def test_import_aliases_ignores_future_imports_after_current_line(self):
        source = "import numpy as np\nvalue = np.array([])\nimport pandas as pd\n"

        aliases = self.module.import_aliases(source, 2)

        self.assertEqual(aliases, {"np": "numpy"})

    def test_imported_target_parts_resolve_alias_chain(self):
        source = "import numpy as np\nvalue = np.random.rand()\n"

        target = self.module.imported_target_parts(source, "np.random.rand", 2)

        self.assertEqual(target, ["numpy", "random", "rand"])

    def test_run_action_with_recovery_uses_lock_when_requested(self):
        fake_jedi = SimpleNamespace(settings=SimpleNamespace(cache_directory=""))

        with mock.patch.object(self.module, "run_action", return_value={"ok": True}) as run_action, mock.patch.object(
            self.module, "acquire_cache_lock"
        ) as acquire_lock, mock.patch.object(self.module, "remove_cache_lock") as remove_lock:
            result = self.module.run_action_with_recovery(
                fake_jedi,
                "complete",
                {"source": ""},
                "cache-dir",
                use_lock=True,
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(fake_jedi.settings.cache_directory, "cache-dir")
        acquire_lock.assert_called_once_with(os.path.join("cache-dir", ".lock"))
        remove_lock.assert_called_once_with(os.path.join("cache-dir", ".lock"))
        run_action.assert_called_once()

    def test_run_action_with_recovery_retries_after_cache_corruption(self):
        fake_jedi = SimpleNamespace(settings=SimpleNamespace(cache_directory=""))
        calls = []

        def fake_run_action(jedi, action, payload):
            if not calls:
                calls.append("failed")
                raise pickle.UnpicklingError("broken cache")
            return {"ok": True}

        with mock.patch.object(self.module, "run_action", side_effect=fake_run_action), mock.patch.object(
            self.module, "clear_cache_directory"
        ) as clear_cache:
            result = self.module.run_action_with_recovery(fake_jedi, "complete", {"source": ""}, "cache-dir")

        self.assertEqual(result, {"ok": True})
        clear_cache.assert_called_once_with("cache-dir")

    def test_lock_is_stale_when_file_is_older_than_30_seconds(self):
        handle = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        try:
            handle.write("old lock")
            handle.close()
            old_time = time.time() - 60
            os.utime(handle.name, (old_time, old_time))

            self.assertTrue(self.module._lock_is_stale(handle.name))
        finally:
            os.unlink(handle.name)

    def test_lock_is_not_stale_when_file_is_fresh(self):
        handle = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        try:
            handle.write("fresh lock")
            handle.close()

            self.assertFalse(self.module._lock_is_stale(handle.name))
        finally:
            os.unlink(handle.name)

    def test_resolve_imported_object_finds_documented_attribute(self):
        full_name, obj = self.module.resolve_imported_object(["json", "loads"])

        self.assertEqual(full_name, "json.loads")
        self.assertTrue(callable(obj))


if __name__ == "__main__":
    unittest.main()