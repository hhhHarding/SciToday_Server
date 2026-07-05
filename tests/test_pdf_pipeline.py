import contextlib
import io
import unittest
from unittest.mock import patch

import pdf_watch_summarize
import tasks


class PdfPipelineTests(unittest.TestCase):
    def test_legacy_cli_delegates_to_unified_task(self):
        output = io.StringIO()
        with (
            patch.object(pdf_watch_summarize.tasks, "run_pdf_watch", return_value=3) as run,
            contextlib.redirect_stdout(output),
        ):
            result = pdf_watch_summarize.main()

        self.assertIsNone(result)
        run.assert_called_once_with()
        self.assertIn("新增 3 篇", output.getvalue())

    def test_pdf_match_prefers_exact_doi(self):
        papers = [
            {
                "id": "paper-1",
                "title": "Unrelated geological study",
                "doi": "10.1234/example.2026.7",
            }
        ]
        paper, score, reason = tasks._match_pdf(
            "Full text DOI: 10.1234/example.2026.7",
            papers,
        )
        self.assertEqual(paper["id"], "paper-1")
        self.assertEqual(score, 1.0)
        self.assertEqual(reason, "DOI matched")

    def test_pdf_title_threshold_accepts_and_rejects_candidates(self):
        papers = [
            {
                "id": "paper-2",
                "title": "Ancient microbial carbonate evolution",
                "doi": "",
            }
        ]
        accepted, score, _ = tasks._match_pdf(
            "ancient microbial carbonate deposits reveal a long evolution",
            papers,
        )
        rejected, rejected_score, _ = tasks._match_pdf(
            "ancient deposits without the remaining title terms",
            papers,
        )
        self.assertEqual(accepted["id"], "paper-2")
        self.assertGreaterEqual(score, 0.65)
        self.assertIsNone(rejected)
        self.assertLess(rejected_score, 0.65)

    def test_pdf_match_prefers_exact_title_over_generic_token_hits(self):
        papers = [
            {"id": "generic", "title": "Issue Information", "doi": ""},
            {
                "id": "target",
                "title": "Determination of fluorine concentration in topaz using Raman spectroscopy",
                "doi": "",
            },
        ]
        text = (
            "American Mineralogist issue information. "
            "Determination of fluorine concentration in topaz using Raman spectroscopy. "
            "The full paper follows."
        )
        paper, score, reason = tasks._match_pdf(text, papers)
        self.assertEqual(paper["id"], "target")
        self.assertEqual(score, 0.99)
        self.assertEqual(reason, "exact title matched")


if __name__ == "__main__":
    unittest.main()
