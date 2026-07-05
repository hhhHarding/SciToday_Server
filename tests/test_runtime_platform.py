import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import push
import tasks


class RuntimePlatformTests(unittest.TestCase):
    def test_auto_runtime_selects_pc_on_windows(self):
        self.assertEqual(tasks._resolve_runtime_profile("auto", "nt"), "pc")
        self.assertEqual(tasks._resolve_runtime_profile("auto", "posix"), "termux")

    def test_explicit_runtime_overrides_platform(self):
        self.assertEqual(tasks._resolve_runtime_profile("termux", "nt"), "termux")
        self.assertEqual(tasks._resolve_runtime_profile("pc", "posix"), "pc")

    def test_base_dir_environment_has_priority(self):
        expected = Path("C:/custom-rssai")
        with patch.dict(os.environ, {"RSSAI_BASE_DIR": str(expected)}, clear=False):
            self.assertEqual(tasks._env_path("RSSAI_BASE_DIR", Path("unused")), expected)

    def test_opml_path_is_always_tenant_owned(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            tenant_paths = type("Paths", (), {"opml": base / "feedly.opml"})()
            with patch.object(tasks, "current_tenant_paths", return_value=tenant_paths):
                self.assertEqual(
                    tasks.get_opml_path({"rss": {"opml_path": ""}}),
                    str(base / "feedly.opml"),
                )

    def test_configured_opml_cannot_escape_tenant_path(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            tenant_paths = type("Paths", (), {"opml": base / "feedly.opml"})()
            with patch.object(tasks, "current_tenant_paths", return_value=tenant_paths):
                self.assertEqual(
                    tasks.get_opml_path({"rss": {"opml_path": "../../outside.opml"}}),
                    str(base / "feedly.opml"),
                )

    def test_ai_environment_cannot_override_tenant_config(self):
        with (
            patch.dict(os.environ, {"AI_API_KEY": "env-key"}, clear=False),
            patch.object(tasks, "_cfg", return_value="config-key"),
        ):
            self.assertEqual(
                tasks._env_or_cfg("AI_API_KEY", "ai.api_key"),
                "config-key",
            )

    def test_ai_base_url_only_adds_v1_when_provider_path_is_empty(self):
        self.assertEqual(
            tasks._normalize_ai_base_url("https://api.deepseek.com/"),
            "https://api.deepseek.com/v1",
        )
        self.assertEqual(
            tasks._normalize_ai_base_url(
                "https://ark.cn-beijing.volces.com/api/v3/"
            ),
            "https://ark.cn-beijing.volces.com/api/v3",
        )
        self.assertEqual(
            tasks._normalize_ai_base_url("https://example.com/v1"),
            "https://example.com/v1",
        )

    def test_pc_auto_notification_is_disabled(self):
        with (
            patch.dict(
                os.environ,
                {
                    "RSSAI_RUNTIME": "pc",
                    "RSSAI_NOTIFICATION_CHANNEL": "auto",
                },
                clear=False,
            ),
            patch.object(push.subprocess, "run") as run,
        ):
            self.assertFalse(push.send_notification("title", "message"))
            run.assert_not_called()

    def test_notification_environment_overrides_config(self):
        config = {"notifications": {"channel": "termux"}}
        with patch.dict(
            os.environ,
            {
                "RSSAI_RUNTIME": "termux",
                "RSSAI_NOTIFICATION_CHANNEL": "none",
            },
            clear=False,
        ):
            self.assertEqual(push.resolve_notification_channel(config), "none")


if __name__ == "__main__":
    unittest.main()
