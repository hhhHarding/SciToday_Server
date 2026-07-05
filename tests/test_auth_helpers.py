import tempfile
import unittest
import logging
from pathlib import Path

from auth import (
    SensitiveDataFilter,
    is_local_operator_request,
    load_operator_token,
    operator_token_matches,
    redact_sensitive_text,
    write_operator_token,
)
from server_config import ServerPaths


class AuthHelperTests(unittest.TestCase):
    def test_operator_token_file_is_created_once_and_loaded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = ServerPaths(Path(temp_dir) / "server")
            token, path = write_operator_token(paths)

            self.assertTrue(path.is_file())
            self.assertEqual(load_operator_token(paths, environ={}), token)
            self.assertTrue(operator_token_matches(token, token))
            self.assertFalse(operator_token_matches(token + "x", token))
            with self.assertRaises(FileExistsError):
                write_operator_token(paths)

    def test_environment_operator_token_has_priority(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = ServerPaths(Path(temp_dir) / "server")
            write_operator_token(paths, token="file-token")
            loaded = load_operator_token(
                paths,
                environ={"RSSAI_OPERATOR_TOKEN": "env-token"},
            )

        self.assertEqual(loaded, "env-token")

    def test_local_operator_request_rejects_proxy_or_public_host(self):
        self.assertTrue(
            is_local_operator_request(
                remote_addr="127.0.0.1",
                request_host="localhost:5200",
                headers={},
            )
        )
        self.assertFalse(
            is_local_operator_request(
                remote_addr="127.0.0.1",
                request_host="rss.example.com",
                headers={"CF-Connecting-IP": "203.0.113.10"},
            )
        )

    def test_sensitive_tokens_are_redacted_from_log_text(self):
        tenant_token = "rssai_tk_0123456789abcdef_supersecretvalue"
        operator_token = "rssai_op_anothersecretvalue"
        rendered = redact_sensitive_text(
            f"GET /inbox?a=1&token={tenant_token} "
            f"Authorization: Bearer {operator_token}"
        )

        self.assertNotIn(tenant_token, rendered)
        self.assertNotIn(operator_token, rendered)
        self.assertIn("[REDACTED]", rendered)

        record = logging.LogRecord(
            "test",
            logging.INFO,
            __file__,
            1,
            "token=%s",
            (tenant_token,),
            None,
        )
        self.assertTrue(SensitiveDataFilter().filter(record))
        self.assertNotIn(tenant_token, record.getMessage())
        self.assertFalse(
            is_local_operator_request(
                remote_addr="127.0.0.1",
                request_host="localhost:5200",
                headers={"X-Forwarded-For": "203.0.113.10"},
            )
        )


if __name__ == "__main__":
    unittest.main()
