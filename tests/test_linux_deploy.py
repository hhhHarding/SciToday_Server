import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE_FILE = ROOT / "deploy" / "scitoday.service"
DEPLOY_README = ROOT / "deploy" / "README_DEPLOY.md"
DEPLOY_ENV = ROOT / "deploy" / "scitoday.env"
NGINX_CONFIG = ROOT / "frp" / "nginx" / "rssaipush.conf"


def _unit_entries():
    section = ""
    entries = []
    for raw_line in SERVICE_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            entries.append((section, key, value))
    return entries


class LinuxDeployConfigTests(unittest.TestCase):
    def test_start_limits_are_unit_directives(self):
        entries = _unit_entries()
        self.assertIn(("Unit", "StartLimitIntervalSec", "60"), entries)
        self.assertIn(("Unit", "StartLimitBurst", "5"), entries)
        self.assertFalse(
            any(
                section == "Service" and key.startswith("StartLimit")
                for section, key, _value in entries
            )
        )

    def test_service_can_only_write_server_data(self):
        entries = _unit_entries()
        self.assertIn(("Service", "ProtectSystem", "strict"), entries)
        self.assertIn(
            (
                "Service",
                "ReadWritePaths",
                "/opt/scitoday/ServerData",
            ),
            entries,
        )
        write_paths = [
            value
            for section, key, value in entries
            if section == "Service" and key == "ReadWritePaths"
        ]
        self.assertEqual(write_paths, ["/opt/scitoday/ServerData"])

    def test_deploy_does_not_give_service_account_ownership_of_code(self):
        readme = DEPLOY_README.read_text(encoding="utf-8")
        self.assertNotIn(
            "chown -R scitoday:scitoday /opt/scitoday\n",
            readme,
        )
        self.assertIn(
            "chown -R root:root /opt/scitoday/SciToday_Server /opt/scitoday/venv",
            readme,
        )
        self.assertIn(
            "chown -R scitoday:scitoday /opt/scitoday/ServerData",
            readme,
        )

    def test_reverse_proxy_is_explicit_and_hsts_is_owned_by_nginx(self):
        env = DEPLOY_ENV.read_text(encoding="utf-8")
        nginx = NGINX_CONFIG.read_text(encoding="utf-8")
        self.assertIn("RSSAI_TRUSTED_PROXY=127.0.0.1", env)
        self.assertNotIn("RSSAI_TRUSTED_PROXY=*", env)
        self.assertIn("proxy_set_header X-Forwarded-Proto $scheme;", nginx)
        self.assertIn("proxy_set_header X-Forwarded-Host $host;", nginx)
        self.assertIn(
            'add_header Strict-Transport-Security '
            '"max-age=31536000; includeSubDomains" always;',
            nginx,
        )
        self.assertIn(
            "proxy_hide_header Strict-Transport-Security;",
            nginx,
        )


if __name__ == "__main__":
    unittest.main()
