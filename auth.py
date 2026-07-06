"""Authentication helpers shared by Flask middleware and the operator CLI."""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import re
import secrets
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from server_config import ServerPaths
from tenancy.config_io import atomic_write_text


OPERATOR_TOKEN_ENV = "RSSAI_OPERATOR_TOKEN"
OPERATOR_TOKEN_FILE_ENV = "RSSAI_OPERATOR_TOKEN_FILE"
INSECURE_DEV_MODE_ENV = "RSSAI_INSECURE_DEV_MODE"
PROXY_HEADERS = (
    "CF-Connecting-IP",
    "CF-Ray",
    "Forwarded",
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Real-IP",
)
# 完整值替换为占位符的模式（无捕获组）。
_FULL_REDACT_PATTERNS = (
    re.compile(r"rssai_(?:tk|op|ws)_[A-Za-z0-9_-]+"),
    # AI 服务密钥（DeepSeek/OpenAI 风格 sk-...）。
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
)
# 保留前缀、只替换敏感值的模式（含一个捕获组作为保留前缀）。
_PREFIX_REDACT_PATTERNS = (
    # URL query 中的 token/api_key/key/secret/password。
    re.compile(r"(?i)([?&](?:token|api_key|apikey|key|secret|password)=)[^&\s]+"),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"),
    # Cookie / Set-Cookie 头的整段值。
    re.compile(r"(?i)(set-cookie\s*:\s*)[^\r\n]+"),
    re.compile(r"(?i)(\bcookie\s*:\s*)[^\r\n]+"),
    # JSON 里的 api_key 字段值。
    re.compile(r'(?i)("(?:api_key|apikey|token|secret|password)"\s*:\s*")[^"]+'),
)

# 兼容保留：旧代码引用 SECRET_PATTERNS[0]。
SECRET_PATTERNS = _FULL_REDACT_PATTERNS + _PREFIX_REDACT_PATTERNS


@dataclass(frozen=True, slots=True)
class AuthPrincipal:
    kind: str
    tenant_id: str
    scopes: frozenset[str]
    token_id: str | None = None

    @property
    def is_operator(self) -> bool:
        return self.kind == "operator"


def redact_sensitive_text(value: str) -> str:
    text = str(value)
    for pattern in _FULL_REDACT_PATTERNS:
        text = pattern.sub("[REDACTED_TOKEN]", text)
    for pattern in _PREFIX_REDACT_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    return text


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        redacted = redact_sensitive_text(rendered)
        if redacted != rendered:
            record.msg = redacted
            record.args = ()
        return True


def install_sensitive_data_filters() -> None:
    for logger in (
        logging.getLogger(),
        logging.getLogger("werkzeug"),
        logging.getLogger("waitress"),
    ):
        for handler in logger.handlers:
            if not any(isinstance(item, SensitiveDataFilter) for item in handler.filters):
                handler.addFilter(SensitiveDataFilter())


def generate_operator_token() -> str:
    return f"rssai_op_{secrets.token_urlsafe(48)}"


def write_operator_token(
    server_paths: ServerPaths,
    *,
    token: str | None = None,
    path: Path | None = None,
    force: bool = False,
) -> tuple[str, Path]:
    target = Path(path or server_paths.operator_token_file)
    if target.exists() and not force:
        raise FileExistsError(f"operator token 文件已存在: {target}")
    plaintext = token or generate_operator_token()
    atomic_write_text(
        target,
        plaintext + "\n",
        tenant_id="operator",
    )
    try:
        _restrict_secret_file_permissions(target)
    except Exception:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    return plaintext, target


def _restrict_secret_file_permissions(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return
    whoami = subprocess.run(
        ["whoami"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    if not whoami:
        raise RuntimeError("无法确定当前 Windows 用户，未保留 operator token 文件")
    subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{whoami}:(F)",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )


def load_operator_token(
    server_paths: ServerPaths,
    *,
    environ: Mapping[str, str] | None = None,
    explicit_token: str | None = None,
) -> str:
    if explicit_token is not None:
        return explicit_token.strip()
    env = os.environ if environ is None else environ
    from_env = (env.get(OPERATOR_TOKEN_ENV) or "").strip()
    if from_env:
        return from_env
    configured_file = (env.get(OPERATOR_TOKEN_FILE_ENV) or "").strip()
    path = Path(configured_file).expanduser() if configured_file else server_paths.operator_token_file
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except (FileNotFoundError, OSError):
        return ""


def operator_token_matches(provided: str, expected: str) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


def is_loopback_address(value: str | None) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    raw = raw.split("%", 1)[0]
    try:
        return ipaddress.ip_address(raw).is_loopback
    except ValueError:
        return raw.lower() == "localhost"


def _host_name(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return (urlsplit(f"//{raw}").hostname or "").strip()
    except ValueError:
        return ""


def is_local_operator_request(
    *,
    remote_addr: str | None,
    request_host: str | None,
    headers: Mapping[str, str],
) -> bool:
    """Reject requests that arrived through a local reverse proxy/tunnel."""

    if not is_loopback_address(remote_addr):
        return False
    if not is_loopback_address(_host_name(request_host)):
        return False
    return not any(str(headers.get(name) or "").strip() for name in PROXY_HEADERS)


def is_loopback_bind_host(value: str | None) -> bool:
    return is_loopback_address(str(value or "").strip())


class UnsafeOutboundURLError(ValueError):
    """Raised when an operator/tenant-controlled URL targets a non-public host."""


def _ip_is_disallowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """内网/回环/链路本地/保留/多播等非公网地址一律视为不安全的出站目标。"""

    # IPv4-mapped IPv6 (::ffff:169.254.169.254) 必须按其内嵌的 v4 地址判定，
    # 否则元数据地址能借 v6 形态绕过。
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def resolve_public_outbound_url(url: str, *, resolver=None) -> tuple[str, tuple[str, ...]]:
    """校验出站 URL，并返回本次允许连接的公网 IP。

    调用方必须连接返回的 IP，而不能再按主机名做一次 DNS 解析；否则校验与连接之间
    仍可能发生 DNS 重绑定。HTTPS 调用方同时必须保留原始 hostname 用于 Host、SNI
    和证书校验。
    """
    raw = str(url or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeOutboundURLError("URL 必须是 http/https")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeOutboundURLError("URL 不能包含内嵌凭据")
    hostname = (parsed.hostname or "").strip()
    if not hostname:
        raise UnsafeOutboundURLError("URL 缺少主机名")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeOutboundURLError("URL 端口无效") from exc
    if port is not None and not 1 <= port <= 65535:
        raise UnsafeOutboundURLError("URL 端口无效")

    # 字面 IP：直接判定，不做 DNS。
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if _ip_is_disallowed(literal_ip):
            raise UnsafeOutboundURLError(f"URL 指向非公网地址: {hostname}")
        return raw, (str(literal_ip),)

    if hostname.lower() == "localhost" or hostname.lower().endswith(".localhost"):
        raise UnsafeOutboundURLError("URL 指向本机地址")

    # 主机名：解析所有 A/AAAA 记录，任一命中内网段即拒绝。
    resolve = resolver if resolver is not None else _resolve_host_addresses
    try:
        addresses = resolve(hostname)
    except OSError as exc:
        raise UnsafeOutboundURLError(f"无法解析主机名: {hostname}") from exc
    if not addresses:
        raise UnsafeOutboundURLError(f"无法解析主机名: {hostname}")
    public_addresses = []
    for address in addresses:
        try:
            resolved_ip = ipaddress.ip_address(str(address).split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeOutboundURLError(
                f"主机名解析结果无效: {hostname}"
            ) from exc
        if _ip_is_disallowed(resolved_ip):
            raise UnsafeOutboundURLError(f"URL 主机名解析到非公网地址: {hostname}")
        normalized = str(resolved_ip)
        if normalized not in public_addresses:
            public_addresses.append(normalized)
    if not public_addresses:
        raise UnsafeOutboundURLError(f"主机名没有可用公网地址: {hostname}")
    return raw, tuple(public_addresses)


def assert_safe_outbound_url(url: str, *, resolver=None) -> str:
    """校验出站 URL 只指向公网主机，用于不直接发请求的配置检查。"""

    safe_url, _addresses = resolve_public_outbound_url(url, resolver=resolver)
    return safe_url


def _resolve_host_addresses(hostname: str) -> list[str]:
    infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


def insecure_dev_mode_requested(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get(INSECURE_DEV_MODE_ENV) or "").strip() == "1"
