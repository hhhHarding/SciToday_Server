"""Local operator CLI for the multi-tenant PC server foundation."""

from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
from pathlib import Path

from auth import write_operator_token
from server_config import ServerPaths
from tenancy.registry import TenantRegistry, read_default_opml


APP_ROOT = Path(__file__).resolve().parent


@contextmanager
def patch_server_paths(tasks_module, paths):
    """Point tasks at the CLI-resolved ServerPaths for the duration of a block.

    Needed because tasks.SERVER_PATHS is resolved from env at import time, which
    may not match a --data-dir override.
    """
    original = tasks_module.SERVER_PATHS
    tasks_module.SERVER_PATHS = paths
    try:
        yield
    finally:
        tasks_module.SERVER_PATHS = original


def _default_tenant_config() -> dict:
    path = APP_ROOT / "config.example.json"
    if not path.exists():
        return {}
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    config.setdefault("ai", {})["api_key"] = ""
    return config


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="RssAiPush 本机运营管理工具（不会启动 Web 服务）"
    )
    parser.add_argument(
        "--data-dir",
        help="覆盖 RSSAI_SERVER_DATA_DIR，仅影响本次命令",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化 control.db 和 owner 租户")

    tenant = sub.add_parser("tenant", help="租户管理")
    tenant_sub = tenant.add_subparsers(dest="tenant_command", required=True)
    create = tenant_sub.add_parser("create", help="创建租户")
    create.add_argument("--name", required=True, help="显示名称，不用作目录名")
    create.add_argument("--id", dest="tenant_id", help="可选内部 ID；默认安全随机生成")
    tenant_sub.add_parser("list", help="列出租户")

    token = sub.add_parser("token", help="租户 token 管理")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    token_create = token_sub.add_parser("create", help="生成一次性显示的租户 token")
    token_create.add_argument("tenant_id")
    token_create.add_argument(
        "--scope",
        action="append",
        choices=("app", "ai_config_write", "tenant_admin"),
        dest="scopes",
        help="可重复；默认仅 app",
    )
    token_list = token_sub.add_parser("list", help="列出 token 安全元数据")
    token_list.add_argument("tenant_id")
    token_revoke = token_sub.add_parser("revoke", help="撤销 token")
    token_revoke.add_argument("token_id")
    token_scopes = token_sub.add_parser(
        "scopes",
        help="替换现有 token 的 scopes，不轮换 token",
    )
    token_scopes.add_argument("token_id")
    token_scopes.add_argument(
        "--scope",
        action="append",
        choices=("app", "ai_config_write", "tenant_admin"),
        dest="scopes",
        required=True,
    )

    operator = sub.add_parser("operator", help="本机 operator 凭据")
    operator_sub = operator.add_subparsers(dest="operator_command", required=True)
    operator_create = operator_sub.add_parser(
        "create",
        help="生成受本机文件保护的 operator token",
    )
    operator_create.add_argument("--file", type=Path, help="覆盖默认 secret 文件路径")
    operator_create.add_argument("--force", action="store_true", help="覆盖已有 secret")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    paths = ServerPaths(args.data_dir) if args.data_dir else ServerPaths.from_env()
    registry = TenantRegistry(paths)

    if args.command == "init":
        paths.ensure_global_directories()
        version = registry.initialize()
        owner = registry.ensure_owner(default_config=_default_tenant_config())
        # 初始化跨租户共享内容缓存（建目录 + 迁移 content.db，幂等）。
        import tasks

        with patch_server_paths(tasks, paths):
            con = tasks._shared_content_db()
            con.close()
        print(f"control.db schema: {version}")
        print(f"server data root: {paths.data_root}")
        print(f"shared cache: {paths.shared_content_db}")
        print(f"owner: {owner.id} ({owner.status.value})")
        return 0

    if args.command == "tenant" and args.tenant_command == "create":
        tenant = registry.create_tenant(
            args.name,
            tenant_id=args.tenant_id,
            default_config=_default_tenant_config(),
            default_opml=read_default_opml(),
        )
        print(f"tenant created: {tenant.id}")
        print(f"display name: {tenant.display_name}")
        print(f"status: {tenant.status.value}")
        return 0

    if args.command == "tenant" and args.tenant_command == "list":
        registry.initialize()
        tenants = registry.list_tenants()
        if not tenants:
            print("no tenants")
            return 0
        for tenant in tenants:
            print(f"{tenant.id}\t{tenant.status.value}\t{tenant.display_name}")
        return 0

    if args.command == "token" and args.token_command == "create":
        issued = registry.create_token(
            args.tenant_id,
            scopes=args.scopes or ("app",),
        )
        print(f"token id: {issued.record.id}")
        print(f"tenant: {issued.record.tenant_id}")
        print(f"scopes: {','.join(issued.record.scopes)}")
        print("token（仅显示一次，请立即保存）:")
        print(issued.token)
        return 0

    if args.command == "token" and args.token_command == "list":
        tokens = registry.list_tokens(args.tenant_id)
        if not tokens:
            print("no tokens")
            return 0
        for token in tokens:
            print(
                f"{token.id}\t{token.status}\t{token.token_prefix}\t"
                f"{','.join(token.scopes)}"
            )
        return 0

    if args.command == "token" and args.token_command == "revoke":
        token = registry.revoke_token(args.token_id)
        print(f"token revoked: {token.id}")
        return 0

    if args.command == "token" and args.token_command == "scopes":
        token = registry.set_token_scopes(args.token_id, args.scopes)
        print(f"token scopes updated: {token.id}")
        print(f"tenant: {token.tenant_id}")
        print(f"scopes: {','.join(token.scopes)}")
        return 0

    if args.command == "operator" and args.operator_command == "create":
        plaintext, path = write_operator_token(
            paths,
            path=args.file,
            force=args.force,
        )
        print(f"operator token file: {path}")
        print("operator token（仅显示一次，请立即保存）:")
        print(plaintext)
        return 0

    raise AssertionError("unhandled command")


if __name__ == "__main__":
    raise SystemExit(main())
