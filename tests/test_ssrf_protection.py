import unittest
from unittest.mock import Mock, patch

import requests

import tasks
from auth import UnsafeOutboundURLError, resolve_public_outbound_url


class _FakeResponse:
    def __init__(self, status_code=200, *, location=""):
        self.status_code = status_code
        self.headers = {"Location": location} if location else {}
        self.url = ""
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True


class OutboundUrlValidationTests(unittest.TestCase):
    def test_literal_private_and_metadata_addresses_are_rejected(self):
        for url in (
            "http://127.0.0.1/feed",
            "http://10.0.0.8/feed",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/feed",
        ):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeOutboundURLError):
                    resolve_public_outbound_url(url)

    def test_hostname_resolving_to_any_private_address_is_rejected(self):
        with self.assertRaises(UnsafeOutboundURLError):
            resolve_public_outbound_url(
                "https://feeds.example/rss",
                resolver=lambda _host: ["8.8.8.8", "10.0.0.5"],
            )

    def test_public_addresses_are_returned_for_connection_pinning(self):
        url, addresses = resolve_public_outbound_url(
            "https://feeds.example/rss",
            resolver=lambda _host: ["8.8.8.8", "1.1.1.1", "8.8.8.8"],
        )
        self.assertEqual(url, "https://feeds.example/rss")
        self.assertEqual(addresses, ("8.8.8.8", "1.1.1.1"))


class SafeFeedFetchTests(unittest.TestCase):
    @staticmethod
    def _resolver(hostname):
        mapping = {
            "feeds.example": ["8.8.8.8"],
            "cdn.example": ["1.1.1.1"],
            "internal.example": ["10.0.0.9"],
        }
        return mapping[hostname]

    def test_request_connects_to_validated_ip_without_automatic_redirects(self):
        session = Mock()
        session.get.return_value = _FakeResponse()

        with patch.object(
            tasks,
            "_make_pinned_feed_session",
            return_value=session,
        ):
            response = tasks._request_pinned_feed_url(
                "https://feeds.example/news?q=1",
                ("8.8.8.8",),
                12,
            )

        session.get.assert_called_once()
        args, kwargs = session.get.call_args
        self.assertEqual(args[0], "https://8.8.8.8/news?q=1")
        self.assertEqual(kwargs["headers"]["Host"], "feeds.example")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], 12)
        self.assertEqual(response.url, "https://feeds.example/news?q=1")
        session.close.assert_called_once()

    def test_redirect_to_metadata_address_is_blocked_before_second_request(self):
        redirect = _FakeResponse(
            302,
            location="http://169.254.169.254/latest/meta-data/",
        )
        with patch.object(
            tasks,
            "_request_pinned_feed_url",
            return_value=redirect,
        ) as request:
            with self.assertRaises(UnsafeOutboundURLError):
                tasks.http_get(
                    "https://feeds.example/rss",
                    max_attempts=1,
                    resolver=self._resolver,
                )

        self.assertEqual(request.call_count, 1)
        self.assertTrue(redirect.closed)

    def test_redirect_to_hostname_resolving_private_is_blocked(self):
        redirect = _FakeResponse(301, location="http://internal.example/rss")
        with patch.object(
            tasks,
            "_request_pinned_feed_url",
            return_value=redirect,
        ) as request:
            with self.assertRaises(UnsafeOutboundURLError):
                tasks.http_get(
                    "https://feeds.example/rss",
                    max_attempts=1,
                    resolver=self._resolver,
                )

        self.assertEqual(request.call_count, 1)

    def test_safe_redirect_is_revalidated_and_uses_each_pinned_address(self):
        redirect = _FakeResponse(302, location="https://cdn.example/rss.xml")
        final = _FakeResponse(200)
        with patch.object(
            tasks,
            "_request_pinned_feed_url",
            side_effect=[redirect, final],
        ) as request:
            response = tasks.http_get(
                "https://feeds.example/rss",
                max_attempts=1,
                resolver=self._resolver,
            )

        self.assertIs(response, final)
        self.assertEqual(
            request.call_args_list[0].args[:2],
            ("https://feeds.example/rss", ("8.8.8.8",)),
        )
        self.assertEqual(
            request.call_args_list[1].args[:2],
            ("https://cdn.example/rss.xml", ("1.1.1.1",)),
        )

    def test_redirect_loop_is_bounded(self):
        redirect = _FakeResponse(302, location="/rss")
        with patch.object(
            tasks,
            "_request_pinned_feed_url",
            return_value=redirect,
        ):
            with self.assertRaises(requests.TooManyRedirects):
                tasks.http_get(
                    "https://feeds.example/rss",
                    max_attempts=1,
                    resolver=self._resolver,
                )


if __name__ == "__main__":
    unittest.main()
