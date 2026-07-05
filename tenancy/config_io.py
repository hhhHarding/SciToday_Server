"""Crash-safe, tenant-serialized configuration writes."""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any


_locks_guard = threading.Lock()
_tenant_locks: dict[tuple[str, str], threading.RLock] = {}


def _lock_for(tenant_id: str, path: Path) -> threading.RLock:
    key = (tenant_id, str(path.expanduser().resolve(strict=False)))
    with _locks_guard:
        lock = _tenant_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _tenant_locks[key] = lock
        return lock


def atomic_write_text(
    path: Path,
    content: str,
    *,
    tenant_id: str,
    encoding: str = "utf-8",
) -> None:
    target = Path(path)
    lock = _lock_for(tenant_id, target)
    with lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp_path.open("x", encoding=encoding, newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def atomic_write_json(path: Path, value: Any, *, tenant_id: str) -> None:
    content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path, content, tenant_id=tenant_id)

