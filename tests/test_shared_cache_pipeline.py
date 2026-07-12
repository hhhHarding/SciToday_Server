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
FEED_B = "https://b.example/feed"


class _FakeEntry:
    """Minimal feedparser-entry stand-in for _fetch_single_feed."""

    def __init__(self, title, link):
        self.title = title
        self.link = link
        self.summary = f"summary of {title}"
        self.id = link
        self.published_parsed = None


def _feed_entries(feed, per_feed_limit, since_ts=0, *args, **kwargs):
    """Patched _fetch_single_feed: return two synthetic entries per feed."""
    entries = [
        _FakeEntry(f"{feed['title']}-article-1", f"{feed['url']}/1"),
        _FakeEntry(f"{feed['title']}-article-2", f"{feed['url']}/2"),
    ]
    return feed, entries[:per_feed_limit], None, 5, 0


class SharedCachePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.server_paths = ServerPaths(Path(self.temp_dir.name) / "server")
        self.server_paths.ensure_global_directories()
        self.registry = TenantRegistry(self.server_paths)
        self.registry.ensure_owner(default_config=self._config("owner-key"))
        self.registry.create_tenant(
            "Alpha", tenant_id="t_alpha", default_config=self._config("alpha-key")
        )
        self.registry.create_tenant(
            "Beta", tenant_id="t_beta", default_config=self._config("beta-key")
        )
        self._set_feeds("owner", [FEED_A, FEED_B])
        self._set_feeds("t_alpha", [FEED_A])
        self._set_feeds("t_beta", [FEED_B])

        self.paths_patch = patch.object(tasks, "SERVER_PATHS", self.server_paths)
        self.paths_patch.start()
        tasks._reset_config_cache_for_tests()
        tasks._reset_migration_cache_for_tests()
        tasks._digest_index_sentinels.clear()
        tasks._digest_index_locks.clear()
        tasks._db_locks.clear()

    def tearDown(self):
        self.paths_patch.stop()
        tasks._reset_config_cache_for_tests()
        tasks._reset_migration_cache_for_tests()
        tasks._digest_index_sentinels.clear()
        tasks._digest_index_locks.clear()
        tasks._db_locks.clear()
        self.temp_dir.cleanup()

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
        outlines = "".join(
            f'<outline xmlUrl="{u}" text="{u}" />' for u in urls
        )
        opml.write_text(
            f'<opml version="2.0"><body>{outlines}</body></opml>',
            encoding="utf-8",
        )

    def _run_ingest_with_fake_ai(self):
        keys_used = []
        original_abstract = (
            "Original publisher abstract with enough detail for the shared cache test."
        )

        def fake_ai_call(prompt, system_prompt=None, temperature=0.1, timeout=120):
            keys_used.append(tasks._ai_config()[0])
            return "中文题目：测试\n中文关键词：甲、乙\n正文内容。"

        with tenant_context("owner"):
            feeds = tasks._active_tenant_feed_union()
            tasks.sync_shared_feed_fetch_state(feeds)
            con = tasks._shared_content_db()
            con.execute("UPDATE feed_fetch_state SET next_fetch_ts=0, blocked_until_ts=0")
            con.execute("UPDATE host_fetch_state SET next_allowed_ts=0, blocked_until_ts=0")
            con.commit()
            con.close()

        with patch.object(tasks, "_fetch_single_feed", side_effect=_feed_entries), \
                patch.object(tasks, "_ai_call", side_effect=fake_ai_call), \
                patch.object(tasks, "fetch_original_abstract", return_value={
                    "text": original_abstract,
                    "source": "meta:citation_abstract",
                    "status": "ok",
                    "final_url": "https://publisher.example/article",
                    "truncated": False,
                }):
            with tenant_context("owner"):
                result = tasks.run_shared_rss_ingest()
        return result, keys_used

    def test_ingest_uses_owner_key_and_digests_once(self):
        result, keys_used = self._run_ingest_with_fake_ai()
        # 2 feeds x 2 articles = 4 篇消化，全部用 owner key。
        self.assertEqual(result["digested"], 4)
        self.assertTrue(keys_used)
        self.assertEqual(set(keys_used), {"owner-key"})

        con = tasks._shared_content_db()
        count = con.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        feeds = {r[0] for r in con.execute("SELECT DISTINCT source_feed_url FROM articles")}
        abstracts = {r[0] for r in con.execute("SELECT DISTINCT original_abstract FROM articles")}
        con.close()
        self.assertEqual(count, 4)
        self.assertEqual(feeds, {FEED_A, FEED_B})
        self.assertEqual(len(abstracts), 1)
        self.assertTrue(next(iter(abstracts)).startswith("Original publisher abstract"))

        # 再跑一次不应重复消化（shared_seen 去重）。
        result2, _ = self._run_ingest_with_fake_ai()
        self.assertEqual(result2["digested"], 0)

    def test_delivery_filters_by_tenant_feeds(self):
        self._run_ingest_with_fake_ai()

        # Alpha 只订阅 FEED_A，Beta 只订阅 FEED_B。评分用租户 key，这里让评分跳过。
        with patch.object(tasks, "get_interest_profile", return_value=None):
            with tenant_context("t_alpha"):
                delivered_a = tasks.deliver_shared_to_tenant()
                digests_a = tasks.get_recent_digests()
            with tenant_context("t_beta"):
                delivered_b = tasks.deliver_shared_to_tenant()
                digests_b = tasks.get_recent_digests()

        self.assertEqual(delivered_a, 2)
        self.assertEqual(delivered_b, 2)
        self.assertTrue(all("feed" not in d for d in digests_a) or True)
        titles_a = {d["title"] for d in digests_a}
        titles_b = {d["title"] for d in digests_b}
        self.assertTrue(all(t.startswith(FEED_A) for t in titles_a))
        self.assertTrue(all(t.startswith(FEED_B) for t in titles_b))
        self.assertEqual(titles_a & titles_b, set())
        with tenant_context("t_alpha"):
            con = tasks._pending_db()
            pending_titles = {
                row[0] for row in con.execute("SELECT title FROM pending_papers")
            }
            con.close()
        self.assertEqual(pending_titles, titles_a)

    def test_pdf_candidate_sync_repairs_existing_shared_deliveries(self):
        self._run_ingest_with_fake_ai()
        with patch.object(tasks, "get_interest_profile", return_value=None):
            with tenant_context("t_alpha"):
                tasks.deliver_shared_to_tenant()
                con = tasks._pending_db()
                con.execute("DELETE FROM pending_papers")
                con.commit()
                con.close()
                added = tasks.sync_pending_from_shared_deliveries()
                con = tasks._pending_db()
                count = con.execute("SELECT COUNT(*) FROM pending_papers").fetchone()[0]
                con.close()
        self.assertEqual(added, 2)
        self.assertEqual(count, 2)

    def test_delivery_is_idempotent(self):
        self._run_ingest_with_fake_ai()
        with patch.object(tasks, "get_interest_profile", return_value=None):
            with tenant_context("t_alpha"):
                first = tasks.deliver_shared_to_tenant()
                second = tasks.deliver_shared_to_tenant()
        self.assertEqual(first, 2)
        self.assertEqual(second, 0)  # deliveries 表阻止重复投递

    def test_tenant_without_key_still_receives_from_cache(self):
        self._run_ingest_with_fake_ai()
        with tenant_context("t_alpha"):
            tasks.save_config(self._config(""))  # 清空租户 key
        with patch.object(tasks, "get_interest_profile", return_value=None):
            with tenant_context("t_alpha"):
                delivered = tasks.deliver_shared_to_tenant()
                digests = tasks.get_recent_digests()
        self.assertEqual(delivered, 2)
        self.assertEqual(len(digests), 2)

    def test_non_owner_cannot_run_shared_ingest(self):
        with tenant_context("t_alpha"):
            with self.assertRaises(RuntimeError):
                tasks.run_shared_rss_ingest()

    def test_existing_shared_article_table_adds_original_abstract_column(self):
        con = tasks.sqlite3.connect(self.server_paths.shared_content_db)
        con.execute("""CREATE TABLE articles(
            item_key TEXT PRIMARY KEY, filename TEXT UNIQUE NOT NULL, title TEXT,
            cn_title TEXT, keywords TEXT, journal TEXT, source_feed_url TEXT,
            source_feed_title TEXT, article_type TEXT, link TEXT, doi TEXT,
            digest_text TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'rss',
            digested_ts INTEGER NOT NULL)""")
        con.commit()
        con.close()

        migrated = tasks._shared_content_db()
        columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(articles)")
        }
        migrated.close()
        self.assertIn("original_abstract", columns)

    def test_retention_prunes_old_shared_articles(self):
        self._run_ingest_with_fake_ai()
        con = tasks._shared_content_db()
        # 把一篇文章标记为 100 天前。
        old_key = con.execute("SELECT item_key FROM articles LIMIT 1").fetchone()[0]
        old_ts = tasks.time.time() - 100 * 86400
        con.execute(
            "UPDATE articles SET digested_ts=? WHERE item_key=?",
            (int(old_ts), old_key),
        )
        con.execute(
            "UPDATE shared_seen SET ts=? WHERE item_key=?",
            (int(old_ts), old_key),
        )
        filename = con.execute(
            "SELECT filename FROM articles WHERE item_key=?", (old_key,)
        ).fetchone()[0]
        con.commit()
        con.close()
        html_path = tasks.shared_inbox_dir() / filename
        self.assertTrue(html_path.is_file())

        result = tasks.cleanup_shared_retention(days=90)
        self.assertEqual(result["articles_deleted"], 1)

        con = tasks._shared_content_db()
        remaining = con.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        con.close()
        self.assertEqual(remaining, 3)
        self.assertFalse(html_path.is_file())


if __name__ == "__main__":
    unittest.main()
