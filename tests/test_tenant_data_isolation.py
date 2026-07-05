import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from werkzeug.datastructures import FileStorage

import tasks
from server_config import ServerPaths
from tenancy.context import tenant_context
from tenancy.paths import TenantPaths
from tenancy.registry import TenantRegistry


class TenantDataIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.server_paths = ServerPaths(Path(self.temp_dir.name) / "server")
        self.registry = TenantRegistry(self.server_paths)
        self.registry.create_tenant("Alpha", tenant_id="t_alpha")
        self.registry.create_tenant("Beta", tenant_id="t_beta")
        self.server_paths_patch = patch.object(
            tasks,
            "SERVER_PATHS",
            self.server_paths,
        )
        self.server_paths_patch.start()
        tasks._reset_config_cache_for_tests()
        tasks._reset_migration_cache_for_tests()
        tasks._digest_index_sentinels.clear()
        tasks._digest_index_locks.clear()
        tasks._db_locks.clear()

    def tearDown(self):
        self.server_paths_patch.stop()
        tasks._reset_config_cache_for_tests()
        tasks._reset_migration_cache_for_tests()
        tasks._digest_index_sentinels.clear()
        tasks._digest_index_locks.clear()
        tasks._db_locks.clear()
        self.temp_dir.cleanup()

    def _paths(self, tenant_id):
        return TenantPaths(self.server_paths.data_root, tenant_id)

    @staticmethod
    def _digest_html(title, content):
        return (
            '<meta name="digest-source" content="rss">'
            '<meta name="digest-timestamp" content="2026-07-03 10:00">'
            f"<title>{title}</title>"
            f'<div class="content">{content}</div>'
        )

    def _populate_rss(self, tenant_id, marker):
        with tenant_context(tenant_id):
            paths = tasks.current_tenant_paths()
            tasks.save_config({
                "marker": marker,
                "ai": {
                    "api_key": f"{marker}-key",
                    "base_url": "https://example.invalid",
                    "model": f"{marker}-model",
                },
                "rss": {"lookback_days": 7},
            })
            paths.opml.write_text(
                f'<opml version="2.0"><body><outline xmlUrl="https://same.example/feed" '
                f'text="{marker}" /></body></opml>',
                encoding="utf-8",
            )
            con = tasks._db_open(str(tasks.RSS_DB))
            con.execute(
                "INSERT INTO seen(id,title,link,feed,ts) VALUES(?,?,?,?,?)",
                ("same-id", marker, "https://same.example/item", "same-feed", 1),
            )
            con.commit()
            con.close()
            paths.inbox_dir.mkdir(parents=True, exist_ok=True)
            (paths.inbox_dir / "same.html").write_text(
                self._digest_html(marker, f"{marker}-secret"),
                encoding="utf-8",
            )
            tasks._sync_digest_index(force=True)
            tasks.record_event("isolation", marker)

    def test_config_opml_ai_and_cache_are_isolated(self):
        self._populate_rss("t_alpha", "alpha")
        self._populate_rss("t_beta", "beta")

        with patch.dict(os.environ, {"AI_API_KEY": "global-must-be-ignored"}):
            with tenant_context("t_alpha"):
                self.assertEqual(tasks.load_config()["marker"], "alpha")
                self.assertIn('text="alpha"', Path(tasks.get_opml_path()).read_text())
                self.assertEqual(tasks._ai_config()[0], "alpha-key")
            with tenant_context("t_beta"):
                self.assertEqual(tasks.load_config()["marker"], "beta")
                self.assertIn('text="beta"', Path(tasks.get_opml_path()).read_text())
                self.assertEqual(tasks._ai_config()[0], "beta-key")

        self.assertEqual(len(tasks._config_cache), 2)
        self.assertEqual(
            {key[0] for key in tasks._config_cache},
            {"t_alpha", "t_beta"},
        )

    def test_same_database_keys_and_digest_filenames_do_not_cross(self):
        self._populate_rss("t_alpha", "alpha")
        self._populate_rss("t_beta", "beta")

        with tenant_context("t_alpha"):
            alpha = tasks.get_recent_digests()
            self.assertEqual(alpha[0]["filename"], "same.html")
            self.assertIn("alpha-secret", tasks.get_digest_text("same.html"))
            con = tasks._db_open(str(tasks.RSS_DB))
            self.assertEqual(con.execute("SELECT title FROM seen").fetchone()[0], "alpha")
            con.close()
        with tenant_context("t_beta"):
            beta = tasks.get_recent_digests()
            self.assertEqual(beta[0]["filename"], "same.html")
            self.assertNotIn("alpha-secret", tasks.get_digest_text("same.html"))
            con = tasks._db_open(str(tasks.RSS_DB))
            self.assertEqual(con.execute("SELECT title FROM seen").fetchone()[0], "beta")
            con.close()

        self.assertNotEqual(
            self._paths("t_alpha").digest_db,
            self._paths("t_beta").digest_db,
        )

    def test_missing_digest_is_not_visible_to_other_tenant_chat_or_pdf(self):
        self._populate_rss("t_alpha", "alpha")
        with tenant_context("t_beta"):
            tasks.save_config({
                "ai": {
                    "api_key": "beta-key",
                    "base_url": "https://example.invalid",
                    "model": "beta-model",
                }
            })
            self.assertEqual(tasks.get_recent_digests(), [])
            with self.assertRaises(FileNotFoundError):
                tasks.get_digest_text("same.html")
            self.assertIsNone(tasks.resolve_pdf_path("same.html"))
            self.assertIn("无法读取该文章内容", tasks.ai_chat("same.html", "问题"))

    def test_same_pdf_and_chunk_ids_are_isolated_and_bound(self):
        saved = {}
        for tenant_id in ("t_alpha", "t_beta"):
            with tenant_context(tenant_id):
                direct = FileStorage(
                    stream=io.BytesIO(b"%PDF-1.4\n" + b"x" * 21_000),
                    filename="same.pdf",
                )
                saved[tenant_id] = Path(tasks.save_uploaded_pdf(direct))
                digest_name, _ = tasks.save_html(
                    f"{tenant_id} PDF",
                    "PDF summary",
                    source="pdf",
                    pdf_path=saved[tenant_id],
                )
                digest_html = (
                    tasks.current_tenant_paths().inbox_dir / digest_name
                ).read_text(encoding="utf-8")
                self.assertNotIn(str(self.server_paths.data_root), digest_html)
                self.assertEqual(
                    Path(tasks.resolve_pdf_path(digest_name)),
                    saved[tenant_id].resolve(),
                )
                first_chunk = FileStorage(
                    stream=io.BytesIO(b"%PDF-1.4\n" + b"a" * 11_000),
                    filename="chunked.pdf",
                )
                result = tasks.save_uploaded_pdf_chunk(
                    "shared-upload-id",
                    "chunked.pdf",
                    0,
                    2,
                    first_chunk,
                )
                self.assertFalse(result["complete"])
                self.assertEqual(result["next_index"], 1)
                with self.assertRaisesRegex(ValueError, "已绑定"):
                    tasks.save_uploaded_pdf_chunk(
                        "shared-upload-id",
                        "different.pdf",
                        1,
                        2,
                        FileStorage(
                            stream=io.BytesIO(b"b" * 11_000),
                            filename="different.pdf",
                        ),
                    )

        self.assertEqual(saved["t_alpha"].name, "same.pdf")
        self.assertEqual(saved["t_beta"].name, "same.pdf")
        self.assertNotEqual(saved["t_alpha"].parent, saved["t_beta"].parent)
        for tenant_id in ("t_alpha", "t_beta"):
            meta = json.loads(
                (
                    self._paths(tenant_id).pdf_chunks_dir
                    / "shared-upload-id"
                    / "meta.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(meta["tenant_id"], tenant_id)

    def test_external_download_scan_is_owner_only_and_explicit(self):
        external = Path(self.temp_dir.name) / "Downloads"
        external.mkdir()
        with tenant_context("t_alpha"):
            tasks.save_config({
                "pc": {
                    "allow_owner_download_scan": True,
                    "owner_download_dirs": [str(external)],
                }
            })
            self.assertEqual(
                tasks._download_dirs(),
                [self._paths("t_alpha").uploaded_pdfs_dir],
            )

        with tenant_context("owner"):
            tasks.save_config({
                "pc": {
                    "allow_owner_download_scan": True,
                    "owner_download_dirs": [str(external)],
                }
            })
            self.assertEqual(
                tasks._download_dirs(),
                [self._paths("owner").uploaded_pdfs_dir, external.resolve()],
            )

    def test_cleanup_of_alpha_does_not_change_beta(self):
        self._populate_rss("t_alpha", "alpha")
        self._populate_rss("t_beta", "beta")
        beta_paths = self._paths("t_beta")
        beta_before = beta_paths.rss_db.stat().st_size

        with tenant_context("t_alpha"):
            tasks.cleanup_source("rss")

        with tenant_context("t_beta"):
            con = tasks._db_open(str(tasks.RSS_DB))
            self.assertEqual(con.execute("SELECT COUNT(*) FROM seen").fetchone()[0], 1)
            con.close()
            self.assertTrue((beta_paths.inbox_dir / "same.html").is_file())
            self.assertIn("beta-secret", tasks.get_digest_text("same.html"))
            self.assertGreaterEqual(beta_paths.rss_db.stat().st_size, beta_before)

    def test_two_tenants_can_run_first_database_migration_concurrently(self):
        tasks._reset_migration_cache_for_tests()

        def initialize(tenant_id):
            with tenant_context(tenant_id):
                connections = [
                    tasks._db_open(str(tasks.RSS_DB)),
                    tasks._pending_db(),
                    tasks._pdf_db(),
                    tasks._digest_db(),
                    tasks._admin_db(),
                ]
                for con in connections:
                    con.close()
                return {
                    os.path.normcase(str(path.resolve()))
                    for path in tasks.current_tenant_paths().database_paths
                }

        with ThreadPoolExecutor(max_workers=2) as executor:
            alpha_future = executor.submit(initialize, "t_alpha")
            beta_future = executor.submit(initialize, "t_beta")
            alpha_paths = alpha_future.result()
            beta_paths = beta_future.result()

        self.assertTrue(alpha_paths.isdisjoint(beta_paths))
        self.assertEqual(len(alpha_paths), 5)
        self.assertEqual(len(beta_paths), 5)
        self.assertTrue(alpha_paths | beta_paths <= tasks._migrated_paths)

    def test_persisted_isolation_survives_fresh_process(self):
        self._populate_rss("t_alpha", "alpha")
        self._populate_rss("t_beta", "beta")
        script = (
            "import json, tasks\n"
            "from tenancy.context import tenant_context\n"
            "with tenant_context('t_beta'):\n"
            " print(json.dumps({'marker': tasks.load_config()['marker'], "
            "'text': tasks.get_digest_text('same.html')}))\n"
        )
        env = os.environ.copy()
        env["RSSAI_SERVER_DATA_DIR"] = str(self.server_paths.data_root)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(completed.stdout.strip().splitlines()[-1])

        self.assertEqual(payload["marker"], "beta")
        self.assertIn("beta-secret", payload["text"])


if __name__ == "__main__":
    unittest.main()
