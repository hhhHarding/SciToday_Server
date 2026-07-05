import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tasks


class DigestFilenameTests(unittest.TestCase):
    def test_chinese_title_is_not_present_in_filename_or_url_identifier(self):
        title = "超高压条件下矿物相变与地球深部动力学研究" * 20
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory)
            with patch.object(tasks.os, "urandom", return_value=b"\xab" * 12):
                filename, _timestamp = tasks.save_html(
                    title,
                    "摘要正文",
                    inbox_dir=inbox,
                )

            self.assertRegex(
                filename,
                r"^\d{8}_\d{6}_[0-9a-f]{24}\.html$",
            )
            self.assertTrue(filename.isascii())
            self.assertLessEqual(len(filename.encode("utf-8")), 255)
            self.assertNotIn("矿物", filename)
            page = (inbox / filename).read_text(encoding="utf-8")
            self.assertIn(title, page)
            self.assertEqual(tasks._digest_from_file(inbox / filename)["title"], title)

    def test_generated_names_are_unique_for_same_title_and_second(self):
        values = [b"\x01" * 12, b"\x02" * 12]
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory)
            with patch.object(tasks.os, "urandom", side_effect=values):
                first, _ = tasks.save_html("同一标题", "内容一", inbox_dir=inbox)
                second, _ = tasks.save_html("同一标题", "内容二", inbox_dir=inbox)

            self.assertNotEqual(first, second)
            self.assertTrue((inbox / first).is_file())
            self.assertTrue((inbox / second).is_file())

    def test_new_filename_has_fixed_utf8_byte_length(self):
        filename = tasks._new_digest_filename("20260704_213000")
        self.assertTrue(filename.isascii())
        self.assertEqual(len(filename.encode("utf-8")), 45)
        self.assertIsNotNone(
            re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{24}\.html", filename)
        )

    def test_pdf_fallback_reads_title_from_html_not_new_filename(self):
        title = "深部矿物相变实验研究"
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory)
            filename, _ = tasks.save_html(title, "摘要正文", inbox_dir=inbox)
            with (
                patch.object(tasks, "INBOX_DIR", inbox),
                patch.object(
                    tasks,
                    "_lookup_pdf_by_title",
                    return_value="/downloads/source.pdf",
                ) as lookup,
            ):
                resolved = tasks.resolve_pdf_path(filename)

            self.assertEqual(resolved, "/downloads/source.pdf")
            lookup.assert_called_once_with(title)


if __name__ == "__main__":
    unittest.main()
