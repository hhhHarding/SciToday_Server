import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tasks


class DigestIndexSentinelTests(unittest.TestCase):
    """inbox 索引哨兵：inbox 无变化时不重扫文件，新增文件后触发一次对账。"""

    def setUp(self):
        tasks._reset_migration_cache_for_tests()
        tasks._digest_index_sentinels.clear()
        tasks._digest_index_locks.clear()

    def tearDown(self):
        tasks._reset_migration_cache_for_tests()
        tasks._digest_index_sentinels.clear()
        tasks._digest_index_locks.clear()

    def _write_digest(self, inbox, name):
        (inbox / name).write_text(
            '<meta name="digest-source" content="rss"><title>t</title>',
            encoding="utf-8",
        )

    def test_unchanged_inbox_skips_file_scan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            inbox = Path(temp_dir) / "inbox"
            inbox.mkdir()
            db_path = Path(temp_dir) / "digest.db"
            self._write_digest(inbox, "20260101_000000_a.html")

            with (
                patch.object(tasks, "INBOX_DIR", inbox),
                patch.object(tasks, "DIGEST_DB", db_path),
            ):
                # 首次：哨兵为 None → 触发对账，会读文件。
                with patch.object(tasks, "_digest_from_file", wraps=tasks._digest_from_file) as spy:
                    tasks._sync_digest_index()
                    self.assertEqual(spy.call_count, 1)

                # 再次：inbox 未变 → 不应再读任何文件。
                with patch.object(tasks, "_digest_from_file", wraps=tasks._digest_from_file) as spy2:
                    tasks._sync_digest_index()
                    tasks._sync_digest_index()
                    self.assertEqual(spy2.call_count, 0)

    def test_new_file_triggers_reconcile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            inbox = Path(temp_dir) / "inbox"
            inbox.mkdir()
            db_path = Path(temp_dir) / "digest.db"
            self._write_digest(inbox, "20260101_000000_a.html")

            with (
                patch.object(tasks, "INBOX_DIR", inbox),
                patch.object(tasks, "DIGEST_DB", db_path),
            ):
                tasks._sync_digest_index()  # 建立哨兵基线

                self._write_digest(inbox, "20260102_000000_b.html")
                with patch.object(tasks, "_digest_from_file", wraps=tasks._digest_from_file) as spy:
                    tasks._sync_digest_index()
                    # 文件数变化 → 触发对账，扫描到 2 个文件。
                    self.assertEqual(spy.call_count, 2)

                con = tasks._digest_db()
                count = con.execute("SELECT COUNT(*) FROM digests").fetchone()[0]
                con.close()
            self.assertEqual(count, 2)

    def test_force_reconcile_ignores_sentinel(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            inbox = Path(temp_dir) / "inbox"
            inbox.mkdir()
            db_path = Path(temp_dir) / "digest.db"
            self._write_digest(inbox, "20260101_000000_a.html")

            with (
                patch.object(tasks, "INBOX_DIR", inbox),
                patch.object(tasks, "DIGEST_DB", db_path),
            ):
                tasks._sync_digest_index()
                with patch.object(tasks, "_digest_from_file", wraps=tasks._digest_from_file) as spy:
                    tasks._sync_digest_index(force=True)
                    self.assertEqual(spy.call_count, 1)


if __name__ == "__main__":
    unittest.main()
