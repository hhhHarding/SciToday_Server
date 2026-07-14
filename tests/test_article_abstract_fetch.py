import unittest
from unittest.mock import patch

import requests

import push
import tasks
from auth import UnsafeOutboundURLError


ABSTRACT = (
    "This study evaluates a reproducible method for extracting scientific "
    "article abstracts from publisher web pages."
)


class _Response:
    def __init__(self, html=b"", status=200, content_type="text/html; charset=utf-8"):
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.url = "https://publisher.example/article/final"
        self.encoding = "utf-8"
        self._html = html
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for offset in range(0, len(self._html), chunk_size):
            yield self._html[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class ArticleAbstractParserTests(unittest.TestCase):
    def test_citation_abstract_has_priority(self):
        html = f"""
            <html><head>
            <meta name="description" content="A generic publisher landing page description that is deliberately long enough.">
            <meta name="citation_abstract" content="{ABSTRACT}">
            </head></html>
        """
        text, source = tasks._extract_original_abstract_from_html(html)
        self.assertEqual(text, ABSTRACT)
        self.assertEqual(source, "meta:citation_abstract")

    def test_json_ld_and_visible_abstract_fallbacks(self):
        json_ld = f"""
            <script type="application/ld+json">
            {{"@type":"ScholarlyArticle","abstract":"{ABSTRACT}"}}
            </script>
        """
        text, source = tasks._extract_original_abstract_from_html(json_ld)
        self.assertEqual(text, ABSTRACT)
        self.assertEqual(source, "jsonld:abstract")

        visible = f'<section class="article-abstract"><h2>Abstract</h2><p>{ABSTRACT}</p></section>'
        text, source = tasks._extract_original_abstract_from_html(visible)
        self.assertEqual(text, ABSTRACT)
        self.assertEqual(source, "element:abstract-container")


class ArticleAbstractFetchTests(unittest.TestCase):
    def test_fetch_is_streamed_bounded_and_closes_response(self):
        response = _Response(
            f'<meta name="citation_abstract" content="{ABSTRACT}">'.encode()
        )
        with patch.object(tasks, "http_get", return_value=response) as get:
            result = tasks.fetch_original_abstract("https://publisher.example/article")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["text"], ABSTRACT)
        self.assertEqual(result["final_url"], response.url)
        self.assertTrue(response.closed)
        self.assertTrue(get.call_args.kwargs["stream"])
        self.assertEqual(get.call_args.kwargs["max_attempts"], 1)

    def test_unsafe_and_http_failures_are_non_fatal(self):
        with patch.object(
            tasks,
            "http_get",
            side_effect=UnsafeOutboundURLError("private address"),
        ):
            result = tasks.fetch_original_abstract("http://127.0.0.1/private")
        self.assertEqual(result["status"], "unsafe_url")
        self.assertEqual(result["text"], "")

        response = _Response(status=403)
        with patch.object(tasks, "http_get", return_value=response):
            result = tasks.fetch_original_abstract("https://publisher.example/blocked")
        self.assertEqual(result["status"], "http_403")
        self.assertTrue(response.closed)

    def test_enrichment_failure_keeps_rss_item_usable(self):
        item = {"title": "Paper", "link": "https://publisher.example/article", "summary": "RSS summary"}
        with patch.object(
            tasks,
            "fetch_original_abstract",
            return_value={
                "text": "",
                "source": "",
                "status": "timeout",
                "final_url": "",
                "truncated": False,
            },
        ):
            enriched = tasks.enrich_rss_item_with_original_abstract(
                item,
                config={"rss": {"fetch_original_abstract": True}},
            )
        self.assertEqual(enriched["summary"], "RSS summary")
        self.assertEqual(enriched["original_abstract_status"], "timeout")

    def test_ai_prompt_and_notification_include_original_abstract(self):
        item = {
            "title": "Paper",
            "summary": "RSS summary",
            "original_abstract": ABSTRACT,
            "link": "https://publisher.example/article",
        }
        with patch.object(tasks, "_env_or_cfg", return_value="key"), patch.object(
            tasks, "_cfg", return_value="prompt"
        ), patch.object(tasks, "_ai_call", return_value="digest") as ai, patch.object(
            tasks, "_ai_digest_with_anthropic_web_fetch"
        ) as web_fetch:
            digest = tasks.ai_digest_one(item)
        self.assertTrue(digest.startswith("digest"))
        self.assertIn("【原文摘要】", digest)
        self.assertIn(ABSTRACT, digest)
        self.assertIn("【原文页面提取摘要】", ai.call_args.args[0])
        self.assertIn(ABSTRACT, ai.call_args.args[0])
        web_fetch.assert_not_called()

        with patch.object(push, "send_notification", return_value=True) as send:
            self.assertTrue(
                push.send_digest_notification(
                    "中文标题",
                    "关键词",
                    "digest.html",
                    original_abstract=ABSTRACT * 3,
                )
            )
        message = send.call_args.args[1]
        self.assertIn("摘要:", message)
        self.assertIn("…", send.call_args.args[1])
        self.assertLess(message.index("摘要:"), message.index("关键词:"))

    def test_notification_summary_fallback_priority(self):
        digest = "中文题目：测试\n中文关键词：甲、乙\n这是 digest 正文预览。"
        item = {
            "original_abstract": "Python abstract",
            "web_fetch_status": "success",
            "summary": "RSS summary",
        }
        self.assertEqual(tasks.notification_summary_for_item(item, digest), "Python abstract")

        item["original_abstract"] = ""
        self.assertEqual(
            tasks.notification_summary_for_item(item, digest),
            "这是 digest 正文预览。",
        )

        item["web_fetch_status"] = "page_error:url_not_accessible"
        self.assertEqual(tasks.notification_summary_for_item(item, digest), "RSS summary")

        item["summary"] = ""
        self.assertEqual(
            tasks.notification_summary_for_item(item, digest),
            "这是 digest 正文预览。",
        )

    def test_fetch_original_abstract_switch_disables_both_fetchers(self):
        item = {
            "link": "https://publisher.example/paper",
            "original_abstract_status": "timeout",
        }
        eligible, status = tasks._anthropic_web_fetch_eligibility(
            item,
            config={
                "rss": {
                    "fetch_original_abstract": False,
                    "anthropic_web_fetch_enabled": True,
                }
            },
        )
        self.assertFalse(eligible)
        self.assertEqual(status, "disabled")

    def test_python_failure_routes_existing_digest_to_anthropic_web_fetch(self):
        item = {
            "title": "Paper",
            "summary": "RSS summary",
            "link": "https://publisher.example/paper",
            "original_abstract": "",
            "original_abstract_status": "http_403",
        }
        cfg = {
            "rss": {
                "fetch_original_abstract": True,
                "anthropic_web_fetch_enabled": True,
            }
        }
        with (
            patch.object(tasks, "_env_or_cfg", return_value="key"),
            patch.object(tasks, "_cfg", return_value="prompt"),
            patch.object(tasks, "load_config", return_value=cfg),
            patch.object(
                tasks,
                "_ai_config",
                return_value=(
                    "key",
                    "https://api.anthropic.com/v1/messages",
                    "claude-opus-4-8",
                ),
            ),
            patch.object(
                tasks,
                "_ai_digest_with_anthropic_web_fetch",
                return_value=("web digest", "success"),
            ) as web_fetch,
            patch.object(tasks, "_ai_call") as plain_ai,
        ):
            digest = tasks.ai_digest_one(item)

        self.assertEqual(digest, "web digest")
        self.assertEqual(item["web_fetch_status"], "success")
        self.assertIn("https://publisher.example/paper", web_fetch.call_args.args[0])
        plain_ai.assert_not_called()


if __name__ == "__main__":
    unittest.main()
