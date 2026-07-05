"""Validated paths for one tenant."""

from __future__ import annotations

import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path


TENANT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def validate_tenant_id(value: str) -> str:
    tenant_id = str(value or "").strip()
    if not TENANT_ID_RE.fullmatch(tenant_id):
        raise ValueError(
            "tenant_id 必须以小写字母开头，且只能包含小写字母、数字、下划线或连字符，"
            "长度不超过 48"
        )
    if tenant_id.rstrip(". ").lower() in WINDOWS_RESERVED_NAMES:
        raise ValueError("tenant_id 是 Windows 保留名称")
    return tenant_id


def generate_tenant_id() -> str:
    return f"t_{secrets.token_hex(12)}"


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except FileNotFoundError:
        return False


def ensure_safe_path(data_root: Path, candidate: Path) -> Path:
    """Return an absolute path after containment and reparse-point checks."""

    root = _absolute_lexical(data_root)
    target = _absolute_lexical(candidate)
    try:
        common = Path(os.path.commonpath((root, target)))
    except ValueError as exc:
        raise ValueError("租户路径不在 server data root 内") from exc
    if common != root:
        raise ValueError("租户路径不在 server data root 内")

    current = root
    paths_to_check = [root]
    if target != root:
        relative = target.relative_to(root)
        for part in relative.parts:
            current = current / part
            paths_to_check.append(current)
    for path in paths_to_check:
        if _is_reparse_point(path):
            raise ValueError(f"租户路径不能经过软链接或目录联接: {path}")

    # resolve() catches a link swapped in between the lexical and attribute checks.
    resolved_root = root.resolve(strict=False)
    resolved_target = target.resolve(strict=False)
    try:
        if Path(os.path.commonpath((resolved_root, resolved_target))) != resolved_root:
            raise ValueError("租户路径解析后逃出 server data root")
    except ValueError as exc:
        raise ValueError("租户路径解析后逃出 server data root") from exc
    return target


@dataclass(frozen=True, slots=True)
class TenantPaths:
    data_root: Path
    tenant_id: str

    def __post_init__(self) -> None:
        root = _absolute_lexical(Path(self.data_root))
        tenant_id = validate_tenant_id(self.tenant_id)
        object.__setattr__(self, "data_root", root)
        object.__setattr__(self, "tenant_id", tenant_id)
        ensure_safe_path(root, root / "tenants" / tenant_id)

    def _child(self, *parts: str) -> Path:
        return ensure_safe_path(self.data_root, self.tenant_dir.joinpath(*parts))

    @property
    def tenant_dir(self) -> Path:
        return ensure_safe_path(
            self.data_root,
            self.data_root / "tenants" / self.tenant_id,
        )

    @property
    def config(self) -> Path:
        return self._child("config.json")

    @property
    def opml(self) -> Path:
        return self._child("feedly.opml")

    @property
    def rss_db(self) -> Path:
        return self._child("rss_ai.db")

    @property
    def pending_db(self) -> Path:
        return self._child("pending_papers.db")

    @property
    def pdf_db(self) -> Path:
        return self._child("pdf_seen.db")

    @property
    def digest_db(self) -> Path:
        return self._child("digest_messages.db")

    @property
    def admin_db(self) -> Path:
        return self._child("admin_state.db")

    @property
    def inbox_dir(self) -> Path:
        return self._child("inbox")

    @property
    def inbox_index(self) -> Path:
        return self._child("inbox", "index.html")

    @property
    def uploaded_pdfs_dir(self) -> Path:
        return self._child("uploaded_pdfs")

    @property
    def pdf_chunks_dir(self) -> Path:
        return self._child("pdf_upload_chunks")

    @property
    def database_paths(self) -> tuple[Path, ...]:
        return (
            self.rss_db,
            self.pending_db,
            self.pdf_db,
            self.digest_db,
            self.admin_db,
        )

    @property
    def directory_paths(self) -> tuple[Path, ...]:
        return (
            self.tenant_dir,
            self.inbox_dir,
            self.uploaded_pdfs_dir,
            self.pdf_chunks_dir,
        )

    def ensure_directories(self) -> None:
        for path in self.directory_paths:
            path.mkdir(parents=True, exist_ok=True)
            ensure_safe_path(self.data_root, path)

