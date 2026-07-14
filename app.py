import logging
import logging.handlers
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, redirect, request, send_file, send_from_directory

from auth import (
    AuthPrincipal,
    UnsafeOutboundURLError,
    assert_safe_outbound_url,
    insecure_dev_mode_requested,
    install_sensitive_data_filters,
    is_local_operator_request,
    is_loopback_bind_host,
    load_operator_token,
    operator_token_matches,
    redact_sensitive_text,
)
from ratelimit import RateLimiter, category_for_endpoint
from server_config import ServerPaths
import tasks
from tenancy.context import (
    OWNER_TENANT_ID,
    get_current_tenant_id,
    reset_current_tenant_id,
    set_current_tenant_id,
)
from tenancy.registry import (
    InvalidTokenError,
    TenantRegistry,
    VALID_TOKEN_SCOPES,
    read_default_opml,
)

SERVER_PATHS = ServerPaths.from_env()
SERVER_PATHS.logs_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            SERVER_PATHS.server_log,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
install_sensitive_data_filters()
logger = logging.getLogger(__name__)

app = Flask(__name__)
# 单次请求体上限，超限 Flask 自动返回 413，防止无限制上传撑爆磁盘/内存。
# 分片上传按此上限逐片提交；save_uploaded_pdf_chunk 另有累计总量上限。
MAX_REQUEST_BYTES = int(os.environ.get("RSSAI_MAX_REQUEST_BYTES") or 64 * 1024 * 1024)
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
app.config["MAX_FORM_MEMORY_SIZE"] = 1 * 1024 * 1024


def _trusted_hosts():
    """可信 Host 列表：默认 loopback，加上 RSSAI_TRUSTED_HOSTS（逗号分隔）里的隧道域名。

    Flask/Werkzeug 的 host 校验对列表项做前缀 '.' 通配（'.example.com' 匹配子域名），
    精确项需同时覆盖带端口与不带端口两种形式（浏览器/隧道可能带 :443 之外的端口）。
    """
    hosts = ["127.0.0.1", "localhost"]
    raw = os.environ.get("RSSAI_TRUSTED_HOSTS") or ""
    for item in raw.split(","):
        host = item.strip()
        if host and host not in hosts:
            hosts.append(host)
    return hosts


# Host 头校验：拒绝非信任 Host，防 Host 头注入 / DNS rebinding。
# 默认放行 loopback；公网隧道域名通过环境变量加入（start 脚本可注入）。
app.config["TRUSTED_HOSTS"] = _trusted_hosts()
APP_ROOT = Path(__file__).resolve().parent
ADMIN_STATIC_DIR = APP_ROOT / "admin_web"
USER_STATIC_DIR = APP_ROOT / "user_web" / "dist"
TENANT_REGISTRY = TenantRegistry(SERVER_PATHS)
RATE_LIMITER = RateLimiter()
PUBLIC_ENDPOINTS = frozenset({"index", "healthz", "admin_web", "user_web"})
WEB_SESSION_COOKIE = "scitoday_user_session"
WEB_CSRF_COOKIE = "scitoday_csrf"
WEB_SESSION_LIFETIME_SECONDS = 30 * 24 * 60 * 60
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
OPERATOR_ENDPOINTS = frozenset(
    {
        "api_admin_local_settings",
        "api_admin_save_local_settings",
        "api_admin_runtime_command",
        "api_admin_tunnel_refresh",
        "api_admin_settings",
        "api_admin_save_settings",
        "api_logs",
        "api_admin_tenants",
        "api_admin_tenant_delete",
        "api_admin_tenant_purge",
        "api_admin_tenant_tokens",
        "api_admin_tenant_token_revoke",
        "api_admin_metrics",
        "api_admin_feed_health",
        "api_admin_rss_probe",
    }
)
# 破坏性/写配置类租户接口：需要 tenant_admin scope。
# 纯 app scope 的手机 token 只能读、推送、上传、标记偏好，不能抹数据或改配置。
# 注意：RSS 源的增删改/导入（add_feed/update_feed/delete_feed/import_opml）属于租户管理自己内容的
# 常规操作，不是破坏性管理，故只需 app scope（与 get_feeds 一致），不在此集合中。
# 同理：删单张卡片（api_delete_digest）是软删（只从本租户显示列表隐藏、可恢复、不动共享
# 内容），属常规内容管理，只需 app scope；仅批量清空 api_clear_digests 才留在此集合。
TENANT_ADMIN_ENDPOINTS = frozenset(
    {
        "api_clear_digests",
        "api_admin_cleanup",
        "api_reset",
        "api_admin_rss_reset_time",
    }
)
AI_CONFIG_WRITE_ENDPOINTS = frozenset({"api_ai_config", "api_test_ai_config"})
LEGACY_CONFIG_WRITE_ENDPOINTS = frozenset({"api_save_config"})
APP_SETTINGS_WRITE_ENDPOINTS = frozenset(
    {"api_save_schedule_settings", "api_save_recommendation_settings"}
)
# 只能在 URL 查询串携带 token 的端点：摘要页由 App 的 WebView 用 loadUrl 直接 GET，
# 无法附带 Authorization 头，只能把 token 放进 ?token=。仅对这些静态只读端点放行
# query-token 回退，其它接口一律要求 Authorization: Bearer 头。
QUERY_TOKEN_ENDPOINTS = frozenset({"serve_inbox"})

_task_types = (
    "shared_ingest",
    "rss_deliver",
    "rss",
    "pdf",
    "rss_discovery",
    "rss_publish",
    "interest_profile",
)


def _task_coordinator():
    return app.config.get("TASK_COORDINATOR")


def _task_snapshot(tenant_id=None):
    coordinator = _task_coordinator()
    tenant_id = tenant_id or get_current_tenant_id()
    if coordinator is not None:
        return coordinator.snapshot(tenant_id)
    return {
        "running": {task_type: False for task_type in _task_types},
        "progress": {
            task_type: {
                "active": False,
                "current": 0,
                "total": 0,
                "message": "",
                "request_id": "",
                "trigger_source": "",
            }
            for task_type in _task_types
        },
    }


def _wake_scheduler():
    coordinator = _task_coordinator()
    if coordinator is not None:
        coordinator.wake_scheduler()


def _trigger_connect_pipeline(tenant_id):
    """Heartbeat only updates tenant-local delivery; it never polls publishers."""
    coordinator = _task_coordinator()
    if coordinator is None:
        return
    try:
        coordinator.submit(tenant_id, "rss_deliver", trigger_source="heartbeat")
    except Exception:
        logger.exception("心跳触发投递失败")


def _trigger_feed_union_refresh():
    """Synchronize shared feed state after an OPML mutation."""
    coordinator = _task_coordinator()
    if coordinator is None:
        return
    try:
        coordinator.submit(
            OWNER_TENANT_ID,
            "shared_ingest",
            trigger_source="feed_change",
        )
        coordinator.wake_scheduler()
    except Exception:
        logger.exception("订阅源变更后触发共享状态同步失败")


def _provided_bearer_token():
    auth = request.headers.get("Authorization", "")
    scheme, separator, value = auth.partition(" ")
    if separator and scheme.lower() == "bearer":
        token = value.strip()
        if token:
            return token
    # WebView 摘要页无法带 Authorization 头，回退读 ?token=（仅限白名单端点）。
    if request.endpoint in QUERY_TOKEN_ENDPOINTS:
        return (request.args.get("token") or "").strip()
    return ""


def _registry():
    return app.config.get("TENANT_REGISTRY") or TENANT_REGISTRY


