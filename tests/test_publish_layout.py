import hashlib
import json
import shutil
import subprocess
import unittest
from pathlib import Path


class PublishLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = Path(__file__).resolve().parents[1]
        cls.script = cls.repo / "installer" / "build_publish.ps1"
        cls.android = cls.repo.parent / "RssAiPushApp"
        if not cls.script.exists() or not cls.android.exists():
            raise unittest.SkipTest("canonical source roots are not available")
        cls.output_a = cls.repo / "dist" / "publish_test_a"
        cls.output_b = cls.repo / "dist" / "publish_test_b"
        for output in (cls.output_a, cls.output_b):
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(cls.script),
                    "-AndroidSource",
                    str(cls.android),
                    "-OutputDir",
                    str(output),
                ],
                cwd=cls.repo,
                check=True,
                capture_output=True,
                text=True,
            )

    @classmethod
    def tearDownClass(cls):
        for output in (getattr(cls, "output_a", None), getattr(cls, "output_b", None)):
            if output and output.exists():
                shutil.rmtree(output)

    def _manifest(self, output):
        return json.loads((output / "SOURCE_MANIFEST.json").read_text(encoding="utf-8-sig"))

    def test_two_builds_have_identical_manifest(self):
        self.assertEqual(self._manifest(self.output_a), self._manifest(self.output_b))

    def test_generated_tasks_matches_canonical_source(self):
        expected = hashlib.sha256((self.repo / "tasks.py").read_bytes()).digest()
        actual = hashlib.sha256(
            (self.output_a / "pc_backend" / "tasks.py").read_bytes()
        ).digest()
        self.assertEqual(actual, expected)

    def test_android_whitelist_matches_canonical_source(self):
        relative = Path("app/src/main/java/com/rssai/push/MainActivity.kt")
        self.assertEqual(
            (self.output_a / "app" / relative).read_bytes(),
            (self.android / relative).read_bytes(),
        )

    def test_generated_tree_excludes_private_and_build_files(self):
        relative_paths = {
            path.relative_to(self.output_a).as_posix()
            for path in self.output_a.rglob("*")
        }
        forbidden_parts = {
            ".gradle",
            ".kotlin",
            ".tmp_phone",
            "__pycache__",
        }
        for relative in relative_paths:
            parts = set(Path(relative).parts)
            self.assertFalse(parts & forbidden_parts, relative)
            self.assertNotIn("/build/", f"/{relative}/")
            self.assertFalse(relative.endswith("gradle-8.9-bin.zip"), relative)
            self.assertNotEqual(relative, "pc_backend/config.json")
            self.assertFalse(relative.endswith(".db"), relative)
            self.assertFalse(relative.endswith(".log"), relative)


if __name__ == "__main__":
    unittest.main()
