import tempfile
import unittest
from pathlib import Path

from server_config import DEFAULT_SERVER_DATA_DIR_NAME, ServerPaths


class ServerConfigTests(unittest.TestCase):
    def test_default_server_data_root_is_independent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            paths = ServerPaths.from_env({}, home=home)

        self.assertEqual(paths.data_root, home / DEFAULT_SERVER_DATA_DIR_NAME)
        self.assertEqual(paths.control_db, paths.data_root / "control" / "control.db")
        self.assertEqual(paths.server_log, paths.data_root / "global" / "logs" / "server.log")

    def test_environment_overrides_server_data_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "custom"
            paths = ServerPaths.from_env({"RSSAI_SERVER_DATA_DIR": str(root)})

        self.assertEqual(paths.data_root, root.resolve())

    def test_global_directory_initialization_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = ServerPaths(Path(temp_dir) / "server")
            paths.ensure_global_directories()
            paths.ensure_global_directories()

            self.assertTrue(paths.control_dir.is_dir())
            self.assertTrue(paths.control_backups_dir.is_dir())
            self.assertTrue(paths.logs_dir.is_dir())


if __name__ == "__main__":
    unittest.main()

