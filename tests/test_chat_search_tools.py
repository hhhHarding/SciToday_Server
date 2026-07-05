import copy
import json
import unittest
from unittest.mock import patch

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
            patch.object(tasks, "get_digest_text", return_value="测试文章内容"),
            patch.object(tasks, "_chat_completion_request", side_effect=fake_completion),
            patch.object(tasks, "record_event"),
        ):
            reply = tasks.ai_chat(
                "test.html",
                "作者使用了什么方法？",
                history=[],
                web_search=True,
            )

        self.assertIn("AI判断无需联网检索", reply)
        self.assertEqual(len(captured), 1)
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
            patch.object(tasks, "get_digest_text", return_value="测试文章内容"),
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

        self.assertIn("文献 1 次", reply)
        self.assertIn("[S1]", reply)
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
            patch.object(tasks, "get_digest_text", return_value="测试文章内容"),
            patch.object(tasks, "_chat_completion_request", side_effect=fake_completion),
            patch.object(tasks, "search_literature", side_effect=fake_search) as search,
            patch.object(tasks, "record_event"),
        ):
            reply = tasks.ai_chat(
                "test.html", "检索相关研究", history=[], web_search=True
            )

        self.assertIn("文献 3 次", reply)
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
            patch.object(tasks, "get_digest_text", return_value="测试文章内容"),
            patch.object(tasks, "_chat_completion_request", side_effect=fake_completion),
        ):
            reply = tasks.ai_chat(
                "test.html", "解释文章", history=[], web_search=False
            )

        self.assertEqual(reply, "普通回答")
        self.assertEqual(captured, [None])


if __name__ == "__main__":
    unittest.main()
