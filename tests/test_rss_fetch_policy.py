import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import tasks
from server_config import ServerPaths
from tenancy.context import tenant_context
from tenancy.paths import TenantPaths
from tenancy.registry import TenantRegistry


FEED_A = "https://feeds.example/a.xml"
FEED_B = "https://feeds.example/b.xml"


class _Response:
    def __init__(self, status, headers=None, content=b""):
        self.status_code = status
        self.headers = dict(headers or {})
        self.content = content
        self.url = FEED_A
        self.closed = False

    def close(self):
        self.closed = True


class RssFetchPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = ServerPaths(Path(self.temp_dir.name) / "server")
        self.paths.ensure_global_directories()
        self.registry = TenantRegistry(self.paths)
        self.registry.ensure_owner(
            default_config={
                "rss": {"lookback_days": 7, "per_feed_limit": 3},
                "schedule": {"rss_discovery_interval_minutes": 60},
            }
        )
        self._set_owner_feeds([FEED_A, FEED_B])
        self.paths_patch = patch.object(tasks, "SERVER_PATHS", self.paths)
        self.paths_patch.start()
        tasks._reset_config_cache_for_tests()
        tasks._reset_migration_cache_for_tests()

    def tearDown(self):
        self.paths_patch.stop()
        tasks._reset_config_cache_for_tests()
        tasks._reset_migration_cache_for_tests()
        self.temp_dir.cleanup()

    def _set_owner_feeds(self, urls):
        opml = TenantPaths(self.paths.data_root, "owner").opml
        outlines = "".join(
            f'<outline xmlUrl="{url}" text="{url}" />' for url in urls
        )
        opml.write_text(
            f'<opml version="2.0"><body>{outlines}</body></opml>',
            encoding="utf-8",
        )

    def _state_row(self, url=FEED_A):
        con = tasks._shared_content_db()
        con.row_factory = tasks.sqlite3.Row
        row = con.execute(
            "SELECT * FROM feed_fetch_state WHERE feed_url=?",
            (url,),
        ).fetchone()
        con.close()
        return row

    def test_conditional_headers_and_304_are_success(self):
        response = _Response(
            304,
            headers={"Cache-Control": "max-age=1800"},
        )
        with patch.object(tasks, "http_get", return_value=response) as get:
            result = tasks._fetch_single_feed(
                {"title": "A", "url": FEED_A},
                3,
                state={"etag": '"abc"', "last_modified": "yesterday"},
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.category, "not_modified")
        self.assertEqual(result.entries, [])
        self.assertTrue(response.closed)
        headers = get.call_args.kwargs["headers"]
        self.assertEqual(headers["If-None-Match"], '"abc"')
        self.assertEqual(headers["If-Modified-Since"], "yesterday")
        self.assertEqual(
            tasks.RSS_HEADERS["User-Agent"], tasks.RSS_DEFAULT_USER_AGENT
        )
        self.assertIn("Mozilla/5.0", tasks.RSS_HEADERS["User-Agent"])
        self.assertNotIn("Connection", tasks.RSS_HEADERS)

    def test_http_403_is_not_retried(self):
        response = _Response(403)
        with patch.object(
            tasks,
            "_request_pinned_feed_url",
            return_value=response,
        ) as request:
            result = tasks.http_get(
                FEED_A,
                max_attempts=2,
                resolver=lambda _host: ["8.8.8.8"],
            )
        self.assertIs(result, response)
        self.assertEqual(request.call_count, 1)

    def test_access_denied_and_second_host_failure_open_circuit(self):
        with tenant_context("owner"):
            tasks.sync_shared_feed_fetch_state(
                {FEED_A: "A", FEED_B: "B"},
                now=1000,
            )
            first = tasks.FeedFetchResult(
                feed={"title": "A", "url": FEED_A},
                category="access_denied",
                error="HTTP 403",
                http_status=403,
            )
            tasks._record_shared_fetch_result(first, now=1000)
            row = self._state_row(FEED_A)
            self.assertEqual(row["blocked_until_ts"], 1000 + 60 * 60)
            self.assertEqual(row["consecutive_failures"], 1)

            second = tasks.FeedFetchResult(
                feed={"title": "B", "url": FEED_B},
                category="access_denied",
                error="HTTP 403",
                http_status=403,
            )
            tasks._record_shared_fetch_result(second, now=5000)
            con = tasks._shared_content_db()
            host = con.execute(
                "SELECT blocked_until_ts FROM host_fetch_state WHERE host='feeds.example'"
            ).fetchone()
            con.close()
            self.assertEqual(host[0], 5000 + 2 * 60 * 60)

    def test_failure_categories_have_distinct_retry_and_disable_policies(self):
        delay, disabled = tasks._feed_failure_policy(
            "rate_limited",
            1,
            retry_after=8 * 60 * 60,
        )
        self.assertEqual(delay, 8 * 60 * 60)
        self.assertFalse(disabled)
        self.assertEqual(
            tasks._feed_failure_policy("not_found", 3),
            (4 * 24 * 60 * 60, True),
        )
        self.assertEqual(
            tasks._feed_failure_policy("gone", 1),
            (7 * 24 * 60 * 60, True),
        )
        self.assertEqual(
            tasks._feed_failure_policy("server_error", 1),
            (15 * 60, False),
        )
        self.assertEqual(
            tasks._feed_failure_policy("unsafe_url", 1),
            (7 * 24 * 60 * 60, True),
        )
        self.assertEqual(
            tasks._fetch_exception_category(requests.Timeout("slow")),
            "network_error",
        )
        self.assertEqual(
            tasks._fetch_exception_category(requests.exceptions.SSLError("tls")),
            "tls_error",
        )

    def test_success_interval_uses_hint_then_doubles_when_unchanged(self):
        with tenant_context("owner"):
            tasks.sync_shared_feed_fetch_state({FEED_A: "A"}, now=1000)
            success = tasks.FeedFetchResult(
                feed={"title": "A", "url": FEED_A},
                category="ok",
                http_status=200,
                cache_hint_seconds=15 * 60,
                etag='"new"',
            )
            with patch.object(tasks.random, "uniform", return_value=1.0), patch.object(
                tasks.random, "randint", return_value=5
            ):
                tasks._record_shared_fetch_result(
                    success,
                    new_count=1,
                    now=1000,
                )
                row = self._state_row()
                self.assertEqual(row["effective_interval_seconds"], 15 * 60)
                self.assertEqual(row["next_fetch_ts"], 1000 + 15 * 60)

                unchanged = tasks.FeedFetchResult(
                    feed={"title": "A", "url": FEED_A},
                    category="not_modified",
                    http_status=304,
                    cache_hint_seconds=15 * 60,
                )
                tasks._record_shared_fetch_result(unchanged, now=2000)
                row = self._state_row()
                self.assertEqual(row["effective_interval_seconds"], 30 * 60)
                self.assertEqual(row["next_fetch_ts"], 2000 + 30 * 60)
                self.assertEqual(row["etag"], '"new"')

    def test_legacy_403_is_seeded_with_one_hour_cooldown(self):
        with tenant_context("owner"):
            tasks.record_feed_health(
                {"title": "A", "url": FEED_A},
                ok=False,
                error="403 Client Error",
            )
            tasks.sync_shared_feed_fetch_state({FEED_A: "A"}, now=1000)
            row = self._state_row()
            self.assertEqual(row["error_category"], "access_denied")
            self.assertEqual(row["blocked_until_ts"], 1000 + 60 * 60)

    def test_host_worker_stops_after_access_denied(self):
        states = [
            {"feed_url": FEED_A, "title": "A"},
            {"feed_url": FEED_B, "title": "B"},
        ]
        denied = tasks.FeedFetchResult(
            feed={"title": "A", "url": FEED_A},
            category="access_denied",
            error="HTTP 403",
            http_status=403,
        )
        session = Mock()
        with patch.object(tasks, "_make_pinned_feed_session", return_value=session), patch.object(
            tasks, "_fetch_single_feed", return_value=denied
        ) as fetch, patch.object(tasks.time, "sleep") as sleep:
            results = tasks._fetch_host_group("feeds.example", states, 3, 0)
        self.assertEqual(len(results), 1)
        self.assertEqual(fetch.call_count, 1)
        sleep.assert_not_called()
        session.close.assert_called_once()

    def test_operator_probe_is_single_feed_and_rate_limited_per_host(self):
        success = tasks.FeedFetchResult(
            feed={"title": FEED_A, "url": FEED_A},
            category="not_modified",
            http_status=304,
        )
        with tenant_context("owner"), patch.object(
            tasks,
            "_fetch_host_group",
            return_value=[success],
        ) as fetch:
            first = tasks.probe_shared_rss_feed(
                FEED_A,
                override_cooldown=True,
                now=1000,
            )
            second = tasks.probe_shared_rss_feed(
                FEED_A,
                override_cooldown=True,
                now=1001,
            )
            missing = tasks.probe_shared_rss_feed(
                "https://other.example/feed",
                override_cooldown=True,
                now=5000,
            )
        self.assertTrue(first["ok"])
        self.assertEqual(first["upstream_status"], 304)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(second["status_code"], 429)
        self.assertEqual(second["error"], "probe_rate_limited")
        self.assertEqual(missing["status_code"], 400)
        self.assertEqual(missing["error"], "not_subscribed")


if __name__ == "__main__":
    unittest.main()
