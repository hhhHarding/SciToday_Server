"""Request/task-local tenant identity.

The owner default preserves the existing single-tenant behavior during the
foundation phase. Public authentication will replace this compatibility
default in the next implementation phase.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator


OWNER_TENANT_ID = "owner"
_current_tenant_id: ContextVar[str] = ContextVar(
    "rssai_current_tenant_id",
    default=OWNER_TENANT_ID,
)


def get_current_tenant_id() -> str:
    return _current_tenant_id.get()


def set_current_tenant_id(tenant_id: str) -> Token[str]:
    from .paths import validate_tenant_id

    return _current_tenant_id.set(validate_tenant_id(tenant_id))


def reset_current_tenant_id(token: Token[str]) -> None:
    _current_tenant_id.reset(token)


@contextmanager
def tenant_context(tenant_id: str) -> Iterator[str]:
    token = set_current_tenant_id(tenant_id)
    try:
        yield tenant_id
    finally:
        reset_current_tenant_id(token)

