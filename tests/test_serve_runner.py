import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import serve
from server_config import ServerPaths


class ServeRunnerTests(unittest.TestCase):
    def test_coordinator_starts_before_waitress_and_shuts_down_after(self):
        events = []
        flask_app = Mock()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        registry = SimpleNamespace(
            server_paths=ServerPaths(Path(temp_dir.name) / "server"),
            list_tenants=lambda: [],
        )
        flask_app.config = {"TENANT_REGISTRY": registry}
        coordinator = Mock()
        coordinator.max_workers = 2
        coordinator.max_pending = 8
        coordinator.start.side_effect = lambda: events.append("coordinator.start")
        coordinator.shutdown.side_effect = (
            lambda **_kwargs: events.append("coordinator.shutdown")
        )
        server = Mock()
        server.run.side_effect = lambda: events.append("server.run")
        server.close.side_effect = lambda: events.append("server.close")

        with (
            patch.object(
                serve.TaskCoordinator,
                "from_env",
                return_value=coordinator,
            ),
            patch.dict(
                "os.environ",
                {
                    "RSSAI_SERVER_HOST": "127.0.0.1",
                    "RSSAI_SERVER_PORT": "5299",
                    "RSSAI_TRUSTED_PROXY": "127.0.0.1",
                },
                clear=False,
            ),
        ):
            server_factory = Mock(return_value=server)
            serve.run(flask_app, server_factory=server_factory)

        self.assertLess(
            events.index("coordinator.start"),
            events.index("server.run"),
        )
        self.assertLess(
            events.index("server.run"),
            events.index("coordinator.shutdown"),
        )
        coordinator.shutdown.assert_called_once_with(wait=True)
        self.assertNotIn("TASK_COORDINATOR", flask_app.config)
        server_factory.assert_called_once_with(
            flask_app,
            host="127.0.0.1",
            port=5299,
            threads=8,
            trusted_proxy="127.0.0.1",
            trusted_proxy_count=1,
            trusted_proxy_headers={
                "x-forwarded-for",
                "x-forwarded-host",
                "x-forwarded-proto",
            },
            clear_untrusted_proxy_headers=True,
        )

    def test_proxy_headers_are_not_trusted_without_explicit_proxy(self):
        self.assertEqual(serve._waitress_proxy_settings({}), {})

    def test_wildcard_trusted_proxy_is_rejected(self):
        with self.assertRaises(RuntimeError):
            serve._waitress_proxy_settings({"RSSAI_TRUSTED_PROXY": "*"})

    def test_insecure_mode_refuses_non_loopback_bind(self):
        flask_app = Mock()
        flask_app.config = {"TENANT_REGISTRY": object()}
        with patch.dict(
            "os.environ",
            {
                "RSSAI_INSECURE_DEV_MODE": "1",
                "RSSAI_SERVER_HOST": "0.0.0.0",
            },
            clear=False,
        ):
            with self.assertRaises(RuntimeError):
                serve.run(flask_app)

    def test_process_lock_rejects_second_server_for_same_data_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "control" / "server.lock"
            first = serve.ServerInstanceLock(path)
            second = serve.ServerInstanceLock(path)
            first.acquire()
            try:
                with self.assertRaises(RuntimeError):
                    second.acquire()
            finally:
                first.release()
            second.acquire()
            second.release()


if __name__ == "__main__":
    unittest.main()
