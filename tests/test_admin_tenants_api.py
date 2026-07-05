import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as backend
import tasks
from job_coordinator import TaskCoordinator
from server_config import ServerPaths
from tenancy.registry import TenantRegistry


class AdminTenantsApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = ServerPaths(Path(self.temp_dir.name) / "server")
        self.registry = TenantRegistry(self.paths)
        self.registry.create_tenant("Owner", tenant_id="owner")
        self.registry.create_tenant("Alpha", tenant_id="t_alpha")
        self.tasks_paths_patch = patch.object(tasks, "SERVER_PATHS", self.paths)
        self.tasks_paths_patch.start()
        tasks._reset_config_cache_for_tests()
        tasks._reset_migration_cache_for_tests()
        self.tenant_token = self.registry.create_token(
            "t_alpha", scopes=("app", "tenant_admin"),
        )
        self.coordinator = TaskCoordinator(
            self.registry, max_workers=2, max_pending=4, scan_interval=30,
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
        for key in ("TENANT_REGISTRY", "TASK_COORDINATOR", "OPERATOR_TOKEN", "BIND_HOST"):
            backend.app.config.pop(key, None)
        self.tasks_paths_patch.stop()
        tasks._reset_config_cache_for_tests()
        tasks._reset_migration_cache_for_tests()
        self.temp_dir.cleanup()

    def _operator(self, method, path, **kwargs):
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = "Bearer operator-test-token"
        return self.client.open(
            path,
            method=method,
            headers=headers,
            base_url="http://localhost:5200",
            **kwargs,
        )

    def _tenant(self, method, path, **kwargs):
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.tenant_token.token}"
        return self.client.open(path, method=method, headers=headers, **kwargs)

    def test_tenant_token_is_forbidden_on_management_and_metrics(self):
        for path in ("/api/admin/tenants", "/api/admin/metrics"):
            with self.subTest(path=path):
                self.assertEqual(self._tenant("GET", path).status_code, 403)

    def test_operator_lists_and_creates_tenant_with_one_time_token(self):
        listed = self._operator("GET", "/api/admin/tenants")
        self.assertEqual(listed.status_code, 200)
        ids = {t["id"] for t in listed.get_json()["tenants"]}
        self.assertEqual(ids, {"owner", "t_alpha"})

        created = self._operator(
            "POST",
            "/api/admin/tenants",
            json={"display_name": "Gamma", "scopes": ["app", "tenant_admin"]},
        )
        self.assertEqual(created.status_code, 201)
        body = created.get_json()
        new_id = body["tenant"]["id"]
        self.assertTrue(body["token"].startswith("rssai_tk_"))
        self.assertEqual(set(body["token_meta"]["scopes"]), {"app", "tenant_admin"})

        # The plaintext token must never come back from a subsequent list.
        tokens = self._operator(
            "GET", f"/api/admin/tenants/{new_id}/tokens",
        ).get_json()["tokens"]
        self.assertEqual(len(tokens), 1)
        listed_text = self._operator(
            "GET", f"/api/admin/tenants/{new_id}/tokens",
        ).get_data(as_text=True)
        self.assertNotIn(body["token"], listed_text)

    def test_create_rejects_invalid_scope_and_empty_name(self):
        bad_scope = self._operator(
            "POST",
            "/api/admin/tenants",
            json={"display_name": "X", "scopes": ["root"]},
        )
        self.assertEqual(bad_scope.status_code, 400)
        empty = self._operator(
            "POST", "/api/admin/tenants", json={"display_name": "  "},
        )
        self.assertEqual(empty.status_code, 400)

    def test_soft_delete_then_purge_flow(self):
        deleted = self._operator(
            "POST", "/api/admin/tenants/t_alpha/delete",
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.get_json()["tenant"]["status"], "deleted")
        # Token no longer authenticates.
        self.assertEqual(
            self._tenant("GET", "/api/auth/me").status_code, 401,
        )
        purged = self._operator(
            "POST", "/api/admin/tenants/t_alpha/purge",
        )
        self.assertEqual(purged.status_code, 200)
        self.assertTrue(Path(purged.get_json()["backup_path"]).exists())
        remaining = {
            t["id"] for t in self._operator("GET", "/api/admin/tenants").get_json()["tenants"]
        }
        self.assertNotIn("t_alpha", remaining)

    def test_owner_delete_is_rejected(self):
        response = self._operator("POST", "/api/admin/tenants/owner/delete")
        self.assertEqual(response.status_code, 400)

    def test_metrics_reports_storage_and_availability(self):
        response = self._operator("GET", "/api/admin/metrics")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIn("available", body)
        self.assertIn("storage", body)
        self.assertIn("data_root_bytes", body["storage"])
        self.assertIn("tenant_count", body)


if __name__ == "__main__":
    unittest.main()
