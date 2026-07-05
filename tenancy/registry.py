"""SQLite-backed tenant registry and provisioning workflow."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from server_config import ServerPaths

from .config_io import atomic_write_json, atomic_write_text
from .models import ApiToken, IssuedApiToken, Tenant, TenantQuota, TenantStatus
from .paths import (
    TenantPaths,
    ensure_safe_path,
    generate_tenant_id,
    validate_tenant_id,
)


CONTROL_SCHEMA_VERSION = 3
TOKEN_LAST_USED_WRITE_INTERVAL = 300
TOKEN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
TOKEN_RE = re.compile(r"^rssai_tk_([0-9a-f]{16})_([A-Za-z0-9_-]{32,})$")
VALID_TOKEN_SCOPES = frozenset({"app", "ai_config_write", "tenant_admin"})
EMPTY_OPML = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>RssAiPush feeds</title></head>
  <body />
</opml>
"""
# 仓库根目录随附的默认订阅源，用作新租户初始 OPML 与空源租户的回填内容。
_DEFAULT_OPML_FILE = Path(__file__).resolve().parent.parent / "feedly.opml"


def read_default_opml() -> str:
    """返回随附的默认订阅源内容；读不到时回退到空 OPML，保证开通不失败。"""

    try:
        return _DEFAULT_OPML_FILE.read_text(encoding="utf-8-sig")
    except (FileNotFoundError, OSError):
        return EMPTY_OPML


def _normalize_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    """规范化并校验 scope：去空白去重排序，至少一个且全部合法，否则抛 ValueError。"""

    normalized = tuple(sorted({str(scope).strip() for scope in scopes}))
    if not normalized or any(scope not in VALID_TOKEN_SCOPES for scope in normalized):
        raise ValueError(
            "scope 必须是 app、ai_config_write 或 tenant_admin，且至少提供一个"
        )
    return normalized


class TenantProvisioningError(RuntimeError):
    pass


class InvalidTokenError(ValueError):
    """Intentionally generic so callers cannot distinguish failure causes."""