def _configured_bind_host():
    configured = app.config.get("BIND_HOST")
    if configured is not None:
        return str(configured)
    from_env = (os.environ.get("RSSAI_SERVER_HOST") or "").strip()
    if from_env:
        return from_env
    return "127.0.0.1"


def _is_local_request():
    return is_local_operator_request(
        remote_addr=request.remote_addr,
        request_host=request.host,
        headers=request.headers,
    )


def _set_request_principal(principal, *, web_session=None):
    g.auth_principal = principal
    g.web_session = web_session
    g.auth_via_web_session = web_session is not None
    g.tenant_context_reset_token = set_current_tenant_id(principal.tenant_id)


def _rate_limit_response(principal):
    """对租户请求按 endpoint 类别限流。超限返回 429 响应，否则 None。"""
    identity = principal.token_id or request.remote_addr or "anon"
    category = category_for_endpoint(request.endpoint)
    decision = RATE_LIMITER.check(category, identity)
    if decision.allowed:
        return None
    response = jsonify({"error": "too_many_requests", "category": category})
    response.status_code = 429
    response.headers["Retry-After"] = str(decision.retry_after)
    return response


def _host_is_trusted():
    """显式 Host 校验：不信任的 Host 头明确返回 400，而非依赖路由层的隐式副作用。"""
    from werkzeug.sansio.utils import host_is_trusted

    trusted = app.config.get("TRUSTED_HOSTS")
    if not trusted:
        return True
    return host_is_trusted(request.host, trusted)


def _cookie_secure():
    configured = app.config.get("WEB_COOKIE_SECURE")
    if configured is not None:
        return bool(configured)
    return bool(
        request.is_secure
        or request.headers.get("X-Forwarded-Proto", "").lower() == "https"
    )


def _clear_web_cookies(response):
    response.delete_cookie(
        WEB_SESSION_COOKIE,
        path="/",
        secure=_cookie_secure(),
        httponly=True,
        samesite="Strict",
    )
    response.delete_cookie(
        WEB_CSRF_COOKIE,
        path="/",
        secure=_cookie_secure(),
        httponly=False,
        samesite="Strict",
    )
    return response


@app.before_request
def _require_auth():
    g.auth_principal = None
    g.web_session = None
    g.auth_via_web_session = False
    g.clear_web_session_cookies = False
    g.tenant_context_reset_token = None
    if not _host_is_trusted():
        return jsonify({"error": "bad_host"}), 400
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None

    if request.endpoint == "web_session" and request.method == "POST":
        decision = RATE_LIMITER.check(
            "web_login",
            request.remote_addr or "unknown",
        )
        if not decision.allowed:
            response = jsonify({"error": "too_many_requests", "category": "web_login"})
            response.status_code = 429
            response.headers["Retry-After"] = str(decision.retry_after)
            return response

    if (
        insecure_dev_mode_requested()
        and is_loopback_bind_host(_configured_bind_host())
        and _is_local_request()
    ):
        _set_request_principal(
            AuthPrincipal(
                kind="developer",
                tenant_id=OWNER_TENANT_ID,
                scopes=frozenset({"app", "tenant_admin"}),
            )
        )
        return None

    provided = _provided_bearer_token()
    token = None
    if provided:
        operator_token = load_operator_token(
            SERVER_PATHS,
            explicit_token=app.config.get("OPERATOR_TOKEN"),
        )
        if operator_token_matches(provided, operator_token):
            if not _is_local_request():
                return jsonify({"error": "forbidden"}), 403
            _set_request_principal(
                AuthPrincipal(
                    kind="operator",
                    tenant_id=OWNER_TENANT_ID,
                    scopes=frozenset({"operator", "app", "tenant_admin"}),
                )
            )
            return None
        try:
            tenant, token = _registry().verify_token(provided)
        except InvalidTokenError:
            return jsonify({"error": "unauthorized"}), 401
        _set_request_principal(
            AuthPrincipal(
                kind="tenant",
                tenant_id=tenant.id,
                scopes=frozenset(token.scopes),
                token_id=token.id,
            )
        )
    else:
        session_cookie = request.cookies.get(WEB_SESSION_COOKIE, "")
        if not session_cookie:
            return jsonify({"error": "unauthorized"}), 401
        try:
            tenant, token, web_session_record = _registry().verify_web_session(
                session_cookie
            )
        except InvalidTokenError:
            g.clear_web_session_cookies = True
            return jsonify({"error": "unauthorized"}), 401
        _set_request_principal(
            AuthPrincipal(
                kind="tenant",
                tenant_id=tenant.id,
                scopes=frozenset(token.scopes),
                token_id=token.id,
            ),
            web_session=web_session_record,
        )
        if request.path.startswith("/api/admin/"):
            return jsonify({"error": "forbidden"}), 403
        if request.method not in SAFE_METHODS:
            csrf = request.headers.get("X-CSRF-Token", "")
            if not _registry().verify_web_session_csrf(web_session_record.id, csrf):
                return jsonify({"error": "csrf_failed"}), 403

    if request.endpoint in OPERATOR_ENDPOINTS:
        return jsonify({"error": "forbidden"}), 403
    if (
        request.endpoint in TENANT_ADMIN_ENDPOINTS
        and "tenant_admin" not in token.scopes
    ):
        return jsonify({"error": "forbidden", "required_scope": "tenant_admin"}), 403
    if (
        request.endpoint in AI_CONFIG_WRITE_ENDPOINTS
        and request.method != "GET"
        and not {"ai_config_write", "tenant_admin"}.intersection(token.scopes)
    ):
        return jsonify({
            "error": "forbidden",
            "required_scope": "ai_config_write",
        }), 403
    if (
        request.endpoint in LEGACY_CONFIG_WRITE_ENDPOINTS
        and not {"app", "ai_config_write", "tenant_admin"}.intersection(token.scopes)
    ):
        return jsonify({
            "error": "forbidden",
            "required_scope": "tenant_admin",
        }), 403
    if (
        request.endpoint in APP_SETTINGS_WRITE_ENDPOINTS
        and not {"app", "tenant_admin"}.intersection(token.scopes)
    ):
        return jsonify({
            "error": "forbidden",
            "required_scope": "app",
        }), 403
    if request.endpoint == "web_session":
        return None
    return _rate_limit_response(g.auth_principal)


@app.after_request
def _security_headers(response):
    """统一安全响应头。CORS 保持关闭：App 是原生客户端，不需要跨域头。"""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    if request.endpoint == "api_pdf":
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    else:
        response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    # admin_web 是本地静态页，只用同源资源；给一个保守的 CSP。
    if request.endpoint == "api_pdf":
        response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
    else:
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "object-src 'self'; frame-src 'self'; frame-ancestors 'none'",
        )
    if request.endpoint == "user_web":
        if request.path == "/user/" or request.path.endswith("/index.html"):
            response.headers["Cache-Control"] = "no-store"
        elif "/user/assets/" in request.path:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    if getattr(g, "clear_web_session_cookies", False):
        _clear_web_cookies(response)
    # 经 https（隧道）访问时启用 HSTS；本机 http 不设，避免污染 loopback。
    if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


@app.teardown_request
def _clear_tenant_context(_error=None):
    token = getattr(g, "tenant_context_reset_token", None)
    if token is not None:
        reset_current_tenant_id(token)
        g.tenant_context_reset_token = None


@app.route("/healthz")
def healthz():
    # 未认证公开端点：只暴露存活状态，不泄露 tenant_id、通知渠道等内部信息。
    return jsonify({"ok": True})


@app.route("/")
def index():
    # 公开端点：仅返回服务标识，不列举内部路由结构。
    return jsonify({"ok": True, "service": "RssAiPush backend"})


@app.route("/admin/")
@app.route("/admin/<path:filename>")
def admin_web(filename="index.html"):
    if not filename or filename == "/":
        filename = "index.html"
    return send_from_directory(ADMIN_STATIC_DIR, filename)


@app.route("/user")
@app.route("/user/")
@app.route("/user/<path:filename>")
def user_web(filename="index.html"):
    if request.path == "/user":
        return redirect("/user/", code=308)
    requested = filename or "index.html"
    candidate = USER_STATIC_DIR / requested
    if requested != "index.html" and candidate.is_file():
        return send_from_directory(USER_STATIC_DIR, requested)
    if not (USER_STATIC_DIR / "index.html").is_file():
        return jsonify({"error": "user_frontend_not_built"}), 503
    return send_from_directory(USER_STATIC_DIR, "index.html")


def _web_session_payload(principal, *, expires_at):
    tenant = _registry().get_tenant(principal.tenant_id)
    return {
        "tenant_id": principal.tenant_id,
        "display_name": tenant.display_name,
        "kind": principal.kind,
        "scopes": sorted(principal.scopes),
        "expires_at": int(expires_at),
    }


@app.route("/api/web/session", methods=["GET", "POST", "DELETE"])
def web_session():
    principal = g.auth_principal
    if principal.kind != "tenant" or not principal.token_id:
        return jsonify({"error": "forbidden"}), 403

    if request.method == "POST":
        _registry().purge_expired_web_sessions()
        issued = _registry().create_web_session(
            principal.token_id,
            lifetime_seconds=WEB_SESSION_LIFETIME_SECONDS,
        )
        response = jsonify({
            "ok": True,
            "principal": _web_session_payload(
                principal,
                expires_at=issued.record.expires_at,
            ),
        })
        response.set_cookie(
            WEB_SESSION_COOKIE,
            issued.session_token,
            max_age=WEB_SESSION_LIFETIME_SECONDS,
            secure=_cookie_secure(),
            httponly=True,
            samesite="Strict",
            path="/",
        )
        response.set_cookie(
            WEB_CSRF_COOKIE,
            issued.csrf_token,
            max_age=WEB_SESSION_LIFETIME_SECONDS,
            secure=_cookie_secure(),
            httponly=False,
            samesite="Strict",
            path="/",
        )
        return response

    current = g.web_session
    if current is None:
        return jsonify({"error": "unauthorized"}), 401
    if request.method == "DELETE":
        _registry().revoke_web_session(current.id)
        return _clear_web_cookies(jsonify({"ok": True}))
    return jsonify({
        "ok": True,
        "principal": _web_session_payload(
            principal,
            expires_at=current.expires_at,
        ),
    })


@app.route("/api/admin/session", methods=["POST"])
def admin_session():
    principal = g.auth_principal
    return jsonify({
        "ok": True,
        "tenant_id": principal.tenant_id,
        "kind": principal.kind,
        "scopes": sorted(principal.scopes),
    })


@app.route("/api/auth/me")
def api_auth_me():
    principal = g.auth_principal
    return jsonify({
        "tenant_id": principal.tenant_id,
        "kind": principal.kind,
        "scopes": sorted(principal.scopes),
    })


# ── Config API ──────────────────────────────────────────

# 每个 section 允许租户写入的字段（依据 config.example.json）。
# section 外的键（server/pc）与 section 内的未知键一律丢弃，避免租户篡改
# 服务器级配置或注入无效键。rss.opml_path 刻意排除：路径由服务器按租户固定，
# get_opml_path 也忽略配置值，禁止客户端写入。
_CONFIG_ALLOWED_FIELDS = {
    "ai": frozenset(
        {"api_key", "base_url", "model", "system_prompt", "rss_prompt", "pdf_prompt"}
    ),
    "rss": frozenset(
        {
            "per_feed_limit",
            "max_push_items",
            "lookback_days",
            "fetch_original_abstract",
            "anthropic_web_fetch_enabled",
            "interest_score_threshold",
            "preference_weights",
        }
    ),
    "schedule": frozenset(
        {
            "rss_discovery_interval_minutes",
            "rss_interval_minutes",
            "pdf_interval_minutes",
            "enabled",
        }
    ),
}
_AI_CONNECTION_FIELDS = frozenset({"api_key", "base_url", "model"})
_AI_PROMPT_FIELDS = frozenset({"system_prompt", "rss_prompt", "pdf_prompt"})
# 正整数字段的上界，防止荒谬取值拖垮抓取/调度。
_CONFIG_INT_LIMITS = {
    "per_feed_limit": (1, 100),
    "max_push_items": (1, 500),
    "lookback_days": (1, 365),
    "rss_discovery_interval_minutes": (15, 1440),
    "rss_interval_minutes": (1, 1440),
    "pdf_interval_minutes": (1, 1440),
}


def _coerce_config_int(key, value):
    """校验正整数字段；返回 int 或抛 ValueError。"""
    if isinstance(value, bool):
        raise ValueError(f"{key} 必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} 必须是整数")
    low, high = _CONFIG_INT_LIMITS[key]
    if number < low or number > high:
        raise ValueError(f"{key} 必须在 {low} 到 {high} 之间")
    return number


def _apply_recommendation_settings(incoming):
    cfg = tasks.load_config()
    old_preference_weights = tasks._preference_weights(cfg)
    cfg.setdefault("rss", {}).update(incoming)
    tasks.save_config(cfg)
    new_preference_weights = tasks._preference_weights(cfg)
    if new_preference_weights != old_preference_weights:
        tasks.recalculate_preference_weights(new_preference_weights, mark_dirty=True)
        if tasks._interest_profile_state()["version"] > 0:
            tasks.schedule_interest_profile_refresh(force=True)


@app.route("/api/config", methods=["GET"])
def api_config():
    cfg = tasks.load_config()
    return jsonify(tasks.public_config(cfg))


@app.route("/api/config", methods=["POST"])
def api_save_config():
    data = request.get_json(silent=True) or {}
    principal = g.auth_principal
    limited_tenant_writer = (
        principal.kind == "tenant"
        and "tenant_admin" not in principal.scopes
    )
    if limited_tenant_writer:
        if (
            set(data) == {"schedule"}
            and isinstance(data.get("schedule"), dict)
            and "app" in principal.scopes
        ):
            incoming, error = _validate_schedule_settings(data["schedule"])
            if error:
                return jsonify({"error": error}), 400
            cfg = tasks.load_config()
            cfg.setdefault("schedule", {}).update(incoming)
            tasks.save_config(cfg)
            _wake_scheduler()
            return jsonify({"ok": True, "compatibility_mode": True})

        if (
            set(data) == {"rss"}
            and isinstance(data.get("rss"), dict)
            and "app" in principal.scopes
        ):
            incoming, error = _validate_recommendation_settings(data["rss"])
            if error:
                return jsonify({"error": error}), 400
            _apply_recommendation_settings(incoming)
            return jsonify({"ok": True, "compatibility_mode": True})

        if (
            set(data) != {"ai"}
            or not isinstance(data.get("ai"), dict)
            or "ai_config_write" not in principal.scopes
        ):
            return jsonify({
                "error": "forbidden",
                "required_scope": "tenant_admin",
            }), 403
        ai_payload = dict(data["ai"])
        allowed_ai_payload = {
            key: ai_payload[key]
            for key in _AI_CONNECTION_FIELDS | _AI_PROMPT_FIELDS
            if key in ai_payload
        }
        if not allowed_ai_payload:
            return jsonify({
                "error": "forbidden",
                "required_scope": "tenant_admin",
            }), 403
        connection_payload = {
            key: value
            for key, value in allowed_ai_payload.items()
            if key in _AI_CONNECTION_FIELDS
        }
        incoming = {}
        if connection_payload:
            incoming, error = _validate_ai_connection_payload(
                connection_payload,
                reject_unknown=True,
            )
            if error:
                return jsonify({"error": error}), 400
        incoming.update({
            key: value
            for key, value in allowed_ai_payload.items()
            if key in _AI_PROMPT_FIELDS
        })
        cfg = tasks.load_config()
        if incoming:
            cfg.setdefault("ai", {}).update(incoming)
            tasks.save_config(cfg)
            tasks.record_event(
                "settings",
                "AI 配置已通过兼容接口更新",
                details={
                    "fields": sorted(incoming),
                    "ignored_fields": sorted(set(ai_payload) - set(allowed_ai_payload)),
                },
            )
        return jsonify({
            "ok": True,
            "compatibility_mode": True,
            "ignored_fields": sorted(set(ai_payload) - set(allowed_ai_payload)),
        })

    cfg = tasks.load_config()
    old_preference_weights = tasks._preference_weights(cfg)
    for section in ("ai", "rss", "schedule"):
        if section not in data:
            continue
        allowed = _CONFIG_ALLOWED_FIELDS[section]
        # 逐 key 过滤：只保留白名单字段，其余静默丢弃（兼容旧客户端）。
        incoming = {
            k: v for k, v in dict(data[section] or {}).items() if k in allowed
        }
        for int_key in _CONFIG_INT_LIMITS:
            if int_key in incoming:
                try:
                    incoming[int_key] = _coerce_config_int(int_key, incoming[int_key])
                except ValueError as e:
                    return jsonify({"error": str(e)}), 400
        if section == "rss" and "interest_score_threshold" in incoming:
            try:
                threshold = float(incoming["interest_score_threshold"])
            except (TypeError, ValueError):
                return jsonify({"error": "interest_score_threshold 必须是0到100的数字"}), 400
            if threshold < 0 or threshold > 100:
                return jsonify({"error": "interest_score_threshold 必须在0到100之间"}), 400
            incoming["interest_score_threshold"] = round(threshold, 2)
        if section == "rss" and "fetch_original_abstract" in incoming:
            if not isinstance(incoming["fetch_original_abstract"], bool):
                return jsonify({"error": "fetch_original_abstract 必须是布尔值"}), 400
        if section == "rss" and "anthropic_web_fetch_enabled" in incoming:
            if not isinstance(incoming["anthropic_web_fetch_enabled"], bool):
                return jsonify({"error": "anthropic_web_fetch_enabled 必须是布尔值"}), 400
        if section == "rss" and "preference_weights" in incoming:
            try:
                incoming["preference_weights"] = tasks.validate_preference_weights(
                    incoming["preference_weights"]
                )
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
        if section == "ai":
            if "base_url" in incoming:
                candidate = str(incoming.get("base_url") or "").strip().rstrip("/")
                try:
                    assert_safe_outbound_url(candidate)
                except UnsafeOutboundURLError:
                    return jsonify({
                        "error": "base_url 不能指向内网、回环或元数据地址"
                    }), 400
                incoming["base_url"] = candidate
            secret_key = "api_key"
            secret = str(incoming.get(secret_key, "")).strip()
            if not secret or secret == tasks.MASKED_SECRET or set(secret) == {"*"}:
                incoming.pop(secret_key, None)
        if incoming:
            cfg.setdefault(section, {}).update(incoming)
    tasks.save_config(cfg)
    new_preference_weights = tasks._preference_weights(cfg)
    if new_preference_weights != old_preference_weights:
        tasks.recalculate_preference_weights(new_preference_weights, mark_dirty=True)
        if tasks._interest_profile_state()["version"] > 0:
            tasks.schedule_interest_profile_refresh(force=True)
    if "schedule" in data:
        _wake_scheduler()
    return jsonify({"ok": True})


def _validate_schedule_settings(data):
    if not isinstance(data, dict) or not data:
        return None, "请求体必须是非空 JSON 对象"
    allowed = _CONFIG_ALLOWED_FIELDS["schedule"]
    unknown = sorted(set(data) - allowed)
    if unknown:
        return None, f"不允许修改字段: {', '.join(unknown)}"

    incoming = {}
    for key, value in data.items():
        if key in _CONFIG_INT_LIMITS:
            try:
                incoming[key] = _coerce_config_int(key, value)
            except ValueError as exc:
                return None, str(exc)
        elif key == "enabled":
            if not isinstance(value, bool):
                return None, "enabled 必须是布尔值"
            incoming[key] = value
    return incoming, None


def _validate_recommendation_settings(data):
    if not isinstance(data, dict) or not data:
        return None, "请求体必须是非空 JSON 对象"
    allowed = {"interest_score_threshold", "preference_weights"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        return None, f"不允许修改字段: {', '.join(unknown)}"
    incoming = {}
    if "interest_score_threshold" in data:
        try:
            threshold = float(data["interest_score_threshold"])
        except (TypeError, ValueError):
            return None, "interest_score_threshold 必须是0到100的数字"
        if threshold < 0 or threshold > 100:
            return None, "interest_score_threshold 必须在0到100之间"
        incoming["interest_score_threshold"] = round(threshold, 2)
    if "preference_weights" in data:
        try:
            incoming["preference_weights"] = tasks.validate_preference_weights(
                data["preference_weights"]
            )
        except ValueError as exc:
            return None, str(exc)
    return incoming, None


@app.route("/api/settings/schedule", methods=["PATCH"])
def api_save_schedule_settings():
    """Allow the mobile app to update its tenant-local scheduler settings.

    This deliberately stays separate from the legacy full-config endpoint so an
    ``app`` token does not gain access to AI secrets, prompts, or destructive
    tenant administration.
    """
    incoming, error = _validate_schedule_settings(request.get_json(silent=True))
    if error:
        return jsonify({"error": error}), 400

    cfg = tasks.load_config()
    cfg.setdefault("schedule", {}).update(incoming)
    tasks.save_config(cfg)
    _wake_scheduler()
    return jsonify({"ok": True})


@app.route("/api/settings/recommendation", methods=["PATCH"])
def api_save_recommendation_settings():
    """Allow an app token to update tenant-local recommendation preferences."""
    incoming, error = _validate_recommendation_settings(
        request.get_json(silent=True)
    )
    if error:
        return jsonify({"error": error}), 400

    _apply_recommendation_settings(incoming)
    return jsonify({"ok": True})


def _public_ai_connection(config=None):
    ai = dict((config if config is not None else tasks.load_config()).get("ai") or {})
    key = str(ai.get("api_key") or "").strip()
    return {
        "api_key": tasks.MASKED_SECRET if key else "",
        "base_url": str(ai.get("base_url") or ""),
        "model": str(ai.get("model") or ""),
    }


def _validate_ai_connection_payload(data, *, reject_unknown):
    unknown = sorted(set(data) - _AI_CONNECTION_FIELDS)
    if reject_unknown and unknown:
        return None, f"不允许修改字段: {', '.join(unknown)}"
    if not data:
        return None, "至少提供一个 AI 配置字段"

    incoming = {}
    if "api_key" in data:
        api_key = str(data.get("api_key") or "").strip()
        if len(api_key) > 4096:
            return None, "api_key 过长"
        if api_key != tasks.MASKED_SECRET and set(api_key) != {"*"}:
            incoming["api_key"] = api_key
    if "base_url" in data:
        base_url = str(data.get("base_url") or "").strip().rstrip("/")
        parsed = urlsplit(base_url)
        if (
            not base_url
            or len(base_url) > 500
            or parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None, "base_url 必须是有效的 http/https URL"
        try:
            assert_safe_outbound_url(base_url)
        except UnsafeOutboundURLError:
            # 不回显解析细节，避免把内网探测结果反馈给调用方。
            return None, "base_url 不能指向内网、回环或元数据地址"
        incoming["base_url"] = base_url
    if "model" in data:
        model = str(data.get("model") or "").strip()
        if not model or len(model) > 200:
            return None, "model 不能为空且不能超过 200 个字符"
        incoming["model"] = model
    return incoming, None


@app.route("/api/ai-config", methods=["GET", "PATCH"])
def api_ai_config():
    if request.method == "GET":
        return jsonify(_public_ai_connection())

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400
    incoming, error = _validate_ai_connection_payload(
        data,
        reject_unknown=True,
    )
    if error:
        return jsonify({"error": error}), 400

    cfg = tasks.load_config()
    if incoming:
        cfg.setdefault("ai", {}).update(incoming)
        tasks.save_config(cfg)
        tasks.record_event(
            "settings",
            "AI 连接配置已更新",
            details={"fields": sorted(incoming)},
        )
    return jsonify({"ok": True, "ai": _public_ai_connection(cfg)})


@app.route("/api/ai-config/test", methods=["POST"])
def api_test_ai_config():
    """测试当前表单中的 AI 连接参数，不修改租户已保存的配置。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400
    incoming, error = _validate_ai_connection_payload(
        data,
        reject_unknown=True,
    )
    if error:
        return jsonify({"error": error}), 400

    ai = dict(tasks.load_config().get("ai") or {})
    ai.update(incoming)
    api_key = str(ai.get("api_key") or "").strip()
    base_url = str(ai.get("base_url") or "").strip()
    model = str(ai.get("model") or "").strip()
    if not api_key:
        return jsonify({"ok": False, "message": "请填写 API Key"})
    if not base_url:
        return jsonify({"ok": False, "message": "请填写 Base URL"})
    if not model:
        return jsonify({"ok": False, "message": "请填写 Model"})

    try:
        tasks.test_ai_connection(api_key, base_url, model)
    except Exception as exc:
        detail = redact_sensitive_text(str(exc)).strip()[:500]
        logger.warning("AI API 测试失败: %s", detail)
        return jsonify({
            "ok": False,
            "message": f"AI API 测试失败：{detail or '未知错误'}",
        })
    return jsonify({"ok": True, "message": "AI API 测试成功"})


# ── Feed API ────────────────────────────────────────────

@app.route("/api/feeds", methods=["GET"])
def get_feeds():
    cfg = tasks.load_config()
    opml = tasks.get_opml_path(cfg)
    feeds = tasks.parse_opml(opml)
    return jsonify(feeds)


@app.route("/api/feeds", methods=["POST"])
def add_feed():
    data = request.get_json(silent=True) or request.form
    title = str(data.get("title") or "").strip()
    url = str(data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "缺少 url"}), 400
    if not title:
        title = urlsplit(url).hostname or url

    cfg = tasks.load_config()
    opml = tasks.get_opml_path(cfg)
    tasks.add_feed_to_opml(opml, title, url)
    _trigger_feed_union_refresh()
    return jsonify({"ok": True})


@app.route("/api/feeds", methods=["PATCH"])
def update_feed():
    data = request.get_json(silent=True) or request.form
    old_url = str(data.get("old_url") or "").strip()
    title = str(data.get("title") or "").strip()
    url = str(data.get("url") or "").strip()
    if not old_url:
        return jsonify({"error": "缺少 old_url"}), 400
    if not url:
        return jsonify({"error": "缺少 url"}), 400
    if not title:
        title = urlsplit(url).hostname or url

    cfg = tasks.load_config()
    opml = tasks.get_opml_path(cfg)
    try:
        updated = tasks.update_feed_in_opml(opml, old_url, title, url)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not updated:
        return jsonify({"error": "RSS 源不存在"}), 404
    _trigger_feed_union_refresh()
    return jsonify({"ok": True, "count": len(tasks.parse_opml(opml))})


@app.route("/api/feeds/<path:url>", methods=["DELETE"])
def delete_feed(url):
    cfg = tasks.load_config()
    opml = tasks.get_opml_path(cfg)
    tasks.remove_feed_from_opml(opml, url)
    _trigger_feed_union_refresh()
    return jsonify({"ok": True})


@app.route("/api/feeds", methods=["DELETE"])
def delete_feed_query():
    """Delete a feed without embedding its complete URL in a URL path.

    The path form remains above for old clients. The query form avoids encoded
    slash/question-mark handling differences across Retrofit, nginx, and Flask.
    """
    url = (request.args.get("url") or "").strip()
    if not url:
        return jsonify({"error": "缺少 url"}), 400
    cfg = tasks.load_config()
    opml = tasks.get_opml_path(cfg)
    tasks.remove_feed_from_opml(opml, url)
    _trigger_feed_union_refresh()
    return jsonify({"ok": True})


@app.route("/api/feeds/import", methods=["POST"])
def import_opml():
    if "file" not in request.files:
        return jsonify({"error": "没有上传文件"}), 400
    f = request.files["file"]
    cfg = tasks.load_config()
    opml = tasks.get_opml_path(cfg)
    f.save(opml)
    _trigger_feed_union_refresh()
    return jsonify({"ok": True, "count": len(tasks.parse_opml(opml))})


# ── Manual trigger ──────────────────────────────────────

def _submit_manual_job(job_type, *, busy_job_types=(), run_as_tenant=None):
    coordinator = _task_coordinator()
    if coordinator is None:
        return jsonify({"error": "任务服务尚未启动"}), 503
    # 共享消化必须以 owner 身份运行（才用 owner Key），可由 run_as_tenant 覆盖。
    tenant_id = run_as_tenant or get_current_tenant_id()
    running = coordinator.snapshot(tenant_id)["running"]
    if any(running.get(key) for key in busy_job_types):
        return jsonify({"error": f"{job_type} 任务正在运行中"}), 409
    result = coordinator.submit(
        tenant_id,
        job_type,
        trigger_source="manual_api",
    )
    if result.accepted:
        return jsonify({
            "ok": True,
            "message": f"{job_type} 任务已进入队列",
            "request_id": result.request_id,
        }), 202
    if result.reason == "duplicate":
        return jsonify({"error": f"{job_type} 任务正在运行中"}), 409
    if result.reason == "queue_full":
        response = jsonify({"error": "任务队列已满，请稍后重试"})
        response.headers["Retry-After"] = str(
            max(1, int(getattr(coordinator, "scan_interval", 30)))
        )
        return response, 429
    return jsonify({"error": "任务服务正在关闭"}), 503


@app.route("/api/run/rss", methods=["POST"])
def run_rss():
    # 手动刷新：先给当前租户从共享缓存投递，同时触发 owner 抓取/消化新内容。
    coordinator = _task_coordinator()
    if coordinator is not None:
        coordinator.submit(
            OWNER_TENANT_ID, "shared_ingest", trigger_source="manual_api"
        )
    return _submit_manual_job(
        "rss_deliver",
        busy_job_types=("rss_deliver",),
    )


@app.route("/api/run/pdf", methods=["POST"])
def run_pdf():
    return _submit_manual_job("pdf", busy_job_types=("pdf",))


@app.route("/api/admin/run/rss-discovery", methods=["POST"])
def run_rss_discovery():
    # 运营者手动触发共享消化，始终以 owner 身份运行（才用 owner Key）。
    return _submit_manual_job(
        "shared_ingest",
        busy_job_types=("shared_ingest",),
        run_as_tenant=OWNER_TENANT_ID,
    )


@app.route("/api/admin/run/rss-publish", methods=["POST"])
def run_rss_publish():
    # 手动触发把共享缓存投递给当前租户。
    return _submit_manual_job(
        "rss_deliver",
        busy_job_types=("rss_deliver",),
    )


@app.route("/api/pdf/upload", methods=["POST"])
def upload_pdf():
    files = request.files.getlist("files")
    if not files and "file" in request.files:
        files = [request.files["file"]]
    if not files:
        return jsonify({"error": "没有上传 PDF 文件"}), 400

    saved = []
    errors = []
    for f in files:
        try:
            saved_path = tasks.save_uploaded_pdf(f)
            saved.append(Path(saved_path).name)
        except Exception as e:
            errors.append({"filename": getattr(f, "filename", ""), "error": str(e)})
    app.logger.info(
        "PDF 上传: tenant=%s saved=%s errors=%s paths=%s",
        get_current_tenant_id(),
        len(saved),
        len(errors),
        saved,
    )
    return jsonify({
        "ok": bool(saved),
        "uploaded": len(saved),
        "paths": saved,
        "errors": errors,
    })


@app.route("/api/pdf/upload-chunk", methods=["POST"])
def upload_pdf_chunk():
    chunk = request.files.get("chunk") or request.files.get("file")
    filename = request.form.get("filename", "")
    if not chunk:
        return jsonify({"error": "没有上传 PDF 分片"}), 400
    try:
        result = tasks.save_uploaded_pdf_chunk(
            request.form.get("upload_id", ""),
            filename,
            request.form.get("index", "0"),
            request.form.get("total", "1"),
            chunk,
        )
        path = Path(result.get("path") or "").name if result.get("path") else ""
        uploaded = 1 if result.get("complete") and path else 0
        paths = [path] if path else []
        app.logger.info(
            "PDF 分片上传: tenant=%s filename=%s received=%s/%s next=%s "
            "complete=%s path=%s",
            get_current_tenant_id(),
            filename,
            result.get("received"),
            result.get("total"),
            result.get("next_index"),
            result.get("complete"),
            path,
        )
        return jsonify({
            "ok": True,
            "uploaded": uploaded,
            "paths": paths,
            "errors": [],
            "complete": bool(result.get("complete")),
            "received": result.get("received", 0),
            "total": result.get("total", 0),
            "next_index": result.get("next_index", 0),
        })
    except Exception as e:
        app.logger.warning("PDF 分片上传失败: filename=%s error=%s", filename, e)
        return jsonify({
            "ok": False,
            "uploaded": 0,
            "paths": [],
            "errors": [{"filename": filename, "error": str(e)}],
            "error": str(e),
        }), 400


@app.route("/api/progress")
def api_progress():
    return jsonify(_task_snapshot()["progress"])


@app.route("/api/status")
def api_status():
    status = tasks.get_status()
    status["pdf_upload_limits"] = {
        "request_bytes": MAX_REQUEST_BYTES,
        "chunk_bytes": tasks.MAX_UPLOAD_CHUNK_BYTES,
        "total_bytes": tasks.MAX_UPLOAD_TOTAL_BYTES,
    }
    return jsonify(status)


@app.route("/api/app/heartbeat", methods=["POST"])
def api_app_heartbeat():
    payload = request.get_json(silent=True) or {}
    heartbeat = tasks.record_app_heartbeat(payload)
    _trigger_connect_pipeline(get_current_tenant_id())
    return jsonify({"ok": True, "heartbeat": heartbeat})


@app.route("/api/admin/overview")
def api_admin_overview():
    state = _task_snapshot()
    return jsonify(tasks.get_admin_overview(
        progress=state["progress"],
        running=state["running"],
    ))


@app.route("/api/admin/events")
def api_admin_events():
    n = request.args.get("limit", 100, type=int)
    source = request.args.get("source") or None
    return jsonify(tasks.get_events(n, source=source))


@app.route("/api/admin/feed-health")
def api_admin_feed_health():
    return jsonify(tasks.get_feed_health())


@app.route("/api/admin/rss-probe", methods=["POST"])
def api_admin_rss_probe():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url") or "").strip()
    override = data.get("override_cooldown", False)
    if not url:
        return jsonify({"error": "缺少 url"}), 400
    if not isinstance(override, bool):
        return jsonify({"error": "override_cooldown 必须是布尔值"}), 400
    result = tasks.probe_shared_rss_feed(url, override_cooldown=override)
    status_code = int(result.pop("status_code", 200))
    response = jsonify(result)
    if result.get("retry_after"):
        response.headers["Retry-After"] = str(max(1, int(result["retry_after"])))
    return response, status_code


@app.route("/api/admin/rss-queue")
def api_admin_rss_queue():
    status = request.args.get("status") or None
    n = request.args.get("limit", 100, type=int)
    return jsonify(tasks.get_rss_queue(status=status, limit=n))


@app.route("/api/admin/pdf-queue")
def api_admin_pdf_queue():
    n = request.args.get("limit", 100, type=int)
    return jsonify(tasks.get_pdf_queue(limit=n))


@app.route("/api/admin/local-settings", methods=["GET"])
def api_admin_local_settings():
    return jsonify(tasks.get_local_settings())


@app.route("/api/admin/local-settings", methods=["POST"])
def api_admin_save_local_settings():
    data = request.get_json(silent=True) or {}
    return jsonify({"ok": True, "settings": tasks.save_local_settings(data)})


@app.route("/api/admin/tunnel/refresh", methods=["POST"])
def api_admin_tunnel_refresh():
    return jsonify(tasks.request_tunnel_refresh())


@app.route("/api/admin/runtime/<command>", methods=["POST"])
def api_admin_runtime_command(command):
    try:
        return jsonify(tasks.request_admin_command(command))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/admin/settings", methods=["GET"])
def api_admin_settings():
    return jsonify(tasks.get_admin_settings())


@app.route("/api/admin/settings", methods=["POST"])
def api_admin_save_settings():
    data = request.get_json(silent=True) or {}
    try:
        settings = tasks.save_admin_settings(data)
        if "schedule" in data:
            _wake_scheduler()
        return jsonify({"ok": True, "settings": settings})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


# ── Tenant administration (operator-only) ──────────────────
# 这些端点都在 OPERATOR_ENDPOINTS 中，只有本机 loopback 的 operator 凭据能访问；
# 隧道过来的租户 token 在 _require_auth 里一律 403，因此租户无法自助增删租户。

def _epoch_to_str(value):
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return ""


def _token_public(token):
    return {
        "id": token.id,
        "tenant_id": token.tenant_id,
        "prefix": token.token_prefix,
        "scopes": list(token.scopes),
        "status": token.status,
        "created_at": _epoch_to_str(token.created_at),
        "last_used_at": _epoch_to_str(token.last_used_at),
        "expires_at": _epoch_to_str(token.expires_at),
        "revoked_at": _epoch_to_str(token.revoked_at),
    }


def _tenant_public(tenant, tokens=None):
    active_tokens = (
        sum(1 for t in tokens if t.status == "active") if tokens is not None else None
    )
    return {
        "id": tenant.id,
        "display_name": tenant.display_name,
        "status": tenant.status.value,
        "created_at": _epoch_to_str(tenant.created_at),
        "updated_at": _epoch_to_str(tenant.updated_at),
        "token_count": len(tokens) if tokens is not None else None,
        "active_token_count": active_tokens,
        "is_owner": tenant.id == OWNER_TENANT_ID,
    }


def _validate_scopes(raw_scopes):
    """规范化并校验 scope 列表；空则默认 ('app',)。"""
    if raw_scopes is None:
        return ("app",)
    if not isinstance(raw_scopes, (list, tuple)):
        raise ValueError("scopes 必须是数组")
    scopes = tuple(sorted({str(s).strip() for s in raw_scopes if str(s).strip()}))
    if not scopes:
        return ("app",)
    invalid = [s for s in scopes if s not in VALID_TOKEN_SCOPES]
    if invalid:
        raise ValueError(f"不支持的 scope: {', '.join(invalid)}")
    return scopes


def _tenant_default_config():
    try:
        from manage import _default_tenant_config

        return _default_tenant_config()
    except Exception:
        return {}


@app.route("/api/admin/tenants", methods=["GET", "POST"])
def api_admin_tenants():
    registry = _registry()
    if request.method == "GET":
        tenants = []
        for tenant in registry.list_tenants():
            tokens = registry.list_tokens(tenant.id)
            tenants.append(_tenant_public(tenant, tokens))
        return jsonify({"tenants": tenants})

    data = request.get_json(silent=True) or {}
    name = str(data.get("display_name") or data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "display_name 不能为空"}), 400
    try:
        scopes = _validate_scopes(data.get("scopes"))
        tenant = registry.create_tenant(
            name,
            default_config=_tenant_default_config(),
            default_opml=read_default_opml(),
        )
        issued = registry.create_token(tenant.id, scopes=scopes)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    tasks.record_event(
        "tenant",
        f"新建租户 {tenant.id}",
        details={"display_name": name, "scopes": list(scopes)},
    )
    return jsonify({
        "ok": True,
        "tenant": _tenant_public(tenant, [issued.record]),
        # 明文 token 仅在此响应中出现一次，控制库只存哈希，无法再取回。
        "token": issued.token,
        "token_meta": _token_public(issued.record),
    }), 201


@app.route("/api/admin/tenants/<tenant_id>/delete", methods=["POST"])
def api_admin_tenant_delete(tenant_id):
    try:
        tenant = _registry().soft_delete_tenant(tenant_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except KeyError as e:
        return jsonify({"error": str(e)}), 404
    tasks.record_event("tenant", f"软删除租户 {tenant.id}", level="warning")
    return jsonify({"ok": True, "tenant": _tenant_public(tenant)})


@app.route("/api/admin/tenants/<tenant_id>/purge", methods=["POST"])
def api_admin_tenant_purge(tenant_id):
    try:
        archive = _registry().purge_tenant(
            tenant_id,
            backups_dir=SERVER_PATHS.control_backups_dir,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except KeyError as e:
        return jsonify({"error": str(e)}), 404
    tasks.record_event(
        "tenant",
        f"彻底删除租户 {tenant_id}",
        level="warning",
        details={"backup": str(archive)},
    )
    return jsonify({"ok": True, "backup_path": str(archive)})


@app.route("/api/admin/tenants/<tenant_id>/tokens", methods=["GET", "POST"])
def api_admin_tenant_tokens(tenant_id):
    registry = _registry()
    if request.method == "GET":
        try:
            tokens = registry.list_tokens(tenant_id)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"tokens": [_token_public(t) for t in tokens]})

    data = request.get_json(silent=True) or {}
    try:
        scopes = _validate_scopes(data.get("scopes"))
        issued = registry.create_token(tenant_id, scopes=scopes)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except KeyError as e:
        return jsonify({"error": str(e)}), 404
    tasks.record_event(
        "tenant",
        f"为租户 {tenant_id} 新建 token",
        details={"scopes": list(scopes)},
    )
    return jsonify({
        "ok": True,
        "token": issued.token,
        "token_meta": _token_public(issued.record),
    }), 201


@app.route("/api/admin/tenants/<tenant_id>/tokens/<token_id>/revoke", methods=["POST"])
def api_admin_tenant_token_revoke(tenant_id, token_id):
    try:
        token = _registry().revoke_token(token_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except KeyError as e:
        return jsonify({"error": str(e)}), 404
    tasks.record_event("tenant", f"撤销 token {token.id}", level="warning")
    return jsonify({"ok": True, "token": _token_public(token)})


@app.route("/api/admin/metrics")
def api_admin_metrics():
    metrics = tasks.get_performance_metrics()
    coordinator = _task_coordinator()
    if coordinator is not None:
        metrics["coordinator"] = {
            "max_workers": coordinator.max_workers,
            "max_pending": coordinator.max_pending,
            "scan_interval": coordinator.scan_interval,
        }
    try:
        metrics["tenant_count"] = len(_registry().list_tenants())
    except Exception:
        metrics["tenant_count"] = None
    return jsonify(metrics)


@app.route("/api/digests")
def api_digests():
    n = request.args.get("limit", type=int)
    source = request.args.get("source") or None
    recommendation = (request.args.get("recommendation") or "").strip().lower() or None
    if recommendation not in {None, "any", "ai", "explore"}:
        return jsonify({"error": "recommendation 必须是 any、ai 或 explore"}), 400
    # 分组模式展开某期刊时按分组键过滤；journal_group_key 显式传入（含空串="未标注期刊"）才生效。
    group_key = request.args.get("journal_group_key")
    interested_only = request.args.get("interested_only") in ("1", "true", "True")
    disliked_only = request.args.get("disliked_only") in ("1", "true", "True")
    exclude_disliked = request.args.get("exclude_disliked") in ("1", "true", "True")
    favorite_only = request.args.get("favorite_only") in ("1", "true", "True")
    if disliked_only and exclude_disliked:
        return jsonify({"error": "disliked_only 和 exclude_disliked 不能同时启用"}), 400
    return jsonify(tasks.get_recent_digests(
        n,
        source=source,
        recommendation=recommendation,
        journal_group_key=group_key,
        interested_only=interested_only,
        disliked_only=disliked_only,
        exclude_disliked=exclude_disliked,
        favorite_only=favorite_only,
    ))


@app.route("/api/digests/ai-search", methods=["POST"])
def api_ai_search_digests():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求体必须是 JSON 对象", "code": "invalid_request"}), 400
    query = data.get("query")
    if not isinstance(query, str):
        return jsonify({"error": "query 必须是字符串", "code": "invalid_query"}), 400
    query = query.strip()
    if not query or len(query) > 200:
        return jsonify({
            "error": "query 长度必须为 1-200 个字符",
            "code": "invalid_query",
        }), 400
    ai_rank = data.get("ai_rank", True)
    if not isinstance(ai_rank, bool):
        return jsonify({"error": "ai_rank 必须是布尔值", "code": "invalid_ai_rank"}), 400
    candidate_filenames = data.get("candidate_filenames") or []
    if not isinstance(candidate_filenames, list) or any(
        not isinstance(item, str) for item in candidate_filenames
    ):
        return jsonify({
            "error": "candidate_filenames 必须是字符串数组",
            "code": "invalid_candidates",
        }), 400
    try:
        return jsonify(tasks.ai_search_digests(
            query,
            candidate_filenames=candidate_filenames,
            ai_rank=ai_rank,
        ))
    except tasks.AiSearchUnavailableError as exc:
        return jsonify({"error": str(exc), "code": "ai_unavailable"}), 503
    except tasks.AiSearchFailedError as exc:
        return jsonify({"error": str(exc), "code": "ai_search_failed"}), 502


@app.route("/api/digests/stats")
def api_digest_stats():
    source = request.args.get("source") or None
    exclude_disliked = request.args.get("exclude_disliked") in ("1", "true", "True")
    favorite_only = request.args.get("favorite_only") in ("1", "true", "True")
    return jsonify(tasks.get_digest_stats(
        source=source,
        exclude_disliked=exclude_disliked,
        favorite_only=favorite_only,
    ))


@app.route("/api/digests/journal-stats")
def api_journal_stats():
    source = request.args.get("source") or None
    return jsonify(tasks.get_journal_stats(source=source))


@app.route("/api/digests/updates")
def api_digest_updates():
    after = request.args.get("after", 0, type=int)
    n = request.args.get("limit", 50, type=int)
    source = request.args.get("source") or None
    return jsonify(tasks.get_digest_updates(after=after, limit=n, source=source))


@app.route("/api/digests/<filename>/content")
def api_digest_content(filename):
    try:
        return jsonify(tasks.get_digest_content(filename))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except FileNotFoundError:
        return jsonify({"error": "摘要不存在"}), 404


@app.route("/api/digests/<filename>/flags", methods=["GET", "PATCH"])
def api_digest_flags(filename):
    try:
        if request.method == "GET":
            return jsonify(tasks.get_digest_flags(filename))

        data = request.get_json(silent=True) or {}
        for key in ("disliked", "interested", "is_read", "favorite"):
            if key in data and not isinstance(data[key], bool):
                return jsonify({"error": f"{key} 必须是布尔值"}), 400
        if data.get("disliked") is True and data.get("interested") is True:
            return jsonify({"error": "不喜欢和感兴趣不能同时启用"}), 400
        flags = tasks.update_digest_flags(
            filename,
            disliked=data.get("disliked"),
            interested=data.get("interested"),
            is_read=data.get("is_read"),
            favorite=data.get("favorite"),
        )
        return jsonify({"ok": True, **flags})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except FileNotFoundError:
        return jsonify({"error": "摘要不存在"}), 404


@app.route("/api/digests/<filename>", methods=["DELETE"])
def api_delete_digest(filename):
    try:
        tasks.delete_digest(filename)
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except FileNotFoundError:
        return jsonify({"error": "摘要不存在"}), 404


@app.route("/api/digests/<filename>/restore", methods=["POST"])
def api_restore_digest(filename):
    try:
        tasks.restore_digest(filename)
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except FileNotFoundError:
        return jsonify({"error": "摘要不存在"}), 404


@app.route("/api/digests/deleted", methods=["GET"])
def api_deleted_digests():
    limit = request.args.get("limit", type=int)
    return jsonify({"items": tasks.list_deleted_digests(limit)})


@app.route("/api/digests", methods=["DELETE"])
def api_clear_digests():
    source = request.args.get("source") or None
    count = tasks.clear_digests(source)
    return jsonify({"ok": True, "count": count})


@app.route("/api/admin/cleanup/<source>", methods=["POST"])
def api_admin_cleanup(source):
    source = (source or "").strip().lower()
    if source not in {"rss", "pdf"}:
        return jsonify({"error": "source 必须是 rss 或 pdf"}), 400

    busy_keys = ("rss_deliver", "shared_ingest") if source == "rss" else ("pdf",)
    running = _task_snapshot()["running"]
    if any(running.get(key) for key in busy_keys):
        return jsonify({"error": f"{source.upper()} 任务运行中，请稍后再清理"}), 409

    try:
        result = tasks.cleanup_source(source)
        return jsonify({"ok": True, **result})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("%s cleanup failed", source.upper())
        return jsonify({"error": f"清理失败: {e}"}), 500


@app.route("/api/admin/rss-reset-time", methods=["POST"])
def api_admin_rss_reset_time():
    running = _task_snapshot()["running"]
    if any(running.get(key) for key in ("rss_deliver", "shared_ingest")):
        return jsonify({"error": "RSS 任务运行中，请稍后再重置时间"}), 409
    data = request.get_json(silent=True) or {}
    try:
        result = tasks.reset_rss_fetch_time(data.get("lookback_days"))
        tasks.record_event("settings", "RSS 抓取时间已重置", details=result)
        return jsonify({"ok": True, **result})
    except Exception as e:
        logger.exception("RSS reset time failed")
        return jsonify({"error": f"重置失败: {e}"}), 500


@app.route("/api/reset", methods=["POST"])
def api_reset():
    # 删除所有摘要
    count = tasks.clear_digests()
    # 重置收录到最近一周
    tasks.reset_seen_to_recent_week()
    return jsonify({"ok": True, "count": count})


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "")
    message = data.get("message", "")
    history = data.get("history", []) or []
    history_summary = data.get("history_summary", "") or ""
    web_search = data.get("web_search", False)
    pdf_filename = data.get("pdf_filename", "") or ""
    if not filename or not message:
        return jsonify({"error": "缺少 filename 或 message"}), 400
    if not isinstance(history, list):
        return jsonify({"error": "history 必须是数组"}), 400
    if not isinstance(history_summary, str):
        return jsonify({"error": "history_summary 必须是字符串"}), 400
    if not isinstance(web_search, bool):
        return jsonify({"error": "web_search 必须是布尔值"}), 400
    if not isinstance(pdf_filename, str) or len(pdf_filename) > 255:
        return jsonify({"error": "pdf_filename 必须是有效文件名"}), 400
    result = tasks.ai_chat(
        filename,
        message,
        history,
        web_search=web_search,
        history_summary=history_summary,
        pdf_filename=pdf_filename,
    )
    return jsonify(result)


@app.route("/api/interest-profile", methods=["GET"])
def api_get_interest_profile():
    # 只读返回当前租户的兴趣画像；无画像时返回空对象（前端据此显示“暂无画像”）。
    return jsonify(tasks.get_interest_profile() or {})


@app.route("/api/interest-profile", methods=["PUT"])
def api_set_interest_profile():
    # 手动覆盖当前租户画像。前端当前只读，此端点供后续编辑能力使用。
    data = request.get_json(silent=True)
    profile_input = data.get("profile") if isinstance(data, dict) and "profile" in data else data
    try:
        result = tasks.set_interest_profile(profile_input)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.route("/api/pdf")
def api_pdf():
    filename = request.args.get("filename", "")
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return jsonify({"error": "非法文件名"}), 400
    pdf_path = tasks.resolve_pdf_path(filename)
    if not pdf_path:
        return jsonify({"error": "未找到对应的 PDF 源文件"}), 404
    return send_file(pdf_path, mimetype="application/pdf", as_attachment=False)


@app.route("/api/logs")
def api_logs():
    n = request.args.get("lines", 200, type=int)
    return jsonify(tasks.get_logs(n))


# ── Inbox 静态文件 ──────────────────────────────────────
# 摘要 HTML 由 tasks 写入 INBOX_DIR。早期版本依赖一个独立的静态服务器
# （http.server，端口 8765）来提供这些页面；现已并入 Flask，统一在 5000 端口
# 通过 /inbox/<filename> 访问，App 端也只需配置一个 host:port。
# send_from_directory 自带路径穿越防护，会拒绝 .. 等越界文件名。
@app.route("/inbox/")
@app.route("/inbox/<path:filename>")
def serve_inbox(filename="index.html"):
    return send_from_directory(tasks.INBOX_DIR, filename)


# ── Main ────────────────────────────────────────────────

if __name__ == "__main__":
    from serve import run

    run(app)
