"""Small immutable models shared by the registry, CLI, and future auth layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TenantStatus(str, Enum):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class TenantQuota:
    daily_chat_requests: int | None = None
    daily_ai_requests: int | None = None
    storage_bytes: int | None = None
    max_pdf_bytes: int | None = None

    def as_dict(self) -> dict[str, int | None]:
        return {
            "daily_chat_requests": self.daily_chat_requests,
            "daily_ai_requests": self.daily_ai_requests,
            "storage_bytes": self.storage_bytes,
            "max_pdf_bytes": self.max_pdf_bytes,
        }


@dataclass(frozen=True, slots=True)
class Tenant:
    id: str
    display_name: str
    status: TenantStatus
    created_at: int
    updated_at: int
    config_version: int = 1
    quota: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ApiToken:
    id: str
    tenant_id: str
    token_prefix: str
    scopes: tuple[str, ...]
    status: str
    created_at: int
    last_used_at: int | None = None
    expires_at: int | None = None
    revoked_at: int | None = None


@dataclass(frozen=True, slots=True)
class IssuedApiToken:
    """Plaintext is returned once and is never reconstructed from control.db."""

    token: str
    record: ApiToken

