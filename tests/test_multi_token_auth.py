import io
import json
import logging
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import app as backend
import tasks
from job_coordinator import TaskCoordinator
from server_config import ServerPaths
from tenancy.context import OWNER_TENANT_ID, get_current_tenant_id, tenant_context
from tenancy.models import TenantStatus
from tenancy.registry import TenantRegistry


if "test_auth_boom" not in backend.app.view_functions:
    @backend.app.route("/api/test-auth-boom")
    def test_auth_boom():
        raise RuntimeError("injected route failure")


class MultiTokenAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = ServerPaths(Path(self.temp_dir.name) / "server")
        self.registry = TenantRegistry(self.paths)
        self.registry.create_tenant("Owner", tenant_id="owner")
        self.registry.create_tenant("Alpha", tenant_id="t_alpha")
        self.registry.create_tenant("Beta", tenant_id="t_beta")
        self.tasks_paths_patch = patch.object(tasks, "SERVER_PATHS", self.paths)
        self.tasks_paths_patch.start()
        tasks._reset_config_cache_for_tests()
        tasks._reset_migration_cache_for_tests()
        tasks._digest_index_sentinels.clear()
        tasks._digest_index_locks.clear()
        self.alpha = self.registry.create_token(
            "t_alpha",
            scopes=("app", "tenant_admin"),
        )
        self.beta = self.registry.create_token("t_beta", scopes=("app",))
        self.beta_ai_writer = self.registry.create_token(
            "t_beta",
            scopes=("app", "ai_config_write"),
        )
        self.coordinator = TaskCoordinator(
            self.registry,
            max_workers=2,
            max_pending=4,
            scan_interval=30,
        )
        self.coordinator.start(start_scanner=False)
        backend.app.config.update(
            TESTING=True,
            TENANT_REGISTRY=self.registry,
            TASK_COORDINATOR=self.coordinator,
            OPERATOR_TOKEN="operator-test-token",
            BIND_HOST="127.0.0.1",
        )
        self.client = backend.app.test_client()

    def tearDown(self):
        self.coordinator.shutdown(wait=True)
        backend.app.config.pop("TENANT_REGISTRY", None)
        backend.app.config.pop("TASK_COORDINATOR", None)
        backend.app.config.pop("OPERATOR_TOKEN", None)
        backend.app.config.pop("BIND_HOST", None)
        self.tasks_paths_patch.stop()
        tasks._reset_config_cache_for_tests()
        tasks._reset_migration_cache_for_tests()
        tasks._digest_index_sentinels.clear()
        tasks._digest_index_locks.clear()
        self.temp_dir.cleanup()

    @staticmethod
    def _headers(token):
        return {"Authorization": f"Bearer {token}"}

    def test_public_whitelist_and_default_deny(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        admin_response = self.client.get("/admin/")
        self.assertEqual(admin_response.status_code, 200)
        admin_response.close()
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.client.get("/not-a-public-route").status_code, 401)

    def test_only_bearer_header_is_accepted(self):
        token = self.alpha.token
        self.assertEqual(
            self.client.get(f"/api/auth/me?token={token}").status_code,
            401,
        )
        self.assertEqual(
            self.client.get(
                "/api/auth/me",
                headers={"Cookie": f"rssai_admin_token={token}"},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get(
                "/api/auth/me",
                headers=self._headers(token),
            ).status_code,
            200,
        )

    def test_tokens_map_to_exact_tenant_and_context_resets(self):
        alpha = self.client.get(
            "/api/auth/me",
            headers=self._headers(self.alpha.token),
        ).get_json()
        self.assertEqual(get_current_tenant_id(), OWNER_TENANT_ID)
        beta = self.client.get(
            "/api/auth/me",
            headers=self._headers(self.beta.token),
        ).get_json()

        self.assertEqual(alpha["tenant_id"], "t_alpha")
        self.assertEqual(beta["tenant_id"], "t_beta")
        self.assertNotEqual(alpha["tenant_id"], beta["tenant_id"])
        self.assertEqual(get_current_tenant_id(), OWNER_TENANT_ID)

    def test_revoked_expired_and_suspended_tenants_return_same_401(self):
        revoked = self.registry.create_token("t_alpha")
        expired = self.registry.create_token(
            "t_alpha",
            now=100,
            expires_at=101,
        )
        suspended = self.registry.create_token("t_beta")
        self.registry.revoke_token(revoked.record.id)
        self.registry.set_tenant_status("t_beta", TenantStatus.SUSPENDED)

        for token in ("forged", revoked.token, expired.token, suspended.token):
            with self.subTest(token_type=token[:10]):
                response = self.client.get(
                    "/api/auth/me",
                    headers=self._headers(token),
                )
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.get_json(), {"error": "unauthorized"})

    def test_exception_path_resets_context(self):
        with self.assertRaises(RuntimeError):
            self.client.get(
                "/api/test-auth-boom",
                headers=self._headers(self.alpha.token),
            )
        self.assertEqual(get_current_tenant_id(), OWNER_TENANT_ID)

    def test_concurrent_requests_do_not_exchange_tenants(self):
        def request_many(tenant_id, token):
            client = backend.app.test_client()
            observed = []
            for _ in range(20):
                response = client.get(
                    "/api/auth/me",
                    headers=self._headers(token),
                )
                observed.append(response.get_json()["tenant_id"])
                if get_current_tenant_id() != OWNER_TENANT_ID:
                    return ["context-leak"]
            return observed

        with ThreadPoolExecutor(max_workers=2) as pool:
            alpha_future = pool.submit(request_many, "t_alpha", self.alpha.token)
            beta_future = pool.submit(request_many, "t_beta", self.beta.token)
            alpha_values = alpha_future.result()
            beta_values = beta_future.result()

        self.assertEqual(set(alpha_values), {"t_alpha"})
        self.assertEqual(set(beta_values), {"t_beta"})

    def test_tenant_cannot_access_operator_route(self):
        response = self.client.get(
            "/api/admin/local-settings",
            headers=self._headers(self.alpha.token),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json(), {"error": "forbidden"})
        runtime_response = self.client.post(
            "/api/admin/runtime/restart_backend",
            headers=self._headers(self.alpha.token),
        )
        self.assertEqual(runtime_response.status_code, 403)

    def test_ai_config_writer_can_only_update_own_ai_fields(self):
        secret = "beta-private-ai-key"
        response = self.client.patch(
            "/api/ai-config",
            headers=self._headers(self.beta_ai_writer.token),
            json={
                "api_key": secret,
                "base_url": "https://1.1.1.1/v1/",
                "model": "beta-model",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["ai"]["api_key"],
            tasks.MASKED_SECRET,
        )
        self.assertNotIn(secret, response.get_data(as_text=True))

        with tenant_context("t_beta"):
            beta_ai = tasks.load_config()["ai"]
        with tenant_context("t_alpha"):
            alpha_ai = tasks.load_config().get("ai") or {}
        self.assertEqual(beta_ai["api_key"], secret)
        self.assertEqual(beta_ai["base_url"], "https://1.1.1.1/v1")
        self.assertEqual(beta_ai["model"], "beta-model")
        self.assertNotEqual(alpha_ai.get("api_key"), secret)

        legacy = self.client.post(
            "/api/config",
            headers=self._headers(self.beta_ai_writer.token),
            json={
                "ai": {
                    "api_key": tasks.MASKED_SECRET,
                    "base_url": "https://8.8.8.8/v1",
                    "model": "legacy-model",
                    "system_prompt": "beta-system-prompt",
                    "rss_prompt": "beta-rss-prompt",
                    "pdf_prompt": "beta-pdf-prompt",
                },
            },
        )
        self.assertEqual(legacy.status_code, 200)
        self.assertTrue(legacy.get_json()["compatibility_mode"])
        with tenant_context("t_beta"):
            legacy_ai = tasks.load_config()["ai"]
        self.assertEqual(legacy_ai["model"], "legacy-model")
        self.assertEqual(legacy_ai["system_prompt"], "beta-system-prompt")
        self.assertEqual(legacy_ai["rss_prompt"], "beta-rss-prompt")
        self.assertEqual(legacy_ai["pdf_prompt"], "beta-pdf-prompt")
        with tenant_context("t_alpha"):
            alpha_ai = tasks.load_config().get("ai") or {}
        self.assertNotEqual(alpha_ai.get("system_prompt"), "beta-system-prompt")

        prompts_only = self.client.post(
            "/api/config",
            headers=self._headers(self.beta_ai_writer.token),
            json={"ai": {"system_prompt": "updated-private-prompt"}},
        )
        full_config = self.client.post(
            "/api/config",
            headers=self._headers(self.beta_ai_writer.token),
            json={"rss": {"per_feed_limit": 99}},
        )
        self.assertEqual(prompts_only.status_code, 200)
        self.assertEqual(full_config.status_code, 403)
        with tenant_context("t_beta"):
            self.assertEqual(
                tasks.load_config()["ai"]["system_prompt"],
                "updated-private-prompt",
            )

    def test_app_only_token_cannot_write_ai_config(self):
        read_response = self.client.get(
            "/api/ai-config",
            headers=self._headers(self.beta.token),
        )
        write_response = self.client.patch(
            "/api/ai-config",
            headers=self._headers(self.beta.token),
            json={"model": "forbidden-model"},
        )
        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(write_response.status_code, 403)
        self.assertEqual(
            write_response.get_json()["required_scope"],
            "ai_config_write",
        )

    def test_chat_api_passes_compressed_history_context(self):
        expected = {
            "reply": "回答",
            "history_summary": "更新后的摘要",
            "context_compressed": True,
        }
        with patch.object(tasks, "ai_chat", return_value=expected) as chat:
            response = self.client.post(
                "/api/chat",
                headers=self._headers(self.beta.token),
                json={
                    "filename": "paper.html",
                    "message": "当前问题",
                    "history": [{"role": "assistant", "content": "最近回答"}],
                    "history_summary": "此前摘要",
                    "web_search": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), expected)
        chat.assert_called_once_with(
            "paper.html",
            "当前问题",
            [{"role": "assistant", "content": "最近回答"}],
            web_search=False,
            history_summary="此前摘要",
        )

        invalid = self.client.post(
            "/api/chat",
            headers=self._headers(self.beta.token),
            json={
                "filename": "paper.html",
                "message": "当前问题",
                "history": "not-a-list",
            },
        )
        self.assertEqual(invalid.status_code, 400)

    def test_ai_config_test_uses_form_values_without_saving_them(self):
        with tenant_context("t_beta"):
            before = dict(tasks.load_config().get("ai") or {})

        with patch.object(tasks, "test_ai_connection") as test_connection:
            response = self.client.post(
                "/api/ai-config/test",
                headers=self._headers(self.beta_ai_writer.token),
                json={
                    "api_key": "temporary-key",
                    "base_url": "https://8.8.8.8/api/v3",
                    "model": "temporary-model",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"ok": True, "message": "AI API 测试成功"},
        )
        test_connection.assert_called_once_with(
            "temporary-key",
            "https://8.8.8.8/api/v3",
            "temporary-model",
        )
        with tenant_context("t_beta"):
            self.assertEqual(tasks.load_config().get("ai") or {}, before)

        forbidden = self.client.post(
            "/api/ai-config/test",
            headers=self._headers(self.beta.token),
            json={
                "api_key": "temporary-key",
                "base_url": "https://8.8.8.8/v1",
                "model": "temporary-model",
            },
        )
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(
            forbidden.get_json()["required_scope"],
            "ai_config_write",
        )

    def test_app_only_token_can_update_own_schedule_and_recommendation(self):
        schedule = self.client.patch(
            "/api/settings/schedule",
            headers=self._headers(self.beta.token),
            json={
                "rss_interval_minutes": 45,
                "pdf_interval_minutes": 15,
                "enabled": False,
            },
        )
        recommendation = self.client.patch(
            "/api/settings/recommendation",
            headers=self._headers(self.beta.token),
            json={"interest_score_threshold": 82.5},
        )

        self.assertEqual(schedule.status_code, 200)
        self.assertEqual(recommendation.status_code, 200)
        with tenant_context("t_beta"):
            beta_config = tasks.load_config()
        with tenant_context("t_alpha"):
            alpha_config = tasks.load_config()
        self.assertEqual(beta_config["schedule"]["rss_interval_minutes"], 45)
        self.assertEqual(beta_config["schedule"]["pdf_interval_minutes"], 15)
        self.assertFalse(beta_config["schedule"]["enabled"])
        self.assertEqual(beta_config["rss"]["interest_score_threshold"], 82.5)
        self.assertNotEqual(
            (alpha_config.get("schedule") or {}).get("rss_interval_minutes"),
            beta_config["schedule"]["rss_interval_minutes"],
        )

    def test_legacy_app_config_endpoint_allows_only_app_settings(self):
        schedule = self.client.post(
            "/api/config",
            headers=self._headers(self.beta.token),
            json={
                "schedule": {
                    "rss_interval_minutes": 55,
                    "pdf_interval_minutes": 20,
                    "enabled": True,
                },
            },
        )
        recommendation = self.client.post(
            "/api/config",
            headers=self._headers(self.beta.token),
            json={"rss": {"interest_score_threshold": 76}},
        )
        forbidden = self.client.post(
            "/api/config",
            headers=self._headers(self.beta.token),
            json={"rss": {"per_feed_limit": 99}},
        )

        self.assertEqual(schedule.status_code, 200)
        self.assertTrue(schedule.get_json()["compatibility_mode"])
        self.assertEqual(recommendation.status_code, 200)
        self.assertEqual(forbidden.status_code, 403)
        with tenant_context("t_beta"):
            config = tasks.load_config()
        self.assertEqual(config["schedule"]["rss_interval_minutes"], 55)
        self.assertEqual(config["rss"]["interest_score_threshold"], 76)

    def test_app_settings_endpoints_reject_invalid_or_extra_fields(self):
        bad_schedule = self.client.patch(
            "/api/settings/schedule",
            headers=self._headers(self.beta.token),
            json={"enabled": "false"},
        )
        extra_recommendation = self.client.patch(
            "/api/settings/recommendation",
            headers=self._headers(self.beta.token),
            json={"interest_score_threshold": 80, "per_feed_limit": 100},
        )
        out_of_range = self.client.patch(
            "/api/settings/recommendation",
            headers=self._headers(self.beta.token),
            json={"interest_score_threshold": 101},
        )

        self.assertEqual(bad_schedule.status_code, 400)
        self.assertEqual(extra_recommendation.status_code, 400)
        self.assertEqual(out_of_range.status_code, 400)

    def test_feed_delete_query_preserves_complete_url(self):
        feed_url = (
            "https://example.test/feed.xml?"
            "journal=earth/science&edition=(online)"
        )
        added = self.client.post(
            "/api/feeds",
            headers=self._headers(self.beta.token),
            json={"title": "", "url": feed_url},
        )
        deleted = self.client.delete(
            "/api/feeds",
            headers=self._headers(self.beta.token),
            query_string={"url": feed_url},
        )

        self.assertEqual(added.status_code, 200)
        self.assertEqual(deleted.status_code, 200)
        with tenant_context("t_beta"):
            feeds = tasks.parse_opml(tasks.get_opml_path(tasks.load_config()))
        self.assertNotIn(feed_url, {feed["url"] for feed in feeds})

    def test_operator_requires_local_direct_connection(self):
        local = self.client.get(
            "/api/auth/me",
            headers=self._headers("operator-test-token"),
            base_url="http://localhost:5200",
        )
        proxied = self.client.get(
            "/api/auth/me",
            headers={
                **self._headers("operator-test-token"),
                "CF-Connecting-IP": "203.0.113.10",
            },
            base_url="http://localhost:5200",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )

        self.assertEqual(local.status_code, 200)
        self.assertEqual(local.get_json()["kind"], "operator")
        self.assertEqual(proxied.status_code, 403)

    def test_insecure_dev_mode_requires_loopback_bind_and_request(self):
        with patch.dict("os.environ", {"RSSAI_INSECURE_DEV_MODE": "1"}):
            allowed = self.client.get(
                "/api/auth/me",
                base_url="http://localhost:5200",
            )
            backend.app.config["BIND_HOST"] = "0.0.0.0"
            denied = self.client.get(
                "/api/auth/me",
                base_url="http://localhost:5200",
            )

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.get_json()["kind"], "developer")
        self.assertEqual(denied.status_code, 401)

    def test_authentication_does_not_log_token(self):
        secret = "this-token-must-not-appear"
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        backend.logger.addHandler(handler)
        try:
            self.client.get(
                "/api/auth/me",
                headers=self._headers(secret),
            )
        finally:
            backend.logger.removeHandler(handler)

        self.assertNotIn(secret, stream.getvalue())

    def test_api_inbox_and_status_are_tenant_isolated(self):
        for tenant_id, marker in (("t_alpha", "alpha"), ("t_beta", "beta")):
            with tenant_context(tenant_id):
                paths = tasks.current_tenant_paths()
                tasks.save_config({"marker": marker})
                paths.inbox_dir.mkdir(parents=True, exist_ok=True)
                (paths.inbox_dir / "same.html").write_text(
                    '<meta name="digest-source" content="rss">'
                    f"<title>{marker}</title>"
                    f'<div class="content">{marker}-secret</div>',
                    encoding="utf-8",
                )
                tasks._sync_digest_index(force=True)

        alpha_inbox = self.client.get(
            "/inbox/same.html",
            headers=self._headers(self.alpha.token),
        )
        beta_inbox = self.client.get(
            "/inbox/same.html",
            headers=self._headers(self.beta.token),
        )
        alpha_digests = self.client.get(
            "/api/digests",
            headers=self._headers(self.alpha.token),
        ).get_json()
        beta_digests = self.client.get(
            "/api/digests",
            headers=self._headers(self.beta.token),
        ).get_json()
        overview = self.client.get(
            "/api/admin/overview",
            headers=self._headers(self.alpha.token),
        )

        self.assertIn(b"alpha-secret", alpha_inbox.data)
        self.assertNotIn(b"beta-secret", alpha_inbox.data)
        self.assertIn(b"beta-secret", beta_inbox.data)
        self.assertNotIn(b"alpha-secret", beta_inbox.data)
        self.assertEqual(alpha_digests[0]["filename"], "same.html")
        self.assertEqual(beta_digests[0]["filename"], "same.html")
        self.assertEqual(overview.status_code, 200)
        self.assertNotIn(
            str(self.paths.data_root),
            json.dumps(overview.get_json(), ensure_ascii=False),
        )
        alpha_inbox.close()
        beta_inbox.close()

    def test_manual_task_thread_keeps_tenant_and_progress_is_private(self):
        finished = threading.Event()
        observed = []

        def fake_pdf_task(progress_callback=None):
            observed.append(get_current_tenant_id())
            if progress_callback:
                progress_callback(1, 1, "alpha-only-progress")
            finished.set()
            return 1

        with patch.object(tasks, "run_pdf_watch", side_effect=fake_pdf_task):
            started = self.client.post(
                "/api/run/pdf",
                headers=self._headers(self.alpha.token),
            )
            self.assertEqual(started.status_code, 202)
            self.assertTrue(finished.wait(5))

        alpha_progress = self.client.get(
            "/api/progress",
            headers=self._headers(self.alpha.token),
        ).get_json()
        beta_progress = self.client.get(
            "/api/progress",
            headers=self._headers(self.beta.token),
        ).get_json()

        self.assertEqual(observed, ["t_alpha"])
        self.assertEqual(alpha_progress["pdf"]["message"], "alpha-only-progress")
        self.assertEqual(beta_progress["pdf"]["message"], "")


if __name__ == "__main__":
    unittest.main()
