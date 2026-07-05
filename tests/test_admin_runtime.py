import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tasks


class AdminRuntimeTests(unittest.TestCase):
    def test_runtime_command_is_atomic_and_uses_global_command_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            command_path = Path(temp_dir) / "global" / "tray_command.json"
            with (
                patch.object(tasks, "TRAY_COMMAND_PATH", command_path),
                patch.object(tasks, "record_event", return_value=None),
            ):
                result = tasks.request_admin_command("restart_backend")

            payload = json.loads(command_path.read_text(encoding="utf-8"))
            self.assertTrue(result["ok"])
            self.assertEqual(payload["command"], "restart_backend")
            self.assertTrue(payload["request_id"])
            self.assertFalse(list(command_path.parent.glob("*.tmp")))

    def test_unknown_runtime_command_is_rejected(self):
        with self.assertRaises(ValueError):
            tasks.request_admin_command("delete_everything")

    def test_local_settings_exposes_scitoday_admin_identity(self):
        with patch.object(
            tasks,
            "_tasklist_contains",
            return_value=True,
        ):
            settings = tasks.get_local_settings()
        self.assertEqual(settings["program_name"], "SciToday_admin")
        self.assertTrue(
            settings["executable_path"].endswith("SciToday_admin.exe")
        )
        self.assertEqual(settings["startup"]["run_name"], "SciToday_admin")
        self.assertTrue(settings["process_running"])

    def test_web_console_exposes_runtime_status_and_restart_control(self):
        root = Path(__file__).resolve().parents[1] / "admin_web"
        html = (root / "index.html").read_text(encoding="utf-8")
        script = (root / "admin.js").read_text(encoding="utf-8")
        self.assertIn("SciToday_admin", html)
        self.assertIn("restartAdminBtn", script)
        self.assertIn("/api/admin/runtime/restart_backend", script)


if __name__ == "__main__":
    unittest.main()
