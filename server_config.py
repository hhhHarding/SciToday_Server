"""Server-level configuration and paths.

This module deliberately contains no tenant data paths. Tenant-owned paths live
in ``tenancy.paths`` so global and tenant state cannot be mixed accidentally.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


SERVER_DATA_DIR_ENV = "RSSAI_SERVER_DATA_DIR"
DEFAULT_SERVER_DATA_DIR_NAME = "RssAiPushServerData"


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve(strict=False)


@dataclass(frozen=True, slots=True)
class ServerPaths:
    """Paths that belong to the server operator rather than any tenant."""

    data_root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_root", _absolute_path(self.data_root))

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> "ServerPaths":
        env = os.environ if environ is None else environ
        raw = (env.get(SERVER_DATA_DIR_ENV) or "").strip()
        root = Path(raw) if raw else (home or Path.home()) / DEFAULT_SERVER_DATA_DIR_NAME
        return cls(root)

    @property
    def control_dir(self) -> Path:
        return self.data_root / "control"

    @property
    def control_db(self) -> Path:
        return self.control_dir / "control.db"

    @property
    def control_backups_dir(self) -> Path:
        return self.control_dir / "backups"

    @property
    def global_dir(self) -> Path:
        return self.data_root / "global"

    @property
    def shared_dir(self) -> Path:
        """Cross-tenant content cache owned by the operator, not any tenant.

        RSS content is digested once with the owner key and stored here; every
        tenant (including owner) reads from it. Kept outside ``tenants/`` so it
        never collides with per-tenant isolation guarantees.
        """
        return self.data_root / "shared"

    @property
    def shared_inbox_dir(self) -> Path:
        return self.shared_dir / "inbox"

    @property
    def shared_content_db(self) -> Path:
        return self.shared_dir / "content.db"

    @property
    def logs_dir(self) -> Path:
        return self.global_dir / "logs"

    @property
    def server_log(self) -> Path:
        return self.logs_dir / "server.log"

    @property
    def quick_tunnel_state(self) -> Path:
        return self.global_dir / "quick_tunnel.json"

    @property
    def tray_command(self) -> Path:
        return self.global_dir / "tray_command.json"

    @property
    def tray_config(self) -> Path:
        return self.global_dir / "tray_config.env"

    @property
    def operator_token_file(self) -> Path:
        return self.control_dir / "operator_token"

    def ensure_global_directories(self) -> None:
        for path in (
            self.control_dir,
            self.control_backups_dir,
            self.logs_dir,
            self.shared_dir,
            self.shared_inbox_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
