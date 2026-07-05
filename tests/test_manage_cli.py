import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import manage
from server_config import ServerPaths
from tenancy.registry import TenantRegistry


class ManageCliTests(unittest.TestCase):
    def test_init_create_and_list_use_selected_data_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir) / "server"
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    manage.main(["--data-dir", str(data_root), "init"]),
                    0,
                )
                self.assertEqual(
                    manage.main(
                        [
                            "--data-dir",
                            str(data_root),
                            "tenant",
                            "create",
                            "--name",
                            "测试租户",
                            "--id",
                            "t_cli",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    manage.main(["--data-dir", str(data_root), "tenant", "list"]),
                    0,
                )

            registry = TenantRegistry(ServerPaths(data_root))
            tenant_ids = {tenant.id for tenant in registry.list_tenants()}
            rendered = output.getvalue()

        self.assertEqual(tenant_ids, {"owner", "t_cli"})
        self.assertIn("owner", rendered)
        self.assertIn("t_cli", rendered)
        self.assertIn("测试租户", rendered)

    def test_token_and_operator_commands_do_not_persist_plaintext_in_control_db(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_root = Path(temp_dir) / "server"
            output = io.StringIO()
            with redirect_stdout(output):
                manage.main(["--data-dir", str(data_root), "init"])
                manage.main(
                    [
                        "--data-dir",
                        str(data_root),
                        "token",
                        "create",
                        "owner",
                        "--scope",
                        "app",
                        "--scope",
                        "tenant_admin",
                    ]
                )
                token_id = next(
                    line.split(":", 1)[1].strip()
                    for line in output.getvalue().splitlines()
                    if line.startswith("token id:")
                )
                manage.main(
                    [
                        "--data-dir",
                        str(data_root),
                        "token",
                        "scopes",
                        token_id,
                        "--scope",
                        "app",
                        "--scope",
                        "ai_config_write",
                    ]
                )
                manage.main(
                    [
                        "--data-dir",
                        str(data_root),
                        "operator",
                        "create",
                    ]
                )
            rendered = output.getvalue()
            self.assertIn("scopes: ai_config_write,app", rendered)
            tenant_plaintext = next(
                line for line in rendered.splitlines() if line.startswith("rssai_tk_")
            )
            operator_plaintext = next(
                line for line in rendered.splitlines() if line.startswith("rssai_op_")
            )
            control_bytes = (data_root / "control" / "control.db").read_bytes()

        self.assertNotIn(tenant_plaintext.encode(), control_bytes)
        self.assertNotIn(operator_plaintext.encode(), control_bytes)


if __name__ == "__main__":
    unittest.main()