class TenantRegistry:
    def __init__(self, server_paths: ServerPaths):
        self.server_paths = server_paths

    def _connect(self) -> sqlite3.Connection:
        self.server_paths.control_dir.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.server_paths.control_db), timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        return con

    def initialize(self) -> int:
        """Apply all control DB migrations. Safe to call repeatedly."""

        con = self._connect()
        try:
            current = int(con.execute("PRAGMA user_version").fetchone()[0])
            if current > CONTROL_SCHEMA_VERSION:
                raise RuntimeError(
                    f"control.db schema {current} 比程序支持的 "
                    f"{CONTROL_SCHEMA_VERSION} 更新"
                )
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                """CREATE TABLE IF NOT EXISTS tenants(
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(
                        status IN ('provisioning','active','suspended','deleted')
                    ),
                    quota_json TEXT NOT NULL DEFAULT '{}',
                    config_version INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )"""
            )
            con.execute(
                """CREATE TABLE IF NOT EXISTS api_tokens(
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL REFERENCES tenants(id),
                    token_prefix TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    scopes_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at INTEGER NOT NULL,
                    last_used_at INTEGER,
                    expires_at INTEGER,
                    revoked_at INTEGER
                )"""
            )
            token_columns = {
                row[1] for row in con.execute("PRAGMA table_info(api_tokens)").fetchall()
            }
            if "status" not in token_columns:
                con.execute(
                    "ALTER TABLE api_tokens "
                    "ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
                )
            con.execute(
                """UPDATE api_tokens SET status='revoked'
                WHERE revoked_at IS NOT NULL AND status<>'revoked'"""
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_tokens_tenant "
                "ON api_tokens(tenant_id)"
            )
            con.execute(
                """CREATE TABLE IF NOT EXISTS job_state(
                    tenant_id TEXT NOT NULL REFERENCES tenants(id),
                    job_type TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    next_run_at INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'idle',
                    request_id TEXT,
                    trigger_source TEXT,
                    queued_at INTEGER,
                    last_started_at INTEGER,
                    last_finished_at INTEGER,
                    lease_until INTEGER,
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(tenant_id, job_type)
                )"""
            )
            con.execute(
                """CREATE INDEX IF NOT EXISTS idx_job_state_due
                ON job_state(enabled, next_run_at, status)"""
            )
            con.execute(f"PRAGMA user_version={CONTROL_SCHEMA_VERSION}")
            con.commit()
            return CONTROL_SCHEMA_VERSION
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def schema_version(self) -> int:
        self.initialize()
        con = self._connect()
        try:
            return int(con.execute("PRAGMA user_version").fetchone()[0])
        finally:
            con.close()

    def create_tenant(
        self,
        display_name: str,
        *,
        tenant_id: str | None = None,
        quota: TenantQuota | dict[str, Any] | None = None,
        default_config: dict[str, Any] | None = None,
        default_opml: str = EMPTY_OPML,
    ) -> Tenant:
        """Create storage while the registry row remains provisioning."""

        self.initialize()
        name = str(display_name or "").strip()
        if not name:
            raise ValueError("display_name 不能为空")
        if len(name) > 120:
            raise ValueError("display_name 不能超过 120 个字符")
        internal_id = validate_tenant_id(tenant_id or generate_tenant_id())
        if isinstance(quota, TenantQuota):
            quota_value = quota.as_dict()
        else:
            quota_value = dict(quota or {})
        now = int(time.time())

        con = self._connect()
        try:
            con.execute(
                """INSERT INTO tenants(
                    id, display_name, status, quota_json,
                    config_version, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 1, ?, ?)""",
                (
                    internal_id,
                    name,
                    TenantStatus.PROVISIONING.value,
                    json.dumps(quota_value, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            con.commit()
        except sqlite3.IntegrityError as exc:
            con.rollback()
            raise ValueError(f"tenant_id 已存在: {internal_id}") from exc
        finally:
            con.close()

        paths = TenantPaths(self.server_paths.data_root, internal_id)
        try:
            self._initialize_tenant_storage(
                paths,
                default_config=default_config or {},
                default_opml=default_opml,
            )
        except Exception as exc:
            # Keep the row in provisioning for diagnosis and safe retry/cleanup.
            con = self._connect()
            try:
                con.execute(
                    "UPDATE tenants SET updated_at=? WHERE id=?",
                    (int(time.time()), internal_id),
                )
                con.commit()
            finally:
                con.close()
            raise TenantProvisioningError(
                f"租户 {internal_id} 初始化失败，状态保留为 provisioning"
            ) from exc

        con = self._connect()
        try:
            completed_at = int(time.time())
            con.execute(
                "UPDATE tenants SET status=?, updated_at=? WHERE id=?",
                (TenantStatus.ACTIVE.value, completed_at, internal_id),
            )
            con.commit()
        finally:
            con.close()
        return self.get_tenant(internal_id)

    def ensure_owner(
        self,
        *,
        default_config: dict[str, Any] | None = None,
    ) -> Tenant:
        self.initialize()
        existing = self.get_tenant("owner", required=False)
        if existing is not None:
            return existing
        return self.create_tenant(
            "Owner",
            tenant_id="owner",
            default_config=default_config or {},
        )

    def _initialize_tenant_storage(
        self,
        paths: TenantPaths,
        *,
        default_config: dict[str, Any],
        default_opml: str,
    ) -> None:
        paths.ensure_directories()
        if not paths.config.exists():
            atomic_write_json(
                paths.config,
                default_config,
                tenant_id=paths.tenant_id,
            )
        if not paths.opml.exists():
            atomic_write_text(
                paths.opml,
                default_opml,
                tenant_id=paths.tenant_id,
            )
        for db_path in paths.database_paths:
            con = sqlite3.connect(str(db_path))
            try:
                con.execute("PRAGMA user_version=0")
                con.commit()
            finally:
                con.close()

    def get_tenant(self, tenant_id: str, *, required: bool = True) -> Tenant | None:
        self.initialize()
        internal_id = validate_tenant_id(tenant_id)
        con = self._connect()
        try:
            row = con.execute(
                "SELECT * FROM tenants WHERE id=?",
                (internal_id,),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            if required:
                raise KeyError(f"租户不存在: {internal_id}")
            return None
        return self._tenant_from_mapping(row)

    def list_tenants(self) -> list[Tenant]:
        self.initialize()
        con = self._connect()
        try:
            rows: Iterable[sqlite3.Row] = con.execute(
                "SELECT * FROM tenants ORDER BY created_at, id"
            ).fetchall()
        finally:
            con.close()
        return [self._tenant_from_mapping(row) for row in rows]

    def ensure_job_schedule(
        self,
        tenant_id: str,
        job_type: str,
        *,
        interval_seconds: int,
        enabled: bool,
        now: int | None = None,
    ) -> None:
        self.initialize()
        internal_id = validate_tenant_id(tenant_id)
        interval = max(1, int(interval_seconds))
        checked_at = int(time.time() if now is None else now)
        con = self._connect()
        try:
            con.execute(
                """INSERT INTO job_state(
                    tenant_id, job_type, interval_seconds, enabled,
                    next_run_at, status, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'idle', ?)
                ON CONFLICT(tenant_id, job_type) DO UPDATE SET
                    next_run_at=CASE
                        WHEN job_state.interval_seconds<>excluded.interval_seconds
                            THEN excluded.next_run_at
                        ELSE job_state.next_run_at
                    END,
                    interval_seconds=excluded.interval_seconds,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at""",
                (
                    internal_id,
                    str(job_type),
                    interval,
                    1 if enabled else 0,
                    checked_at + interval,
                    checked_at,
                ),
            )
            con.commit()
        finally:
            con.close()

    def list_due_jobs(self, *, now: int | None = None) -> list[dict[str, Any]]:
        self.initialize()
        checked_at = int(time.time() if now is None else now)
        con = self._connect()
        try:
            rows = con.execute(
                """SELECT job.tenant_id, job.job_type, job.interval_seconds,
                    job.next_run_at
                FROM job_state AS job
                JOIN tenants AS tenant ON tenant.id=job.tenant_id
                WHERE job.enabled=1
                  AND job.next_run_at<=?
                  AND job.status NOT IN ('queued','running')
                  AND tenant.status='active'
                ORDER BY job.next_run_at, job.tenant_id, job.job_type""",
                (checked_at,),
            ).fetchall()
        finally:
            con.close()
        return [dict(row) for row in rows]

    def record_job_queued(
        self,
        tenant_id: str,
        job_type: str,
        *,
        request_id: str,
        trigger_source: str,
        now: int | None = None,
    ) -> None:
        self._upsert_job_runtime(
            tenant_id,
            job_type,
            status="queued",
            request_id=request_id,
            trigger_source=trigger_source,
            queued_at=int(time.time() if now is None else now),
            last_error="",
        )

    def record_job_running(
        self,
        tenant_id: str,
        job_type: str,
        *,
        request_id: str,
        lease_seconds: int,
        now: int | None = None,
    ) -> None:
        started_at = int(time.time() if now is None else now)
        self._upsert_job_runtime(
            tenant_id,
            job_type,
            status="running",
            request_id=request_id,
            last_started_at=started_at,
            lease_until=started_at + max(1, int(lease_seconds)),
            last_error="",
        )

    def record_job_finished(
        self,
        tenant_id: str,
        job_type: str,
        *,
        request_id: str,
        success: bool,
        error: str = "",
        next_run_at: int | None = None,
        now: int | None = None,
    ) -> None:
        finished_at = int(time.time() if now is None else now)
        values: dict[str, Any] = {
            "status": "succeeded" if success else "failed",
            "request_id": request_id,
            "last_finished_at": finished_at,
            "lease_until": None,
            "last_error": str(error or "")[:1000],
        }
        if next_run_at is not None:
            values["next_run_at"] = int(next_run_at)
        self._upsert_job_runtime(tenant_id, job_type, **values)

    def defer_job(
        self,
        tenant_id: str,
        job_type: str,
        *,
        reason: str,
        next_run_at: int,
        request_id: str | None = None,
    ) -> None:
        self._upsert_job_runtime(
            tenant_id,
            job_type,
            status="deferred",
            request_id=request_id,
            lease_until=None,
            last_error=str(reason or "")[:1000],
            next_run_at=int(next_run_at),
        )

    def recover_incomplete_jobs(self, *, now: int | None = None) -> int:
        self.initialize()
        checked_at = int(time.time() if now is None else now)
        con = self._connect()
        try:
            cursor = con.execute(
                """UPDATE job_state
                SET status='deferred', lease_until=NULL,
                    next_run_at=CASE
                        WHEN enabled=1 THEN MIN(next_run_at, ?)
                        ELSE next_run_at
                    END,
                    last_error='recovered after process restart',
                    updated_at=?
                WHERE status IN ('queued','running')""",
                (checked_at, checked_at),
            )
            con.commit()
            return int(cursor.rowcount)
        finally:
            con.close()

    def get_job_states(self, tenant_id: str) -> list[dict[str, Any]]:
        self.initialize()
        internal_id = validate_tenant_id(tenant_id)
        con = self._connect()
        try:
            rows = con.execute(
                """SELECT * FROM job_state
                WHERE tenant_id=? ORDER BY job_type""",
                (internal_id,),
            ).fetchall()
        finally:
            con.close()
        return [dict(row) for row in rows]

    def _upsert_job_runtime(
        self,
        tenant_id: str,
        job_type: str,
        **values: Any,
    ) -> None:
        self.initialize()
        internal_id = validate_tenant_id(tenant_id)
        allowed = {
            "status",
            "request_id",
            "trigger_source",
            "queued_at",
            "last_started_at",
            "last_finished_at",
            "lease_until",
            "last_error",
            "next_run_at",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        updates["updated_at"] = int(time.time())
        columns = ["tenant_id", "job_type", *updates.keys()]
        params = [internal_id, str(job_type), *updates.values()]
        assignments = ", ".join(
            f"{column}=excluded.{column}" for column in updates
        )
        placeholders = ", ".join("?" for _ in columns)
        con = self._connect()
        try:
            con.execute(
                f"""INSERT INTO job_state({', '.join(columns)})
                VALUES({placeholders})
                ON CONFLICT(tenant_id, job_type) DO UPDATE SET {assignments}""",
                params,
            )
            con.commit()
        finally:
            con.close()

    def set_tenant_status(self, tenant_id: str, status: TenantStatus | str) -> Tenant:
        self.initialize()
        internal_id = validate_tenant_id(tenant_id)
        next_status = status if isinstance(status, TenantStatus) else TenantStatus(status)
        con = self._connect()
        try:
            cursor = con.execute(
                "UPDATE tenants SET status=?, updated_at=? WHERE id=?",
                (next_status.value, int(time.time()), internal_id),
            )
            con.commit()
        finally:
            con.close()
        if cursor.rowcount != 1:
            raise KeyError(f"租户不存在: {internal_id}")
        return self.get_tenant(internal_id)

    def soft_delete_tenant(self, tenant_id: str) -> Tenant:
        """Mark a tenant deleted, revoke its tokens, and stop its schedules.

        Reversible: tenant storage stays on disk so an operator can inspect,
        recover, or later purge it. The owner tenant is protected because the
        shared ingest pipeline runs under its identity.
        """

        self.initialize()
        internal_id = validate_tenant_id(tenant_id)
        if internal_id == "owner":
            raise ValueError("不能删除 owner 租户")
        deleted_at = int(time.time())
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT status FROM tenants WHERE id=?",
                (internal_id,),
            ).fetchone()
            if row is None:
                con.rollback()
                raise KeyError(f"租户不存在: {internal_id}")
            con.execute(
                "UPDATE tenants SET status=?, updated_at=? WHERE id=?",
                (TenantStatus.DELETED.value, deleted_at, internal_id),
            )
            con.execute(
                """UPDATE api_tokens SET status='revoked', revoked_at=?
                WHERE tenant_id=? AND status='active'""",
                (deleted_at, internal_id),
            )
            con.execute(
                """UPDATE job_state SET enabled=0, updated_at=?
                WHERE tenant_id=?""",
                (deleted_at, internal_id),
            )
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return self.get_tenant(internal_id)

    def purge_tenant(self, tenant_id: str, *, backups_dir: Path | None = None) -> Path:
        """Back up then permanently delete a soft-deleted tenant's storage.

        Requires the tenant to already be ``deleted`` so purge is always a
        deliberate second step. Returns the backup archive path.
        """

        self.initialize()
        internal_id = validate_tenant_id(tenant_id)
        if internal_id == "owner":
            raise ValueError("不能删除 owner 租户")
        tenant = self.get_tenant(internal_id, required=False)
        if tenant is None:
            raise KeyError(f"租户不存在: {internal_id}")
        if tenant.status is not TenantStatus.DELETED:
            raise ValueError("只能彻底删除已软删除(deleted)的租户")

        paths = TenantPaths(self.server_paths.data_root, internal_id)
        tenant_dir = paths.tenant_dir  # ensure_safe_path runs inside the property.
        backups_root = Path(backups_dir or self.server_paths.control_backups_dir)
        backups_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_base = backups_root / f"{internal_id}-{stamp}"
        archive_path: Path | None = None
        if tenant_dir.exists():
            # make_archive appends .zip; base_dir keeps the tenant folder name
            # as the archive's top-level directory for a clean restore.
            archive_str = shutil.make_archive(
                str(archive_base),
                "zip",
                root_dir=str(tenant_dir.parent),
                base_dir=internal_id,
            )
            archive_path = Path(archive_str)
            ensure_safe_path(self.server_paths.data_root, tenant_dir)
            shutil.rmtree(tenant_dir)

        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            # Child rows first: api_tokens/job_state reference tenants(id).
            con.execute("DELETE FROM job_state WHERE tenant_id=?", (internal_id,))
            con.execute("DELETE FROM api_tokens WHERE tenant_id=?", (internal_id,))
            con.execute("DELETE FROM tenants WHERE id=?", (internal_id,))
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return archive_path if archive_path is not None else archive_base.with_suffix(".zip")

    def create_token(
        self,
        tenant_id: str,
        *,
        scopes: Iterable[str] = ("app",),
        expires_at: int | None = None,
        now: int | None = None,
    ) -> IssuedApiToken:
        """Issue one high-entropy token; plaintext is returned only once."""

        self.initialize()
        tenant = self.get_tenant(tenant_id)
        if tenant.status is not TenantStatus.ACTIVE:
            raise ValueError("只能为 active 租户创建 token")
        normalized_scopes = _normalize_scopes(scopes)
        issued_at = int(time.time() if now is None else now)
        if expires_at is not None and int(expires_at) <= issued_at:
            raise ValueError("expires_at 必须晚于创建时间")

        token_id = secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        plaintext = f"rssai_tk_{token_id}_{secret}"
        # 前缀仅用于展示识别，只含公开的 token_id，绝不写入 secret 的任何字节。
        prefix = f"rssai_tk_{token_id}…"
        token_hash = self._hash_token(plaintext)
        con = self._connect()
        try:
            con.execute(
                """INSERT INTO api_tokens(
                    id, tenant_id, token_prefix, token_hash, scopes_json,
                    status, created_at, expires_at
                ) VALUES(?, ?, ?, ?, ?, 'active', ?, ?)""",
                (
                    token_id,
                    tenant.id,
                    prefix,
                    token_hash,
                    json.dumps(normalized_scopes, ensure_ascii=False),
                    issued_at,
                    int(expires_at) if expires_at is not None else None,
                ),
            )
            con.commit()
        finally:
            con.close()
        return IssuedApiToken(
            token=plaintext,
            record=ApiToken(
                id=token_id,
                tenant_id=tenant.id,
                token_prefix=prefix,
                scopes=normalized_scopes,
                status="active",
                created_at=issued_at,
                expires_at=int(expires_at) if expires_at is not None else None,
            ),
        )

    def set_token_scopes(
        self,
        token_id: str,
        scopes: Iterable[str],
    ) -> ApiToken:
        """Replace one token's scopes without rotating its plaintext secret."""

        self.initialize()
        normalized_id = str(token_id or "").strip().lower()
        if not TOKEN_ID_RE.fullmatch(normalized_id):
            raise ValueError("token_id 格式错误")
        normalized_scopes = _normalize_scopes(scopes)
        con = self._connect()
        try:
            cursor = con.execute(
                "UPDATE api_tokens SET scopes_json=? WHERE id=?",
                (
                    json.dumps(normalized_scopes, ensure_ascii=False),
                    normalized_id,
                ),
            )
            con.commit()
            row = con.execute(
                "SELECT * FROM api_tokens WHERE id=?",
                (normalized_id,),
            ).fetchone()
        finally:
            con.close()
        if cursor.rowcount != 1 or row is None:
            raise KeyError("token 不存在")
        return self._token_from_row(row, scopes=normalized_scopes)

    def verify_token(
        self,
        plaintext: str,
        *,
        now: int | None = None,
        last_used_interval: int = TOKEN_LAST_USED_WRITE_INTERVAL,
    ) -> tuple[Tenant, ApiToken]:
        """Validate a token and return its active tenant and safe metadata."""

        match = TOKEN_RE.fullmatch(str(plaintext or ""))
        if match is None:
            raise InvalidTokenError("invalid credentials")
        token_id = match.group(1)
        checked_at = int(time.time() if now is None else now)
        self.initialize()
        con = self._connect()
        try:
            row = con.execute(
                """SELECT tok.*, tenant.display_name, tenant.status AS tenant_status,
                    tenant.quota_json, tenant.config_version,
                    tenant.created_at AS tenant_created_at,
                    tenant.updated_at AS tenant_updated_at
                FROM api_tokens AS tok
                JOIN tenants AS tenant ON tenant.id=tok.tenant_id
                WHERE tok.id=?""",
                (token_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "active"
                or row["revoked_at"] is not None
                or row["tenant_status"] != TenantStatus.ACTIVE.value
                or (
                    row["expires_at"] is not None
                    and int(row["expires_at"]) <= checked_at
                )
                or not hmac.compare_digest(
                    str(row["token_hash"]),
                    self._hash_token(plaintext),
                )
            ):
                raise InvalidTokenError("invalid credentials")

            last_used_at = (
                int(row["last_used_at"]) if row["last_used_at"] is not None else None
            )
            if (
                last_used_at is None
                or checked_at - last_used_at >= max(0, int(last_used_interval))
            ):
                con.execute(
                    """UPDATE api_tokens SET last_used_at=?
                    WHERE id=? AND status='active'""",
                    (checked_at, token_id),
                )
                con.commit()
                last_used_at = checked_at

            try:
                scopes = tuple(json.loads(row["scopes_json"] or "[]"))
            except (TypeError, json.JSONDecodeError):
                scopes = ()
            tenant_row = {
                "id": row["tenant_id"],
                "display_name": row["display_name"],
                "status": row["tenant_status"],
                "quota_json": row["quota_json"],
                "config_version": row["config_version"],
                "created_at": row["tenant_created_at"],
                "updated_at": row["tenant_updated_at"],
            }
            tenant = self._tenant_from_mapping(tenant_row)
            token = self._token_from_row(
                row,
                scopes=scopes,
                last_used_at=last_used_at,
            )
            return tenant, token
        finally:
            con.close()

    def revoke_token(self, token_id: str, *, now: int | None = None) -> ApiToken:
        self.initialize()
        normalized_id = str(token_id or "").strip().lower()
        if not TOKEN_ID_RE.fullmatch(normalized_id):
            raise ValueError("token_id 格式错误")
        revoked_at = int(time.time() if now is None else now)
        con = self._connect()
        try:
            cursor = con.execute(
                """UPDATE api_tokens SET status='revoked', revoked_at=?
                WHERE id=? AND status='active'""",
                (revoked_at, normalized_id),
            )
            con.commit()
            row = con.execute(
                "SELECT * FROM api_tokens WHERE id=?",
                (normalized_id,),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            raise KeyError("token 不存在")
        if cursor.rowcount not in (0, 1):
            raise RuntimeError("token 撤销状态异常")
        return self._token_from_row(row)

    def list_tokens(self, tenant_id: str) -> list[ApiToken]:
        self.initialize()
        internal_id = validate_tenant_id(tenant_id)
        con = self._connect()
        try:
            rows = con.execute(
                """SELECT * FROM api_tokens
                WHERE tenant_id=? ORDER BY created_at, id""",
                (internal_id,),
            ).fetchall()
        finally:
            con.close()
        return [self._token_from_row(row) for row in rows]

    @staticmethod
    def _hash_token(plaintext: str) -> str:
        return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()

    @staticmethod
    def _token_from_row(
        row: sqlite3.Row,
        *,
        scopes: tuple[str, ...] | None = None,
        last_used_at: int | None = None,
    ) -> ApiToken:
        if scopes is None:
            try:
                scopes = tuple(json.loads(row["scopes_json"] or "[]"))
            except (TypeError, json.JSONDecodeError):
                scopes = ()
        return ApiToken(
            id=row["id"],
            tenant_id=row["tenant_id"],
            token_prefix=row["token_prefix"],
            scopes=scopes,
            status=row["status"],
            created_at=int(row["created_at"]),
            last_used_at=(
                last_used_at
                if last_used_at is not None
                else (
                    int(row["last_used_at"])
                    if row["last_used_at"] is not None
                    else None
                )
            ),
            expires_at=(
                int(row["expires_at"]) if row["expires_at"] is not None else None
            ),
            revoked_at=(
                int(row["revoked_at"]) if row["revoked_at"] is not None else None
            ),
        )

    @staticmethod
    def _tenant_from_mapping(row: Any) -> Tenant:
        try:
            quota = json.loads(row["quota_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            quota = {}
        return Tenant(
            id=row["id"],
            display_name=row["display_name"],
            status=TenantStatus(row["status"]),
            quota=quota,
            config_version=int(row["config_version"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )
