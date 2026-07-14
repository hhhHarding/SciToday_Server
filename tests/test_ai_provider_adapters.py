import json
import unittest
from unittest.mock import Mock, patch

import tasks


class AIProviderAdapterTests(unittest.TestCase):
    def setUp(self):
        tasks._reset_anthropic_web_fetch_capabilities_for_tests()

    def tearDown(self):
        tasks._reset_anthropic_web_fetch_capabilities_for_tests()

    @staticmethod
    def _response(payload, status=200):
        response = Mock()
        response.status_code = status
        response.headers = {"Content-Type": "application/json"}
        response.json.return_value = payload
        return response

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

    def test_anthropic_web_fetch_payload_and_result_are_structured(self):
        response = self._response({
            "stop_reason": "end_turn",
            "content": [
                {
                    "type": "server_tool_use",
                    "id": "srv_1",
                    "name": "web_fetch",
                    "input": {"url": "https://publisher.example/paper"},
                },
                {
                    "type": "web_fetch_tool_result",
                    "tool_use_id": "srv_1",
                    "content": {
                        "type": "web_fetch_result",
                        "url": "https://publisher.example/paper",
                        "content": {"type": "document", "title": "Paper"},
                    },
                },
                {"type": "text", "text": "最终 digest"},
            ],
        })
        tool = tasks._anthropic_web_fetch_tool(
            tasks.ANTHROPIC_WEB_FETCH_LATEST,
            ["publisher.example"],
        )
        with (
            patch.object(tasks, "AI_SESSION") as session,
            patch.object(tasks, "assert_safe_outbound_url"),
        ):
            session.post.return_value = response
            message = tasks._chat_completion_request(
                [{"role": "user", "content": "fetch this URL"}],
                "secret-key",
                "https://api.anthropic.com/v1/messages",
                "claude-opus-4-8",
                anthropic_server_tools=[tool],
            )

        self.assertEqual(message["content"], "最终 digest")
        self.assertTrue(message["_anthropic_server_tool"]["succeeded"])
        self.assertNotIn("web_fetch_result", message["content"])
        request_payload = json.loads(session.post.call_args.kwargs["data"].decode("utf-8"))
        self.assertEqual(request_payload["tools"][0]["type"], "web_fetch_20260318")
        self.assertEqual(request_payload["tools"][0]["max_uses"], 1)
        self.assertEqual(request_payload["tools"][0]["max_content_tokens"], 12000)
        self.assertEqual(request_payload["tools"][0]["allowed_domains"], ["publisher.example"])

    def test_anthropic_web_fetch_page_error_uses_final_rss_digest(self):
        response = self._response({
            "stop_reason": "end_turn",
            "content": [
                {
                    "type": "server_tool_use",
                    "id": "srv_1",
                    "name": "web_fetch",
                    "input": {"url": "https://publisher.example/paper"},
                },
                {
                    "type": "web_fetch_tool_result",
                    "tool_use_id": "srv_1",
                    "content": {
                        "type": "web_fetch_tool_result_error",
                        "error_code": "url_not_accessible",
                    },
                },
                {"type": "text", "text": "根据 RSS 生成的 digest"},
            ],
        })
        with (
            patch.object(tasks, "AI_SESSION") as session,
            patch.object(
                tasks,
                "_ai_config",
                return_value=(
                    "key",
                    "https://api.anthropic.com/v1/messages",
                    "claude-opus-4-8",
                ),
            ),
            patch.object(tasks, "assert_safe_outbound_url"),
        ):
            session.post.return_value = response
            digest, status = tasks._ai_digest_with_anthropic_web_fetch(
                "prompt",
                "system",
                {"link": "https://publisher.example/paper"},
            )

        self.assertEqual(digest, "根据 RSS 生成的 digest")
        self.assertEqual(status, "page_error:url_not_accessible")

    def test_anthropic_pause_turn_continues_at_most_twice(self):
        first = self._response({
            "stop_reason": "pause_turn",
            "content": [{
                "type": "server_tool_use",
                "id": "srv_1",
                "name": "web_fetch",
                "input": {"url": "https://publisher.example/paper"},
            }],
        })
        second = self._response({
            "stop_reason": "pause_turn",
            "content": [{
                "type": "web_fetch_tool_result",
                "tool_use_id": "srv_1",
                "content": {"type": "web_fetch_result", "url": "https://publisher.example/paper"},
            }],
        })
        third = self._response({
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "continued digest"}],
        })
        with (
            patch.object(tasks, "AI_SESSION") as session,
            patch.object(tasks, "assert_safe_outbound_url"),
        ):
            session.post.side_effect = [first, second, third]
            message = tasks._chat_completion_request(
                [{"role": "user", "content": "fetch"}],
                "key",
                "https://api.anthropic.com/v1/messages",
                "claude-opus-4-8",
                anthropic_server_tools=[tasks._anthropic_web_fetch_tool(
                    tasks.ANTHROPIC_WEB_FETCH_LATEST,
                    ["publisher.example"],
                )],
            )

        self.assertEqual(message["content"], "continued digest")
        self.assertEqual(message["_anthropic_server_tool"]["continuations"], 2)
        self.assertEqual(session.post.call_count, 3)
        final_payload = json.loads(session.post.call_args.kwargs["data"].decode("utf-8"))
        self.assertEqual([m["role"] for m in final_payload["messages"]], [
            "user", "assistant", "assistant",
        ])

    def test_unsupported_tool_versions_trip_process_circuit(self):
        latest_error = self._response({
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "unknown tool web_fetch_20260318",
            },
        }, status=400)
        legacy_error = self._response({
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "unknown tool web_fetch_20250910",
            },
        }, status=400)
        plain_one = self._response({"content": [{"type": "text", "text": "plain one"}]})
        plain_two = self._response({"content": [{"type": "text", "text": "plain two"}]})
        config = (
            "key",
            "https://proxy.example/v1/messages",
            "claude-opus-4-8",
        )
        with (
            patch.object(tasks, "AI_SESSION") as session,
            patch.object(tasks, "_ai_config", return_value=config),
            patch.object(tasks, "assert_safe_outbound_url"),
        ):
            session.post.side_effect = [latest_error, legacy_error, plain_one, plain_two]
            first_digest, first_status = tasks._ai_digest_with_anthropic_web_fetch(
                "prompt", "system", {"link": "https://publisher.example/paper"}
            )
            second_digest, second_status = tasks._ai_digest_with_anthropic_web_fetch(
                "prompt", "system", {"link": "https://publisher.example/paper-2"}
            )

        self.assertEqual((first_digest, first_status), ("plain one", "unsupported"))
        self.assertEqual((second_digest, second_status), ("plain two", "unsupported"))
        self.assertEqual(session.post.call_count, 4)
        payloads = [
            json.loads(call.kwargs["data"].decode("utf-8"))
            for call in session.post.call_args_list
        ]
        self.assertEqual(payloads[0]["tools"][0]["type"], "web_fetch_20260318")
        self.assertEqual(payloads[1]["tools"][0]["type"], "web_fetch_20250910")
        self.assertNotIn("tools", payloads[2])
        self.assertNotIn("tools", payloads[3])


if __name__ == "__main__":
    unittest.main()
