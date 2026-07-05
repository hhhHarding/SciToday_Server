import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tasks
from server_config import ServerPaths
from tenancy.context import tenant_context
from tenancy.paths import TenantPaths
from tenancy.registry import TenantRegistry


FEED_A = "https://a.example/feed"


class _FakeEntry:
    def __init__(self, title, link):
        self.title = title
        self.link = link
        self.summary = f"summary of {title}"
        self.id = link
        self.published_parsed = None


def _feed_entries(feed, per_feed_limit, since_ts=0):
    entries = [
        _FakeEntry(f"{feed['title']}-article-1", f"{feed['url']}/1"),
        _FakeEntry(f"{feed['title']}-article-2", f"{feed['url']}/2"),
    ]
    return feed, entries[:per_feed_limit], None, 5, 0


class SoftDeleteDigestTests(unittest.TestCase):
    """软删语义：删卡片只从本租户显示列表隐藏，可恢复，绝不动共享内容。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.server_paths = ServerPaths(Path(self.temp_dir.name) / "server")
        self.server_paths.ensure_global_directories()
        self.registry = TenantRegistry(self.server_paths)
        self.registry.ensure_owner(default_config=self._config("owner-key"))
        self.registry.create_tenant(
            "Alpha", tenant_id="t_alpha", default_config=self._config("alpha-key")
        )
        self._set_feeds("owner", [FEED_A])
        self._set_feeds("t_alpha", [FEED_A])

        self.paths_patch = patch.object(tasks, "SERVER_PATHS", self.server_paths)
        self.paths_patch.start()
        self._reset_caches()

    def tearDown(self):
        self.paths_patch.stop()
        self._reset_caches()
        self.temp_dir.cleanup()

    def _reset_caches(self):
        tasks._reset_config_cache_for_tests()
        tasks._reset_migration_cache_for_tests()
        tasks._digest_index_sentinels.clear()
        tasks._digest_index_locks.clear()
        tasks._db_locks.clear()

    @staticmethod
    def _config(api_key):
        return {
            "ai": {
                "api_key": api_key,
                "base_url": "https://example.invalid",
                "model": "m",
            },
            "rss": {"lookback_days": 7, "max_push_items": 20},
        }

    def _set_feeds(self, tenant_id, urls):
        opml = TenantPaths(self.server_paths.data_root, tenant_id).opml
        outlines = "".join(f'<outline xmlUrl="{u}" text="{u}" />' for u in urls)
        opml.write_text(
            f'<opml version="2.0"><body>{outlines}</body></opml>',
            encoding="utf-8",
        )

    def _seed_tenant_digests(self):
        """跑一次共享消化 + 投递，让 t_alpha 拿到 2 张卡片。返回文件名列表。"""
        def fake_ai_call(prompt, system_prompt=None, temperature=0.1, timeout=120):
            return "中文题目：测试\n中文关键词：甲、乙\n正文内容。"

        with patch.object(tasks, "_fetch_single_feed", side_effect=_feed_entries), \
                patch.object(tasks, "_ai_call", side_effect=fake_ai_call):
            with tenant_context("owner"):
                tasks.run_shared_rss_ingest()
        with patch.object(tasks, "get_interest_profile", return_value=None):
            with tenant_context("t_alpha"):
                tasks.deliver_shared_to_tenant()
                return [d["filename"] for d in tasks.get_recent_digests()]

    def _shared_article_count(self):
        con = tasks._shared_content_db()
        try:
            return con.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        finally:
            con.close()

    def test_migration_adds_deleted_columns(self):
        with tenant_context("t_alpha"):
            con = tasks._digest_db()
            cols = {row[1] for row in con.execute("PRAGMA table_info(digests)")}
            con.close()
        self.assertIn("deleted", cols)
        self.assertIn("deleted_ts", cols)

    def test_delete_hides_from_list_but_keeps_file_and_shared(self):
        filenames = self._seed_tenant_digests()
        self.assertEqual(len(filenames), 2)
        shared_before = self._shared_article_count()
        target = filenames[0]

        with tenant_context("t_alpha"):
            tasks.delete_digest(target)
            visible = {d["filename"] for d in tasks.get_recent_digests()}
            # HTML 文件必须保留（否则 _sync_digest_index 会清掉软删行）。
            self.assertTrue((tasks.INBOX_DIR / target).is_file())

        self.assertNotIn(target, visible)
        self.assertEqual(len(visible), 1)
        # 共享内容分毫不动。
        self.assertEqual(self._shared_article_count(), shared_before)

    def test_deleted_card_appears_in_recycle_bin(self):
        filenames = self._seed_tenant_digests()
        target = filenames[0]
        with tenant_context("t_alpha"):
            tasks.delete_digest(target)
            deleted = tasks.list_deleted_digests()
            deleted_names = {d["filename"] for d in deleted}
        self.assertEqual(deleted_names, {target})
        self.assertTrue(all(d["deleted_ts"] > 0 for d in deleted))

    def test_restore_returns_card_to_list(self):
        filenames = self._seed_tenant_digests()
        target = filenames[0]
        with tenant_context("t_alpha"):
            tasks.delete_digest(target)
            tasks.restore_digest(target)
            visible = {d["filename"] for d in tasks.get_recent_digests()}
            deleted_after = tasks.list_deleted_digests()
        self.assertIn(target, visible)
        self.assertEqual(len(visible), 2)
        self.assertEqual(deleted_after, [])

    def test_sync_index_neither_revives_nor_erases_soft_deleted(self):
        """核心暗礁：对账既不能把软删卡片复活，也不能误删其行。"""
        filenames = self._seed_tenant_digests()
        target = filenames[0]
        with tenant_context("t_alpha"):
            tasks.delete_digest(target)
            # 强制全量对账（模拟读接口每次触发的 _sync_digest_index）。
            tasks._sync_digest_index(force=True)
            visible = {d["filename"] for d in tasks.get_recent_digests()}
            deleted_names = {d["filename"] for d in tasks.list_deleted_digests()}
        # 没被复活到显示列表。
        self.assertNotIn(target, visible)
        # 也没被对账误删——仍在回收站、可恢复。
        self.assertEqual(deleted_names, {target})

    def test_delete_updates_are_hidden_from_incremental_feed(self):
        filenames = self._seed_tenant_digests()
        target = filenames[0]
        with tenant_context("t_alpha"):
            tasks.delete_digest(target)
            updates = tasks.get_digest_updates(after=0, limit=200)
        names = {item["filename"] for item in updates["items"]}
        self.assertNotIn(target, names)

    def test_delete_missing_file_raises(self):
        self._seed_tenant_digests()
        with tenant_context("t_alpha"):
            with self.assertRaises(FileNotFoundError):
                tasks.delete_digest("does_not_exist.html")

    def test_delete_rejects_bad_filename(self):
        with tenant_context("t_alpha"):
            with self.assertRaises(ValueError):
                tasks.delete_digest("../escape.html")
            with self.assertRaises(ValueError):
                tasks.restore_digest("../escape.html")


if __name__ == "__main__":
    unittest.main()
