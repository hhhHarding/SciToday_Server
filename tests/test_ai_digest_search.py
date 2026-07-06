import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import tasks
from ratelimit import category_for_endpoint


class AiDigestSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "digests.db"
        self.db_patch = patch.object(tasks, "DIGEST_DB", self.db_path)
        self.db_patch.start()
        tasks._reset_migration_cache_for_tests()

    def tearDown(self):
        self.db_patch.stop()
        tasks._reset_migration_cache_for_tests()
        self.temp_dir.cleanup()

    @staticmethod
    def _digest(filename, *, title="", cn_title="", keywords="", journal="",
                preview="", source="rss", created_ts=1):
        return {
            "filename": filename,
            "timestamp": "",
            "title": title,
            "cn_title": cn_title,
            "keywords": keywords,
            "journal": journal,
            "source": source,
            "preview": preview,
            "created_ts": created_ts,
        }

    def _seed(self, *digests):
        con = tasks._digest_db()
        for digest in digests:
            tasks._upsert_digest(con, digest)
        con.commit()
        con.close()

    def _search(self, query, limit=tasks.AI_SEARCH_CANDIDATE_LIMIT):
        with patch.object(tasks, "_sync_digest_index"):
            return tasks.search_digest_candidates(query, limit)

    def test_migration_creates_fts_and_triggers_keep_it_in_sync(self):
        self._seed(self._digest("one.html", title="Initial marker"))
        con = tasks._digest_db()
        self.assertEqual(
            1,
            con.execute(
                "SELECT COUNT(*) FROM digests_fts WHERE digests_fts MATCH 'Initial'"
            ).fetchone()[0],
        )
        con.execute("UPDATE digests SET title='Updated marker' WHERE filename='one.html'")
        con.commit()
        self.assertEqual(
            1,
            con.execute(
                "SELECT COUNT(*) FROM digests_fts WHERE digests_fts MATCH 'Updated'"
            ).fetchone()[0],
        )
        con.execute("DELETE FROM digests WHERE filename='one.html'")
        con.commit()
        self.assertEqual(
            0,
            con.execute(
                "SELECT COUNT(*) FROM digests_fts WHERE digests_fts MATCH 'Updated'"
            ).fetchone()[0],
        )
        con.close()

    def test_coarse_search_supports_chinese_weights_and_visibility_filters(self):
        self._seed(
            self._digest(
                "title.html",
                title="Single cell atlas",
                cn_title="阿尔茨海默病小胶质细胞图谱",
                created_ts=1,
            ),
            self._digest(
                "preview.html",
                title="Unrelated title",
                preview="A single cell workflow for microglia",
                created_ts=3,
            ),
            self._digest(
                "pdf.html",
                title="Single cell PDF",
                source="pdf",
                created_ts=4,
            ),
            self._digest(
                "deleted.html",
                title="Single cell deleted",
                created_ts=5,
            ),
        )
        con = tasks._digest_db()
        con.execute("UPDATE digests SET deleted=1 WHERE filename='deleted.html'")
        con.commit()
        con.close()

        english = self._search("single cell")
        chinese = self._search("阿尔茨海默病小胶质细胞")

        self.assertEqual(["title.html", "preview.html"], [item["filename"] for item in english])
        self.assertEqual("title.html", chinese[0]["filename"])
        self.assertNotIn("pdf.html", {item["filename"] for item in english})
        self.assertNotIn("deleted.html", {item["filename"] for item in english})

    def test_candidate_limit_is_capped_at_one_hundred(self):
        self._seed(*[
            self._digest(
                f"{index:03d}.html",
                title=f"single cell candidate {index}",
                created_ts=index,
            )
            for index in range(105)
        ])
        self.assertEqual(100, len(self._search("single cell", limit=1000)))

    def test_ai_rerank_preserves_valid_order_and_caps_results(self):
        candidates = [
            self._digest(f"{index:02d}.html", title=f"Paper {index}")
            for index in range(35)
        ]
        matches = (
            [{"filename": "missing.html"}, {"filename": "02.html"}, {"filename": "02.html"}]
            + [{"filename": f"{index:02d}.html"} for index in range(34, -1, -1)]
        )
        with patch.object(tasks, "search_digest_candidates", return_value=candidates), patch.object(
            tasks, "_ai_call", return_value=json.dumps({"matches": matches})
        ):
            result = tasks.ai_search_digests("query")

        self.assertTrue(result["ai_ranked"])
        self.assertEqual(30, len(result["items"]))
        self.assertEqual("02.html", result["items"][0]["filename"])
        self.assertEqual("34.html", result["items"][1]["filename"])

    def test_invalid_json_retries_once_then_succeeds(self):
        candidates = [self._digest("one.html", title="Paper")]
        ai_call = Mock(side_effect=[
            "not-json",
            '{"matches":[{"filename":"one.html"}]}',
        ])
        with patch.object(tasks, "search_digest_candidates", return_value=candidates), patch.object(
            tasks, "_ai_call", ai_call
        ):
            result = tasks.ai_search_digests("query")
        self.assertEqual(2, ai_call.call_count)
        self.assertEqual("one.html", result["items"][0]["filename"])

    def test_invalid_json_twice_returns_failure(self):
        candidates = [self._digest("one.html", title="Paper")]
        ai_call = Mock(return_value="not-json")
        with patch.object(tasks, "search_digest_candidates", return_value=candidates), patch.object(
            tasks, "_ai_call", ai_call
        ):
            with self.assertRaises(tasks.AiSearchFailedError):
                tasks.ai_search_digests("query")
        self.assertEqual(2, ai_call.call_count)

    def test_empty_candidates_skip_ai_and_endpoint_is_ai_rate_limited(self):
        ai_call = Mock()
        with patch.object(tasks, "search_digest_candidates", return_value=[]), patch.object(
            tasks, "_ai_call", ai_call
        ):
            result = tasks.ai_search_digests("query")
        ai_call.assert_not_called()
        self.assertEqual([], result["items"])
        self.assertEqual("ai", category_for_endpoint("api_ai_search_digests"))


if __name__ == "__main__":
    unittest.main()
