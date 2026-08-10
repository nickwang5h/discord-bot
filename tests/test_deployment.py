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

    def test_compose_has_no_published_ports_and_keeps_single_service(self):
        text = (PROJECT_ROOT / "ops" / "vps" / "compose.yaml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("\n    ports:", text)
        self.assertIn("restart: unless-stopped", text)
        self.assertIn("read_only: true", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
