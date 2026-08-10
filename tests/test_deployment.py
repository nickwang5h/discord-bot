import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DeploymentScriptTests(unittest.TestCase):
    def test_success_status_uses_persisted_compose_environment(self):
        cases = (
            ("deploy.sh", '"${compose_candidate[@]}" ps'),
            ("rollback.sh", '"${compose[@]}" ps'),
        )
        for script_name, stale_command in cases:
            with self.subTest(script=script_name):
                text = (PROJECT_ROOT / "ops" / "vps" / script_name).read_text(
                    encoding="utf-8"
                )
                success_tail = text.split('mv "$candidate_env" "$deploy_env"', 1)[1]
                self.assertIn('--env-file "$deploy_env"', success_tail)
                self.assertNotIn(stale_command, success_tail)

    def test_operator_cli_help_does_not_require_network_or_configuration(self):
        result = subprocess.run(
            ["bash", "scripts/vps.sh", "help"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("deploy", result.stdout)
        self.assertIn("rollback", result.stdout)

    def test_operator_cli_rejects_invalid_log_count_before_ssh(self):
        result = subprocess.run(
            ["bash", "scripts/vps.sh", "logs", "0"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Log line count", result.stderr)

    def test_compatibility_deploy_entry_routes_to_operator_cli(self):
        text = (PROJECT_ROOT / "scripts" / "deploy_vps.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn('vps.sh" deploy', text)

    def test_compose_has_no_published_ports_and_keeps_single_service(self):
        text = (PROJECT_ROOT / "ops" / "vps" / "compose.yaml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("\n    ports:", text)
        self.assertIn("restart: unless-stopped", text)
        self.assertIn("read_only: true", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
