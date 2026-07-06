import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import tasks


class ChatSearchToolTests(unittest.TestCase):
    def _config_value(self, key, default=""):
        values = {
            "ai.api_key": "test-key",
            "ai.base_url": "https://api.deepseek.com",
            "ai.model": "deepseek-chat",
            "ai.system_prompt": "系统提示",
        }
        return values.get(key, default)

    def test_ai_can_decide_no_search(self):
        captured = []

        def fake_completion(messages, api_key, base_url, model, tools=None, timeout=120):
            captured.append({
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
            })
            return {"role": "assistant", "content": "文章内容足以回答。"}

        with (
            patch.object(tasks, "_cfg", side_effect=self._config_value),
            patch.object(
                tasks,
                "_load_chat_pdf_text",
                return_value=("测试 PDF 原文", None),
            ),
            patch.object(tasks, "_chat_completion_request", side_effect=fake_completion),
            patch.object(tasks, "record_event"),
        ):
            reply = tasks.ai_chat(
                "test.html",
                "作者使用了什么方法？",
                history=[],
                web_search=True,
            )

        self.assertIn("AI判断无需联网检索", reply["reply"])
        self.assertEqual(len(captured), 1)
        self.assertIn("测试 PDF 原文", captured[0]["messages"][0]["content"])
        self.assertEqual(
            {tool["function"]["name"] for tool in captured[0]["tools"]},
            {"search_literature", "search_web"},
        )

    def test_ai_calls_literature_tool_and_receives_numbered_sources(self):
        captured = []
        responses = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "search_literature",
                        "arguments": json.dumps({
                            "query": "zircon oxygen isotope granite petrogenesis",
                            "limit": 6,
                        }),
                    },
                }],
            },
            {
                "role": "assistant",
                "content": "已有研究支持这一解释 [S1]。\n\n检索来源\n- 示例论文：https://doi.org/example",
            },
        ]

        def fake_completion(messages, api_key, base_url, model, tools=None, timeout=120):
            captured.append(copy.deepcopy(messages))
            return responses.pop(0)

        literature_result = {
            "results": [{
                "title": "示例论文",
                "url": "https://doi.org/example",
                "snippet": "摘要",
                "provider": "Crossref",
            }],
            "errors": [],
        }
        with (
            patch.object(tasks, "_cfg", side_effect=self._config_value),
            patch.object(
                tasks,
                "_load_chat_pdf_text",
                return_value=("测试 PDF 原文", None),
            ),
            patch.object(tasks, "_chat_completion_request", side_effect=fake_completion),
            patch.object(tasks, "search_literature", return_value=literature_result) as search,
            patch.object(tasks, "record_event"),
        ):
            reply = tasks.ai_chat(
                "test.html",
                "是否有其他研究支持？",
                history=[{"role": "assistant", "content": "前文结论"}],
                web_search=True,
            )

        self.assertIn("文献 1 次", reply["reply"])
        self.assertIn("[S1]", reply["reply"])
        search.assert_called_once_with(
            "zircon oxygen isotope granite petrogenesis", 6, None, None
        )
        tool_messages = [
            item for item in captured[1] if item.get("role") == "tool"
        ]
        self.assertEqual(len(tool_messages), 1)
        tool_payload = json.loads(tool_messages[0]["content"])
        self.assertEqual(tool_payload["results"][0]["citation_id"], "S1")

    def test_search_literature_deduplicates_provider_results(self):
        duplicate = {
            "title": "Same paper",
            "url": "https://doi.org/10.1000/test",
            "snippet": "abstract",
            "provider": "Crossref",
        }
        openalex_copy = dict(duplicate, provider="OpenAlex")
        with (
            patch.object(tasks, "_crossref_search", return_value=[duplicate]),
            patch.object(tasks, "_openalex_search", return_value=[openalex_copy]),
        ):
            response = tasks.search_literature("test query", limit=8)

        self.assertEqual(len(response["results"]), 1)
        self.assertEqual(response["results"][0]["provider"], "Crossref")
        self.assertEqual(response["errors"], [])

    def test_tool_call_and_result_budgets_are_enforced(self):
        calls = []
        tool_calls = []
        for index in range(4):
            tool_calls.append({
                "id": f"call-{index}",
                "type": "function",
                "function": {
                    "name": "search_literature",
                    "arguments": json.dumps({"query": f"query {index}", "limit": 8}),
                },
            })
        responses = [
            {"role": "assistant", "content": None, "tool_calls": tool_calls},
            {"role": "assistant", "content": "综合回答 [S20]"},
        ]

        def fake_completion(messages, api_key, base_url, model, tools=None, timeout=120):
            calls.append(copy.deepcopy(messages))
            return responses.pop(0)

        def fake_search(query, limit=8, year_from=None, year_to=None):
            return {
                "results": [{
                    "title": f"{query}-{index}",
                    "url": f"https://example.test/{query}/{index}",
                    "snippet": "摘要",
                    "provider": "Test",
                } for index in range(8)],
                "errors": [],
            }

        with (
            patch.object(tasks, "_cfg", side_effect=self._config_value),
            patch.object(
                tasks,
                "_load_chat_pdf_text",
                return_value=("测试 PDF 原文", None),
            ),
            patch.object(tasks, "_chat_completion_request", side_effect=fake_completion),
            patch.object(tasks, "search_literature", side_effect=fake_search) as search,
            patch.object(tasks, "record_event"),
        ):
            reply = tasks.ai_chat(
                "test.html", "检索相关研究", history=[], web_search=True
            )

        self.assertIn("文献 3 次", reply["reply"])
        self.assertEqual(search.call_count, 3)
        tool_messages = [item for item in calls[1] if item.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 3)
        numbered = [
            result
            for item in tool_messages
            for result in json.loads(item["content"])["results"]
        ]
        self.assertEqual(len(numbered), 20)
        self.assertEqual(numbered[-1]["citation_id"], "S20")

    def test_missing_inline_citations_gets_honest_source_fallback(self):
        answer = tasks._append_verified_search_sources(
            "根据检索结果可以进一步分析。",
            [{
                "citation_id": "S1",
                "title": "示例论文",
                "url": "https://doi.org/example",
                "snippet": "摘要",
                "provider": "Crossref",
            }],
        )

        self.assertIn("模型未在正文中逐条标注", answer)
        self.assertIn("[S1] 示例论文：https://doi.org/example", answer)

    def test_web_search_disabled_does_not_offer_tools(self):
        captured = []

        def fake_completion(messages, api_key, base_url, model, tools=None, timeout=120):
            captured.append(tools)
            return {"role": "assistant", "content": "普通回答"}

        with (
            patch.object(tasks, "_cfg", side_effect=self._config_value),
            patch.object(
                tasks,
                "_load_chat_pdf_text",
                return_value=("测试 PDF 原文", None),
            ),
            patch.object(tasks, "_chat_completion_request", side_effect=fake_completion),
        ):
            reply = tasks.ai_chat(
                "test.html", "解释文章", history=[], web_search=False
            )

        self.assertEqual(reply["reply"], "普通回答")
        self.assertEqual(captured, [None])

    def test_pdf_full_text_and_history_are_injected_on_every_turn(self):
        captured = []

        def fake_completion(messages, api_key, base_url, model, tools=None, timeout=120):
            captured.append(copy.deepcopy(messages))
            return {"role": "assistant", "content": "回答"}

        with (
            patch.object(tasks, "_cfg", side_effect=self._config_value),
            patch.object(
                tasks,
                "_load_chat_pdf_text",
                return_value=("每轮都必须出现的 PDF 全文", None),
            ) as load_pdf,
            patch.object(tasks, "_chat_completion_request", side_effect=fake_completion),
        ):
            first = tasks.ai_chat("test.html", "第一问", history=[])
            second = tasks.ai_chat(
                "test.html",
                "第二问",
                history=[
                    {"role": "user", "content": "第一问"},
                    {"role": "assistant", "content": "第一答"},
                ],
            )

        self.assertEqual(first["reply"], "回答")
        self.assertEqual(second["reply"], "回答")
        self.assertEqual(load_pdf.call_count, 2)
        for request_messages in captured:
            self.assertIn(
                "每轮都必须出现的 PDF 全文",
                request_messages[0]["content"],
            )
        self.assertEqual(
            [item["content"] for item in captured[1][1:]],
            ["第一问", "第一答", "第二问"],
        )

    def test_chat_pdf_loader_reads_all_pages_and_uses_file_identity_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "source.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\nsource")
            tasks._cached_chat_pdf_text.cache_clear()
            with (
                patch.object(tasks, "resolve_pdf_path", return_value=str(pdf_path)),
                patch.object(
                    tasks,
                    "_extract_pdf_text",
                    return_value="完整 PDF 文本",
                ) as extract,
            ):
                first = tasks._load_chat_pdf_text("digest.html")
                second = tasks._load_chat_pdf_text("digest.html")

        self.assertEqual(first, ("完整 PDF 文本", None))
        self.assertEqual(second, ("完整 PDF 文本", None))
        extract.assert_called_once_with(pdf_path.resolve(), max_pages=None)
        tasks._cached_chat_pdf_text.cache_clear()

    def test_chat_pdf_loader_accepts_only_current_tenant_uploaded_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            upload_root = Path(directory) / "uploaded_pdfs"
            upload_root.mkdir()
            selected = upload_root / "selected.pdf"
            selected.write_bytes(b"%PDF-1.4\nselected")
            paths = type("Paths", (), {"uploaded_pdfs_dir": upload_root})()
            tasks._cached_chat_pdf_text.cache_clear()
            with (
                patch.object(tasks, "current_tenant_paths", return_value=paths),
                patch.object(
                    tasks,
                    "_extract_pdf_text",
                    return_value="选择的 PDF 全文",
                ),
            ):
                loaded = tasks._load_chat_pdf_text(
                    "digest.html",
                    "selected.pdf",
                )
                escaped = tasks._load_chat_pdf_text(
                    "digest.html",
                    "../selected.pdf",
                )

        self.assertEqual(loaded, ("选择的 PDF 全文", None))
        self.assertIn("无效", escaped[1])
        tasks._cached_chat_pdf_text.cache_clear()

    def test_missing_or_failed_pdf_stops_before_ai_request(self):
        completion = Mock()
        with (
            patch.object(tasks, "_cfg", side_effect=self._config_value),
            patch.object(tasks, "resolve_pdf_path", return_value=None),
            patch.object(tasks, "_chat_completion_request", completion),
        ):
            missing = tasks.ai_chat("missing.html", "问题")
        self.assertIn("未找到", missing["reply"])
        completion.assert_not_called()

        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "broken.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\nbroken")
            tasks._cached_chat_pdf_text.cache_clear()
            with (
                patch.object(tasks, "_cfg", side_effect=self._config_value),
                patch.object(tasks, "resolve_pdf_path", return_value=str(pdf_path)),
                patch.object(tasks, "_extract_pdf_text", side_effect=ValueError("bad pdf")),
                patch.object(tasks, "_chat_completion_request", completion),
            ):
                failed = tasks.ai_chat("broken.html", "问题")
        self.assertIn("提取失败", failed["reply"])
        completion.assert_not_called()
        tasks._cached_chat_pdf_text.cache_clear()

        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "empty.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\nempty")
            tasks._cached_chat_pdf_text.cache_clear()
            with (
                patch.object(tasks, "_cfg", side_effect=self._config_value),
                patch.object(tasks, "resolve_pdf_path", return_value=str(pdf_path)),
                patch.object(tasks, "_extract_pdf_text", return_value=" \n "),
                patch.object(tasks, "_chat_completion_request", completion),
            ):
                empty = tasks.ai_chat("empty.html", "问题")
        self.assertIn("提取失败", empty["reply"])
        completion.assert_not_called()
        tasks._cached_chat_pdf_text.cache_clear()

    def test_context_overflow_compresses_history_and_retries_with_pdf(self):
        captured = []

        def fake_completion(messages, api_key, base_url, model, tools=None, timeout=120):
            captured.append(copy.deepcopy(messages))
            if len(captured) == 1:
                raise tasks.AIContextLengthError("too long")
            if len(captured) == 2:
                return {"role": "assistant", "content": "压缩后的历史"}
            return {"role": "assistant", "content": "基于全文的最终回答"}

        with (
            patch.object(tasks, "_cfg", side_effect=self._config_value),
            patch.object(
                tasks,
                "_load_chat_pdf_text",
                return_value=("PDF 全文标记", None),
            ),
            patch.object(tasks, "_chat_completion_request", side_effect=fake_completion),
        ):
            result = tasks.ai_chat(
                "test.html",
                "当前问题",
                history=[
                    {"role": "user", "content": "旧问题"},
                    {"role": "assistant", "content": "旧回答"},
                ],
                history_summary="更早的摘要",
            )

        self.assertEqual(result["reply"], "基于全文的最终回答")
        self.assertTrue(result["context_compressed"])
        self.assertIn("压缩后的历史", result["history_summary"])
        self.assertIn("更早的摘要", captured[1][1]["content"])
        self.assertIn("旧问题", captured[1][1]["content"])
        self.assertIn("PDF 全文标记", captured[2][0]["content"])
        self.assertIn("压缩后的历史", captured[2][0]["content"])
        self.assertEqual(captured[2][-1]["content"], "当前问题")

    def test_context_overflow_is_bounded_to_two_compression_rounds(self):
        calls = 0

        def fake_completion(messages, api_key, base_url, model, tools=None, timeout=120):
            nonlocal calls
            calls += 1
            if calls in {1, 3, 5}:
                raise tasks.AIContextLengthError("too long")
            return {"role": "assistant", "content": f"压缩摘要 {calls}"}

        with (
            patch.object(tasks, "_cfg", side_effect=self._config_value),
            patch.object(
                tasks,
                "_load_chat_pdf_text",
                return_value=("PDF 全文", None),
            ),
            patch.object(tasks, "_chat_completion_request", side_effect=fake_completion),
        ):
            result = tasks.ai_chat(
                "test.html",
                "问题",
                history=[{"role": "user", "content": "很长的历史"}],
            )

        self.assertEqual(calls, 5)
        self.assertTrue(result["context_compressed"])
        self.assertIn("压缩两轮后仍超过", result["reply"])

    def test_non_context_error_does_not_trigger_history_compression(self):
        with (
            patch.object(tasks, "_cfg", side_effect=self._config_value),
            patch.object(
                tasks,
                "_load_chat_pdf_text",
                return_value=("PDF 全文", None),
            ),
            patch.object(
                tasks,
                "_chat_completion_request",
                side_effect=RuntimeError("provider unavailable"),
            ) as completion,
        ):
            result = tasks.ai_chat(
                "test.html",
                "问题",
                history=[{"role": "user", "content": "旧问题"}],
            )

        self.assertEqual(completion.call_count, 1)
        self.assertFalse(result["context_compressed"])
        self.assertIn("provider unavailable", result["reply"])

    def test_provider_context_length_error_is_classified(self):
        response = Mock()
        response.status_code = 400
        response.json.return_value = {
            "error": {
                "code": "context_length_exceeded",
                "message": "maximum context length exceeded",
            }
        }
        with (
            patch.object(tasks, "AI_SESSION") as session,
            patch.object(tasks, "_safe_ai_endpoint", return_value="https://api.test/v1/chat/completions"),
        ):
            session.post.return_value = response
            with self.assertRaises(tasks.AIContextLengthError):
                tasks._chat_completion_request(
                    [{"role": "user", "content": "hello"}],
                    "key",
                    "https://api.test/v1",
                    "model",
                )
        response.raise_for_status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
