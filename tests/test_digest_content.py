import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import tasks


class DigestContentTests(unittest.TestCase):
    def test_pdf_metadata_uses_resolved_server_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / "inbox"
            inbox.mkdir()
            digest_file = inbox / "digest.html"
            digest_file.write_text(
                """<!doctype html><html><head><title>测试论文</title></head>
                <body><h1>测试论文</h1><div class="content">摘要正文</div></body></html>""",
                encoding="utf-8",
            )
            pdf_file = root / "server-final-name.pdf"
            pdf_file.write_bytes(b"%PDF-1.7\n" + b"0" * 25_000)

            connection = Mock()
            connection.execute.return_value.fetchone.return_value = ("pdf", 0)

            with (
                patch.object(tasks, "INBOX_DIR", inbox),
                patch.object(tasks, "_sync_digest_index"),
                patch.object(tasks, "_digest_db", return_value=connection),
                patch.object(tasks, "resolve_pdf_path", return_value=str(pdf_file)),
            ):
                content = tasks.get_digest_content(digest_file.name)

            self.assertTrue(content["pdf_available"])
            self.assertEqual(content["pdf_filename"], pdf_file.name)
            self.assertEqual(content["pdf_size"], pdf_file.stat().st_size)
            connection.close.assert_called_once()

    def test_missing_pdf_has_empty_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory)
            digest_file = inbox / "digest.html"
            digest_file.write_text(
                '<html><h1>测试论文</h1><div class="content">摘要正文</div></html>',
                encoding="utf-8",
            )
            connection = Mock()
            connection.execute.return_value.fetchone.return_value = ("pdf", 0)

            with (
                patch.object(tasks, "INBOX_DIR", inbox),
                patch.object(tasks, "_sync_digest_index"),
                patch.object(tasks, "_digest_db", return_value=connection),
                patch.object(tasks, "resolve_pdf_path", return_value=None),
            ):
                content = tasks.get_digest_content(digest_file.name)

            self.assertFalse(content["pdf_available"])
            self.assertEqual(content["pdf_filename"], "")
            self.assertEqual(content["pdf_size"], 0)


if __name__ == "__main__":
    unittest.main()
