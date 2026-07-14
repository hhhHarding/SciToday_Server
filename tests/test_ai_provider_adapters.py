import json
import unittest
from unittest.mock import Mock, patch

import tasks


class AIProviderAdapterTests(unittest.TestCase):
    def test_anthropic_messages_endpoint_uses_native_protocol(self):
        response = Mock()
        response.status_code = 200
        response.headers = {"Content-Type": "application/json"}
        response.json.return_value = {
            "content": [{"type": "text", "text": "OK"}],
        }

        with (
            patch.object(tasks, "AI_SESSION") as session,
            patch.object(tasks, "assert_safe_outbound_url"),
        ):
            session.post.return_value = response
            message = tasks._chat_completion_request(
                [
                    {"role": "system", "content": "system rule"},
                    {"role": "user", "content": "hello"},
                ],
                "secret-key",
                "https://www.right.codes/claude/v1/messages",
                "claude-opus-4-8",
            )

        self.assertEqual(message, {"role": "assistant", "content": "OK"})
        endpoint = session.post.call_args.args[0]
        kwargs = session.post.call_args.kwargs
        self.assertEqual(endpoint, "https://www.right.codes/claude/v1/messages")
        self.assertEqual(kwargs["headers"]["x-api-key"], "secret-key")
        self.assertEqual(kwargs["headers"]["anthropic-version"], "2023-06-01")
        payload = json.loads(kwargs["data"].decode("utf-8"))
        self.assertEqual(payload["model"], "claude-opus-4-8")
        self.assertEqual(payload["system"], "system rule")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "hello"}])
        self.assertIn("max_tokens", payload)

    def test_openai_compatible_endpoint_still_uses_chat_completions(self):
        response = Mock()
        response.status_code = 200
        response.headers = {"Content-Type": "application/json"}
        response.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "OK"}}],
        }

        with (
            patch.object(tasks, "AI_SESSION") as session,
            patch.object(tasks, "assert_safe_outbound_url"),
        ):
            session.post.return_value = response
            message = tasks._chat_completion_request(
                [{"role": "user", "content": "hello"}],
                "secret-key",
                "https://api.example.com/v1",
                "gpt-test",
            )

        self.assertEqual(message["content"], "OK")
        endpoint = session.post.call_args.args[0]
        self.assertEqual(endpoint, "https://api.example.com/v1/chat/completions")

    def test_non_json_success_response_has_diagnostic_error(self):
        response = Mock()
        response.status_code = 200
        response.headers = {"Content-Type": "text/html"}
        response.text = "<html>not an api</html>"
        response.json.side_effect = ValueError("not json")

        with (
            patch.object(tasks, "AI_SESSION") as session,
            patch.object(tasks, "assert_safe_outbound_url"),
        ):
            session.post.return_value = response
            with self.assertRaises(RuntimeError) as ctx:
                tasks._chat_completion_request(
                    [{"role": "user", "content": "hello"}],
                    "secret-key",
                    "https://api.example.com/v1",
                    "gpt-test",
                )

        self.assertIn("AI 返回非 JSON 响应", str(ctx.exception))
        self.assertIn("text/html", str(ctx.exception))

if __name__ == "__main__":
    unittest.main()
