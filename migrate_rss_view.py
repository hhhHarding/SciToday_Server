"""One-time migration: make the shared cache the only RSS source.

After switching to the shared-cache pipeline, each tenant's RSS view is driven by
delivery from the shared cache. This script removes each tenant's *old*
per-tenant ``source='rss'`` digests (HTML files + digest_db rows) so the RSS view
starts clean. PDF-sourced digests are never touched.

Dry-run by default: prints per-tenant counts and changes nothing. Pass --commit
to actually delete. Always back up the data root before running with --commit.
"""

from __future__ import annotations

import argparse
import sys

from server_config import ServerPaths
from tenancy.context import tenant_context
from tenancy.registry import TenantRegistry


def _rss_digest_filenames(tasks) -> list[str]:
    tasks._sync_digest_index()
    con = tasks._digest_db()
    try:
        rows = con.execute(
            "SELECT filename FROM digests WHERE source='rss'"
        ).fetchall()
    finally:
        con.close()
    return [row[0] for row in rows]


def migrate(data_dir: str | None = None, *, commit: bool = False) -> int:
    paths = ServerPaths(data_dir) if data_dir else ServerPaths.from_env()
    registry = TenantRegistry(paths)
    registry.initialize()

    # Import after ServerPaths is known; align tasks to this data root.
    import tasks

    tasks.SERVER_PATHS = paths
    tasks._reset_config_cache_for_tests()
    tasks._reset_migration_cache_for_tests()

    total_removed = 0
    for tenant in registry.list_tenants():
        with tenant_context(tenant.id):
            filenames = _rss_digest_filenames(tasks)
            pdf_kept = 0
            con = tasks._digest_db()
            try:
                pdf_kept = con.execute(
                    "SELECT COUNT(*) FROM digests WHERE source<>'rss'"
                ).fetchone()[0]
            finally:
                con.close()
            action = "would delete" if not commit else "deleting"
            print(
                f"{tenant.id}: {action} {len(filenames)} rss digests "
                f"(keeping {pdf_kept} non-rss)"
            )
            if commit:
                removed = 0
                for filename in filenames:
                    try:
                        tasks.delete_digest(filename)
                        removed += 1
                    except (FileNotFoundError, ValueError):
                        continue
                tasks._rebuild_inbox_index()
                total_removed += removed
                print(f"  -> removed {removed}")
    if commit:
        print(f"done: removed {total_removed} rss digests across all tenants")
    else:
        print("dry-run only; re-run with --commit to apply")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", help="覆盖 RSSAI_SERVER_DATA_DIR")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="真正删除；缺省仅 dry-run",
    )
    args = parser.parse_args(argv)
    return migrate(args.data_dir, commit=args.commit)


if __name__ == "__main__":
    sys.exit(main())
