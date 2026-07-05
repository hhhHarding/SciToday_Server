import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tenancy.config_io import atomic_write_json
from tenancy.context import OWNER_TENANT_ID, get_current_tenant_id, tenant_context


class AtomicConfigWriteTests(unittest.TestCase):
    def test_replace_failure_preserves_old_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text('{"version": 1}\n', encoding="utf-8")

            with patch("tenancy.config_io.os.replace", side_effect=OSError("injected")):
                with self.assertRaises(OSError):
                    atomic_write_json(path, {"version": 2}, tenant_id="owner")

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"version": 1},
            )
            self.assertEqual(list(path.parent.glob(".config.json.*.tmp")), [])

    def test_successful_write_replaces_complete_document(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            atomic_write_json(
                path,
                {"name": "测试", "nested": {"value": 2}},
                tenant_id="t_alpha",
            )

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"name": "测试", "nested": {"value": 2}},
            )

    def test_owner_context_is_default_and_context_resets(self):
        self.assertEqual(get_current_tenant_id(), OWNER_TENANT_ID)
        with tenant_context("t_alpha"):
            self.assertEqual(get_current_tenant_id(), "t_alpha")
        self.assertEqual(get_current_tenant_id(), OWNER_TENANT_ID)


if __name__ == "__main__":
    unittest.main()

