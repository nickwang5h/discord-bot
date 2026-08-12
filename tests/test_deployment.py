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

    def test_compose_isolates_bot_and_video_sidecar_without_published_ports(self):
        text = (PROJECT_ROOT / "ops" / "vps" / "compose.yaml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("\n    ports:", text)
        self.assertNotIn("/var/run/docker.sock", text)
        self.assertIn("  video-summary:", text)
        self.assertIn("  bot:", text)
        self.assertIn("condition: service_healthy", text)
        self.assertIn("/run/info-curator:ro", text)
        self.assertIn("/run/media-transcriber:ro", text)
        self.assertGreaterEqual(text.count("read_only: true"), 2)

    def test_video_sidecar_uses_pinned_python_and_source_only_named_contexts(self):
        text = (PROJECT_ROOT / "ops" / "vps" / "VideoSummary.Dockerfile").read_text(
            encoding="utf-8"
        )

        self.assertRegex(text, r"FROM python:3\.12\.\d+-slim-bookworm@sha256:[a-f0-9]{64}")
        self.assertIn("COPY --from=info_curator /src", text)
        self.assertIn("COPY --from=media_transcriber /src", text)
        self.assertNotIn("COPY --from=info_curator / /", text)
        self.assertIn("USER 1000:1000", text)

    def test_deploy_builds_one_manifest_from_three_clean_checkouts(self):
        text = (PROJECT_ROOT / "ops" / "vps" / "deploy.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn('info_repo=${INFO_CURATOR_REPO:-/srv/info-curator/source}', text)
        self.assertIn('media_repo=${MEDIA_TRANSCRIBER_REPO:-/srv/media-transcriber/source}', text)
        self.assertIn('update_repo "Discord Bot"', text)
        self.assertIn('update_repo "Info Curator"', text)
        self.assertIn('update_repo "Media Transcriber"', text)
        self.assertIn('printf \'%s\\n%s\\n%s\\n\'', text)
        rollback = (PROJECT_ROOT / "ops" / "vps" / "rollback.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('docker image inspect "discord-video-summary:$release"', rollback)
        self.assertIn("--no-deps --no-build bot", rollback)
        self.assertIn(
            "--no-deps --no-build bot",
            (PROJECT_ROOT / "ops" / "vps" / "deploy.sh").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
