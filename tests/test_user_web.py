import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as backend


class UserWebStaticTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "assets").mkdir()
        (self.root / "index.html").write_text(
            "<!doctype html><title>SciToday User</title>",
            encoding="utf-8",
        )
        (self.root / "assets" / "app.js").write_text(
            "console.log('ok')",
            encoding="utf-8",
        )
        self.patch = patch.object(backend, "USER_STATIC_DIR", self.root)
        self.patch.start()
        backend.app.config.update(TESTING=True)
        self.client = backend.app.test_client()

    def tearDown(self):
        self.patch.stop()
        self.temp_dir.cleanup()

    def test_user_root_redirects_and_spa_routes_fall_back_to_index(self):
        redirect = self.client.get("/user")
        self.assertEqual(redirect.status_code, 308)
        self.assertEqual(redirect.headers["Location"], "/user/")

        root = self.client.get("/user/")
        route = self.client.get("/user/messages")
        self.assertEqual(root.status_code, 200)
        self.assertEqual(route.status_code, 200)
        self.assertIn(b"SciToday User", route.data)
        self.assertEqual(root.headers["Cache-Control"], "no-store")
        redirect.close()
        root.close()
        route.close()

    def test_hashed_assets_receive_long_lived_cache_policy(self):
        response = self.client.get("/user/assets/app.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["Cache-Control"],
            "public, max-age=31536000, immutable",
        )
        response.close()


if __name__ == "__main__":
    unittest.main()
