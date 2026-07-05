import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tenancy.paths import TenantPaths, ensure_safe_path, validate_tenant_id


class TenantPathsTests(unittest.TestCase):
    def test_every_tenant_owned_path_is_distinct(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = TenantPaths(root, "t_alpha")
            second = TenantPaths(root, "t_beta")

            first_paths = {
                first.config,
                first.opml,
                *first.database_paths,
                first.inbox_dir,
                first.uploaded_pdfs_dir,
                first.pdf_chunks_dir,
            }
            second_paths = {
                second.config,
                second.opml,
                *second.database_paths,
                second.inbox_dir,
                second.uploaded_pdfs_dir,
                second.pdf_chunks_dir,
            }

        self.assertTrue(first_paths.isdisjoint(second_paths))
        self.assertEqual(len(first.database_paths), 5)
        self.assertEqual(len(second.database_paths), 5)

    def test_candidate_cannot_escape_data_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            outside = Path(temp_dir) / "outside"
            root.mkdir()

            with self.assertRaisesRegex(ValueError, "不在 server data root"):
                ensure_safe_path(root, outside)

    def test_invalid_tenant_ids_are_rejected(self):
        for value in ("", "../escape", "UpperCase", "a/b", "a\\b", ".hidden", "CON"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_tenant_id(value)

    def test_existing_symlink_tenant_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            tenants = root / "tenants"
            outside = Path(temp_dir) / "outside"
            tenants.mkdir(parents=True)
            outside.mkdir()
            link = tenants / "linked"
            try:
                os.symlink(outside, link, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"当前环境不能创建目录软链接: {exc}")

            with self.assertRaisesRegex(ValueError, "软链接或目录联接|逃出"):
                TenantPaths(root, "linked")

    def test_junction_or_reparse_point_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            tenant_dir = root / "tenants" / "linked"
            tenant_dir.mkdir(parents=True)

            def fake_reparse(path):
                return Path(path) == tenant_dir

            with patch("tenancy.paths._is_reparse_point", side_effect=fake_reparse):
                with self.assertRaisesRegex(ValueError, "软链接或目录联接"):
                    TenantPaths(root, "linked")

    def test_layout_creation_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = TenantPaths(Path(temp_dir), "t_layout")
            paths.ensure_directories()
            paths.ensure_directories()

            self.assertTrue(all(path.is_dir() for path in paths.directory_paths))


if __name__ == "__main__":
    unittest.main()

