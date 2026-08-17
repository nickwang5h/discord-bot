from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from core.info_curator_client import (
    InfoCuratorError,
    canonicalize_video_url,
    fetch_curated_video_brief,
    fetch_curated_video_summary,
    is_bilibili_url,
    is_supported_video_url,
    is_youtube_url,
)
from core.video_summary_worker import WorkerError, create_server, run_info_curator


class InfoCuratorClientTests(unittest.IsolatedAsyncioTestCase):
    def test_bilibili_input_is_canonical_and_rejects_short_or_multipart_links(self):
        self.assertTrue(is_bilibili_url("https://b23.tv/example"))
        self.assertTrue(is_youtube_url("https://youtu.be/dQw4w9WgXcQ"))
        self.assertTrue(is_supported_video_url("https://www.bilibili.com/video/BV1234567890"))
        self.assertTrue(is_supported_video_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
        self.assertFalse(is_supported_video_url("https://example.com/article"))
        self.assertEqual(
            canonicalize_video_url(
                "https://www.bilibili.com/video/BV1234567890?spm_id_from=tracking"
            ),
            "https://www.bilibili.com/video/BV1234567890",
        )
        self.assertEqual(
            canonicalize_video_url("https://youtu.be/dQw4w9WgXcQ"),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
        self.assertEqual(
            canonicalize_video_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s"),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
        with self.assertRaises(InfoCuratorError) as short:
            canonicalize_video_url("https://b23.tv/example")
        with self.assertRaises(InfoCuratorError) as multipart:
            canonicalize_video_url(
                "https://www.bilibili.com/video/BV1234567890?p=2"
            )
        self.assertEqual(short.exception.code, "unsupported_media_url")
        self.assertEqual(multipart.exception.code, "unsupported_media_url")

    async def test_external_or_https_worker_endpoint_is_rejected_before_network(self):
        for endpoint in (
            "https://video-summary:8080/v1/video-summary",
            "http://example.com:8080/v1/video-summary",
            "http://video-summary:8080/other",
        ):
            with self.subTest(endpoint=endpoint), patch.object(
                config, "INFO_CURATOR_SERVICE_URL", endpoint
            ):
                with self.assertRaises(InfoCuratorError) as caught:
                    await fetch_curated_video_summary(
                        "https://www.bilibili.com/video/BV1234567890"
                    )
                self.assertEqual(caught.exception.code, "worker_config_invalid")

    async def test_private_worker_result_is_strict_and_canonical_url_is_forwarded(self):
        server = create_server("127.0.0.1", 0)
        port = server.server_address[1]
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.05), daemon=True
        )
        thread.start()
        result = {
            "schema_version": "discord_video_summary_worker_v1",
            "status": "complete",
            "markdown": "# 视频总结\n\n带时间引用",
            "provider": "openrouter",
            "model": "fixture-model",
            "profile": "summary",
            "transcript_source": "automatic_subtitle",
            "language": "ai-zh",
            "reused": False,
            "media_reused": True,
        }
        try:
            with (
                patch.object(
                    config,
                    "INFO_CURATOR_SERVICE_URL",
                    f"http://127.0.0.1:{port}/v1/video-summary",
                ),
                patch("core.video_summary_worker.run_info_curator", return_value=result) as run,
            ):
                summary = await fetch_curated_video_summary(
                    "https://www.bilibili.com/video/BV1234567890?tracking=1"
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(summary.provider, "openrouter")
        self.assertEqual(summary.profile, "summary")
        self.assertTrue(summary.media_reused)
        run.assert_called_once_with("https://www.bilibili.com/video/BV1234567890")

    async def test_brief_client_forwards_only_the_explicit_brief_profile(self):
        server = create_server("127.0.0.1", 0)
        port = server.server_address[1]
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.05), daemon=True
        )
        thread.start()
        result = {
            "schema_version": "discord_video_summary_worker_v1",
            "status": "complete",
            "markdown": "# 标题：fixture\n\n## 一句话总结\n\n核心结论",
            "provider": "groq",
            "model": "qwen/qwen3.6-27b",
            "profile": "brief",
            "transcript_source": "automatic_subtitle",
            "language": "ai-zh",
            "reused": False,
            "media_reused": True,
        }
        try:
            with (
                patch.object(
                    config,
                    "INFO_CURATOR_SERVICE_URL",
                    f"http://127.0.0.1:{port}/v1/video-summary",
                ),
                patch("core.video_summary_worker.run_info_curator", return_value=result) as run,
            ):
                summary = await fetch_curated_video_brief(
                    "https://www.bilibili.com/video/BV1234567890?tracking=1"
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(summary.profile, "brief")
        self.assertEqual(summary.transcript_source, "automatic_subtitle")
        run.assert_called_once_with(
            "https://www.bilibili.com/video/BV1234567890", profile="brief"
        )

    async def test_worker_error_is_mapped_without_exposing_subprocess_text(self):
        server = create_server("127.0.0.1", 0)
        port = server.server_address[1]
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.05), daemon=True
        )
        thread.start()
        try:
            with (
                patch.object(
                    config,
                    "INFO_CURATOR_SERVICE_URL",
                    f"http://127.0.0.1:{port}/v1/video-summary",
                ),
                patch(
                    "core.video_summary_worker.run_info_curator",
                    side_effect=WorkerError("provider_invalid_json"),
                ),
            ):
                with self.assertRaises(InfoCuratorError) as caught:
                    await fetch_curated_video_summary(
                        "https://www.bilibili.com/video/BV1234567890"
                    )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(caught.exception.code, "provider_invalid_json")
        self.assertNotIn("subprocess", caught.exception.message)


