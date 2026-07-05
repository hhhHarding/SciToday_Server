import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server_config import ServerPaths
from tenancy.models import TenantStatus
from tenancy.paths import TenantPaths
from tenancy.registry import (
    CONTROL_SCHEMA_VERSION,
    InvalidTokenError,
    TenantProvisioningError,
    TenantRegistry,
)


class TenantRegistryTests(unittest.TestCase):
    def _registry(self, temp_dir):
        return TenantRegistry(ServerPaths(Path(temp_dir) / "server"))

    def test_control_db_migration_is_idempotent_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self._registry(temp_dir)
            self.assertEqual(registry.initialize(), CONTROL_SCHEMA_VERSION)
            created = registry.create_tenant("Alpha", tenant_id="t_alpha")

            self.assertEqual(registry.initialize(), CONTROL_SCHEMA_VERSION)
            self.assertEqual(registry.initialize(), CONTROL_SCHEMA_VERSION)
            loaded = registry.get_tenant(created.id)

            con = sqlite3.connect(str(registry.server_paths.control_db))
            try:
                version = con.execute("PRAGMA user_version").fetchone()[0]
                row_count = con.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
            finally:
                con.close()

        self.assertEqual(version, CONTROL_SCHEMA_VERSION)
        self.assertEqual(row_count, 1)
        self.assertEqual(loaded.display_name, "Alpha")
        self.assertEqual(loaded.status, TenantStatus.ACTIVE)

    def test_tenant_becomes_active_only_after_all_storage_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self._registry(temp_dir)
            tenant = registry.create_tenant(
                "..\\not-a-directory",
                tenant_id="t_storage",
                default_config={"rss": {"lookback_days": 7}},
            )
            paths = TenantPaths(registry.server_paths.data_root, tenant.id)

            self.assertEqual(tenant.status, TenantStatus.ACTIVE)
            self.assertEqual(
                json.loads(paths.config.read_text(encoding="utf-8")),
                {"rss": {"lookback_days": 7}},
            )
            self.assertIn("<opml", paths.opml.read_text(encoding="utf-8"))
            self.assertTrue(all(path.is_file() for path in paths.database_paths))
            self.assertNotIn("not-a-directory", str(paths.tenant_dir))
            for db_path in paths.database_paths:
                con = sqlite3.connect(str(db_path))
                try:
                    self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                finally:
                    con.close()

    def test_failed_storage_initialization_leaves_provisioning_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self._registry(temp_dir)
            with patch.object(
                registry,
                "_initialize_tenant_storage",
                side_effect=OSError("injected failure"),
            ):
                with self.assertRaises(TenantProvisioningError):
                    registry.create_tenant("Broken", tenant_id="t_broken")

            tenant = registry.get_tenant("t_broken")

        self.assertEqual(tenant.status, TenantStatus.PROVISIONING)

    def test_owner_initialization_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self._registry(temp_dir)
            first = registry.ensure_owner(default_config={"owner": True})
            second = registry.ensure_owner(default_config={"owner": False})
            tenant_count = len(registry.list_tenants())

        self.assertEqual(first.id, "owner")
        self.assertEqual(second.id, "owner")
        self.assertEqual(tenant_count, 1)

    def test_plaintext_token_is_never_stored_and_last_used_is_throttled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self._registry(temp_dir)
            registry.create_tenant("Alpha", tenant_id="t_alpha")
            issued = registry.create_token(
                "t_alpha",
                scopes=("app", "tenant_admin"),
                now=100,
            )

            con = sqlite3.connect(str(registry.server_paths.control_db))
            try:
                stored_hash, stored_prefix = con.execute(
                    "SELECT token_hash, token_prefix FROM api_tokens WHERE id=?",
                    (issued.record.id,),
                ).fetchone()
            finally:
                con.close()

            self.assertNotEqual(stored_hash, issued.token)
            self.assertNotIn(issued.token, stored_prefix)
            self.assertNotIn(
                issued.token.encode("utf-8"),
                registry.server_paths.control_db.read_bytes(),
            )

            _, first = registry.verify_token(issued.token, now=200)
            _, throttled = registry.verify_token(issued.token, now=300)
            _, refreshed = registry.verify_token(issued.token, now=500)

        self.assertEqual(first.last_used_at, 200)
        self.assertEqual(throttled.last_used_at, 200)
        self.assertEqual(refreshed.last_used_at, 500)

    def test_token_scopes_can_add_ai_config_write_without_rotation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self._registry(temp_dir)
            registry.create_tenant("Alpha", tenant_id="t_alpha")
            issued = registry.create_token("t_alpha", scopes=("app",))
            updated = registry.set_token_scopes(
                issued.record.id,
                ("app", "ai_config_write"),
            )
            tenant, verified = registry.verify_token(issued.token)

        self.assertEqual(updated.scopes, ("ai_config_write", "app"))
        self.assertEqual(verified.scopes, ("ai_config_write", "app"))
        self.assertEqual(tenant.id, "t_alpha")

    def test_revoked_expired_and_suspended_tokens_are_indistinguishable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self._registry(temp_dir)
            registry.create_tenant("Alpha", tenant_id="t_alpha")
            revoked = registry.create_token("t_alpha", now=100)
            expired = registry.create_token("t_alpha", now=100, expires_at=101)
            suspended = registry.create_token("t_alpha", now=100)

            registry.revoke_token(revoked.record.id, now=150)
            for plaintext in (revoked.token, expired.token):
                with self.subTest(kind=plaintext[:18]):
                    with self.assertRaisesRegex(InvalidTokenError, "invalid credentials"):
                        registry.verify_token(plaintext, now=200)

            registry.set_tenant_status("t_alpha", TenantStatus.SUSPENDED)
            with self.assertRaisesRegex(InvalidTokenError, "invalid credentials"):
                registry.verify_token(suspended.token, now=200)

    def test_soft_delete_marks_deleted_and_revokes_tokens(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self._registry(temp_dir)
            registry.create_tenant("Alpha", tenant_id="t_alpha")
            issued = registry.create_token("t_alpha", scopes=("app", "tenant_admin"))
            registry.ensure_job_schedule(
                "t_alpha", "pdf", interval_seconds=300, enabled=True,
            )

            deleted = registry.soft_delete_tenant("t_alpha")

            self.assertEqual(deleted.status, TenantStatus.DELETED)
            tokens = registry.list_tokens("t_alpha")
            self.assertTrue(all(t.status == "revoked" for t in tokens))
            with self.assertRaises(InvalidTokenError):
                registry.verify_token(issued.token)
            job_states = registry.get_job_states("t_alpha")
            self.assertTrue(all(j["enabled"] == 0 for j in job_states))

    def test_owner_cannot_be_soft_deleted_or_purged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self._registry(temp_dir)
            registry.ensure_owner()
            with self.assertRaises(ValueError):
                registry.soft_delete_tenant("owner")
            with self.assertRaises(ValueError):
                registry.purge_tenant("owner")

    def test_purge_requires_deleted_status_then_backs_up_and_removes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self._registry(temp_dir)
            registry.create_tenant("Alpha", tenant_id="t_alpha")
            paths = TenantPaths(registry.server_paths.data_root, "t_alpha")

            # Active tenant cannot be purged.
            with self.assertRaises(ValueError):
                registry.purge_tenant("t_alpha")

            registry.soft_delete_tenant("t_alpha")
            archive = registry.purge_tenant(
                "t_alpha",
                backups_dir=registry.server_paths.control_backups_dir,
            )

            self.assertTrue(archive.exists())
            self.assertEqual(archive.suffix, ".zip")
            self.assertFalse(paths.tenant_dir.exists())
            self.assertIsNone(registry.get_tenant("t_alpha", required=False))
            self.assertEqual(registry.list_tokens("t_alpha"), [])

    def test_soft_delete_missing_tenant_raises_keyerror(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self._registry(temp_dir)
            with self.assertRaises(KeyError):
                registry.soft_delete_tenant("t_missing")

    def test_v1_control_db_migrates_token_status_idempotently(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self._registry(temp_dir)
            registry.server_paths.control_dir.mkdir(parents=True)
            con = sqlite3.connect(str(registry.server_paths.control_db))
            try:
                con.execute(
                    """CREATE TABLE tenants(
                        id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                        status TEXT NOT NULL, quota_json TEXT NOT NULL DEFAULT '{}',
                        config_version INTEGER NOT NULL DEFAULT 1,
                        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                    )"""
                )
                con.execute(
                    """CREATE TABLE api_tokens(
                        id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                        token_prefix TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                        scopes_json TEXT NOT NULL DEFAULT '[]',
                        created_at INTEGER NOT NULL, last_used_at INTEGER,
                        expires_at INTEGER, revoked_at INTEGER
                    )"""
                )
                con.execute("PRAGMA user_version=1")
                con.commit()
            finally:
                con.close()

            self.assertEqual(registry.initialize(), CONTROL_SCHEMA_VERSION)
            self.assertEqual(registry.initialize(), CONTROL_SCHEMA_VERSION)
            con = sqlite3.connect(str(registry.server_paths.control_db))
            try:
                columns = {
                    row[1] for row in con.execute("PRAGMA table_info(api_tokens)")
                }
                version = con.execute("PRAGMA user_version").fetchone()[0]
            finally:
                con.close()

        self.assertIn("status", columns)
        self.assertEqual(version, CONTROL_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
