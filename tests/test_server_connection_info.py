import unittest

import tasks


class ServerConnectionInfoTests(unittest.TestCase):
    def test_configured_server_url_has_priority_over_quick_tunnel(self):
        config = {
            "pc": {
                "cloudflare_tunnel_url": "https://scitoday.example.com/",
            }
        }
        quick = {"url": "https://temporary.trycloudflare.com"}

        self.assertEqual(
            tasks._preferred_server_url(config, quick),
            "https://scitoday.example.com",
        )

    def test_quick_tunnel_is_used_when_server_url_is_empty(self):
        config = {"pc": {"cloudflare_tunnel_url": ""}}
        quick = {"url": "https://temporary.trycloudflare.com/"}

        self.assertEqual(
            tasks._preferred_server_url(config, quick),
            "https://temporary.trycloudflare.com",
        )


if __name__ == "__main__":
    unittest.main()