class VideoSummaryWorkerTests(unittest.TestCase):
    def test_worker_invokes_cli_as_argument_array_and_validates_success_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "fake-info-curator"
            executable.write_text(
                """#!/usr/bin/python3
import json
import pathlib
import sys
assert sys.argv[1] == "summarize-video"
assert sys.argv[3] == "--output"
output = pathlib.Path(sys.argv[4])
output.write_text("# fixture\\n\\n- [00:01](https://example.test?t=1)\\n", encoding="utf-8")
output.chmod(0o600)
print(json.dumps({
    "schema_version": "content_enrichment_cli_summary_v1",
    "status": "complete",
    "profile": "summary",
    "reused": False,
    "media_reused": True,
    "provider": "fixture-provider",
    "model": "fixture-model",
    "transcript_source": "automatic_subtitle",
    "language": "ai-zh",
    "output": str(output),
}))
""",
                encoding="utf-8",
            )
            executable.chmod(0o700)

            result = run_info_curator(
                "https://www.bilibili.com/video/BV1234567890;not-a-shell-command",
                executable=str(executable),
                timeout_seconds=30,
            )

        self.assertEqual(result["schema_version"], "discord_video_summary_worker_v1")
        self.assertEqual(result["provider"], "fixture-provider")
        self.assertEqual(result["profile"], "summary")
        self.assertIn("00:01", result["markdown"])

    def test_worker_adds_brief_profile_to_the_cli_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "fake-info-curator"
            executable.write_text(
                '''#!/usr/bin/python3
import json
import pathlib
import sys
assert sys.argv[1:6] == ["summarize-video", sys.argv[2], "--profile", "brief", "--output"]
output = pathlib.Path(sys.argv[6])
output.write_text("# 标题：fixture\\n\\n## 一句话总结\\n\\n核心结论\\n", encoding="utf-8")
output.chmod(0o600)
print(json.dumps({
    "schema_version": "content_enrichment_cli_summary_v1",
    "status": "complete",
    "profile": "brief",
    "reused": False,
    "media_reused": True,
    "provider": "groq",
    "model": "qwen/qwen3.6-27b",
    "transcript_source": "automatic_subtitle",
    "language": "ai-zh",
    "output": str(output),
}))
''',
                encoding="utf-8",
            )
            executable.chmod(0o700)
            result = run_info_curator(
                "https://www.bilibili.com/video/BV1234567890",
                executable=str(executable),
                timeout_seconds=30,
                profile="brief",
            )

        self.assertEqual(result["profile"], "brief")
        self.assertEqual(result["transcript_source"], "automatic_subtitle")

    def test_worker_propagates_only_allowlisted_cli_error_code(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "fake-info-curator"
            executable.write_text(
                """#!/usr/bin/python3
import json
import sys
print(json.dumps({
    "schema_version": "content_enrichment_cli_error_v1",
    "error_code": "media_not_available",
    "message": "private upstream detail",
}), file=sys.stderr)
raise SystemExit(2)
""",
                encoding="utf-8",
            )
            executable.chmod(0o700)

            with self.assertRaises(WorkerError) as caught:
                run_info_curator(
                    "https://www.bilibili.com/video/BV1234567890",
                    executable=str(executable),
                    timeout_seconds=30,
                )

        self.assertEqual(caught.exception.code, "media_not_available")
        self.assertNotIn("private upstream detail", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
