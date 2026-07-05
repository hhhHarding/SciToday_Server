import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tasks


class ConfigCacheTests(unittest.TestCase):
    """config.json 按 mtime 缓存：mtime 不变时不重复读盘，save_config 后失效。"""

    def setUp(self):
        # 隔离缓存，避免受其他用例/真实配置影响。
        tasks._reset_config_cache_for_tests()

    def tearDown(self):
        tasks._reset_config_cache_for_tests()

    def test_repeated_loads_do_not_reparse_when_mtime_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg_path = Path(temp_dir) / "config.json"
            cfg_path.write_text(json.dumps({"ai": {"model": "deepseek-chat"}}), encoding="utf-8")

            reads = []
            real_read_text = Path.read_text

            def counting_read_text(self, *args, **kwargs):
                if self == cfg_path:
                    reads.append(1)
                return real_read_text(self, *args, **kwargs)

            with (
                patch.object(tasks, "CONFIG_PATH", cfg_path),
                patch.object(Path, "read_text", counting_read_text),
            ):
                first = tasks.load_config()
                for _ in range(10):
                    tasks.load_config()

            self.assertEqual(first, {"ai": {"model": "deepseek-chat"}})
            self.assertEqual(len(reads), 1)

    def test_returned_config_is_a_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg_path = Path(temp_dir) / "config.json"
            cfg_path.write_text(json.dumps({"rss": {"lookback_days": 7}}), encoding="utf-8")
            with patch.object(tasks, "CONFIG_PATH", cfg_path):
                first = tasks.load_config()
                first["rss"]["lookback_days"] = 999  # 原地改动不得污染缓存
                second = tasks.load_config()

        self.assertEqual(second["rss"]["lookback_days"], 7)

    def test_save_config_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg_path = Path(temp_dir) / "config.json"
            cfg_path.write_text(json.dumps({"ai": {"model": "old"}}), encoding="utf-8")
            with patch.object(tasks, "CONFIG_PATH", cfg_path):
                self.assertEqual(tasks.load_config()["ai"]["model"], "old")
                tasks.save_config({"ai": {"model": "new"}})
                self.assertEqual(tasks.load_config()["ai"]["model"], "new")

    def test_missing_config_returns_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg_path = Path(temp_dir) / "does_not_exist.json"
            with patch.object(tasks, "CONFIG_PATH", cfg_path):
                self.assertEqual(tasks.load_config(), {})


if __name__ == "__main__":
    unittest.main()
