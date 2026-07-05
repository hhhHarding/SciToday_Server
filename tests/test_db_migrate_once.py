import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tasks


class DbMigrateOnceTests(unittest.TestCase):
    """迁移守卫：同一 DB 路径的建表/迁移体只在进程内首次连接时执行一次。"""

    def setUp(self):
        tasks._reset_migration_cache_for_tests()

    def tearDown(self):
        tasks._reset_migration_cache_for_tests()

    def test_admin_migration_runs_only_once_per_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "admin.db"
            calls = []
            real_migrate = tasks._migrate_admin_db

            def counting_migrate(con):
                calls.append(1)
                return real_migrate(con)

            with (
                patch.object(tasks, "ADMIN_DB", db_path),
                patch.object(tasks, "_migrate_admin_db", side_effect=counting_migrate),
            ):
                for _ in range(5):
                    tasks._admin_db().close()

            self.assertEqual(len(calls), 1)

    def test_admin_open_does_not_overwrite_preference_weight(self):
        """回归：开连接不应再触发全表权重 UPDATE，手动改的权重要被保留。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "admin.db"
            with patch.object(tasks, "ADMIN_DB", db_path):
                con = tasks._admin_db()
                con.execute("""INSERT INTO interest_feedback(
                    filename, first_interested_ts, updated_ts, interested,
                    preference_weight, primary_signal)
                    VALUES('x.html', 1, 1, 1, 12345, 'manual')""")
                con.commit()
                con.close()

                # 再次开连接：不应重跑权重重算（那会把 12345 覆盖成配置权重）。
                con = tasks._admin_db()
                row = con.execute(
                    "SELECT preference_weight, primary_signal FROM interest_feedback WHERE filename='x.html'"
                ).fetchone()
                con.close()

            self.assertEqual(row, (12345, "manual"))

    def test_connection_enables_wal_and_busy_timeout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "admin.db"
            with patch.object(tasks, "ADMIN_DB", db_path):
                con = tasks._admin_db()
                journal_mode = con.execute("PRAGMA journal_mode").fetchone()[0]
                busy_timeout = con.execute("PRAGMA busy_timeout").fetchone()[0]
                con.close()

        self.assertEqual(str(journal_mode).lower(), "wal")
        self.assertEqual(busy_timeout, 5000)


if __name__ == "__main__":
    unittest.main()
