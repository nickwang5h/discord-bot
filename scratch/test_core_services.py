import asyncio
import calendar
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core import news_cache, settings
from core.ai_providers import (
    AIResult,
    ModelSpec,
    ProviderError,
    clean_model_content,
    request_openai_compatible,
)
from core.feeds import FeedSource, _parse_feed
from core.jobs import RetryPolicy, retry_async
from core.storage import JsonStore
from core.web_fetcher import UnsafeUrlError, _validate_public_url


class JsonStoreTests(unittest.TestCase):
    def test_update_writes_valid_json_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "settings.json"
            store = JsonStore(path, dict)

            store.update(lambda data: {**data, "channel": "123"})

            self.assertEqual(store.read(strict=True), {"channel": "123"})
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_strict_read_rejects_corrupt_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("{broken", encoding="utf-8")
            store = JsonStore(path, dict)

            with self.assertRaisesRegex(RuntimeError, "无法读取 JSON"):
                store.read(strict=True)

    def test_secret_is_not_written_to_public_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            public_store = JsonStore(Path(directory) / "settings.json", dict)
            secret_store = JsonStore(Path(directory) / "data" / "secrets.json", dict)
            with (
                patch.object(settings, "_settings_store", public_store),
                patch.object(settings, "_secrets_store", secret_store),
            ):
                settings.set_secret("GEMINI_API_KEY", "secret-value")

                self.assertNotIn("GEMINI_API_KEY", settings.load_settings())
                self.assertEqual(settings.get_secret("GEMINI_API_KEY"), "secret-value")

    def test_example_placeholder_is_not_treated_as_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            public_store = JsonStore(Path(directory) / "settings.json", dict)
            secret_store = JsonStore(Path(directory) / "secrets.json", dict)
            with (
                patch.object(settings, "_settings_store", public_store),
                patch.object(settings, "_secrets_store", secret_store),
                patch.dict("os.environ", {"GROQ_API_KEY": "your_groq_api_key_here"}),
            ):
                self.assertIsNone(settings.get_secret("GROQ_API_KEY"))

    def test_news_cache_deduplicates_one_input_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_store = JsonStore(Path(directory) / "news.json", list)
            items = [
                {"title": "Same", "url": "https://example.com/1"},
                {"title": "Same", "url": "https://example.com/2"},
            ]
            with patch.object(news_cache, "_cache_store", cache_store):
                added = news_cache.add_items(items)

                self.assertEqual(added, 1)
                self.assertEqual(len(news_cache.load_cache()), 1)

    def test_news_cache_prunes_legacy_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_store = JsonStore(Path(directory) / "news.json", list)
            current = {
                "title": "Current",
                "url": "https://example.com/current",
                "relevance_score": 0.8,
                "novelty_score": 0.7,
                "quality_score": 0.9,
                "llm_interestingness": 0.6,
                "cross_domain_bridge": 0.5,
                "discovery_score": 0.72,
            }
            legacy = {
                "title": "Legacy",
                "url": "https://example.com/legacy",
                "theme_score": 8,
                "serendipity_score": 7,
            }
            cache_store.write([legacy, current])

            with patch.object(news_cache, "_cache_store", cache_store):
                removed = news_cache.prune_legacy_items()

                self.assertEqual(removed, 1)
                self.assertEqual(news_cache.load_cache(), [current])


class FeedParsingTests(unittest.TestCase):
    def test_feed_timestamp_is_interpreted_as_utc(self):
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel><title>Test</title>
          <item><title>Story</title><link>https://example.com/story</link>
          <description>Summary</description><pubDate>Wed, 22 Jul 2026 12:00:00 GMT</pubDate></item>
        </channel></rss>"""

        items = _parse_feed(
            xml,
            FeedSource("World", "https://example.com/rss"),
            max_age_seconds=None,
            max_items=5,
        )

        expected = calendar.timegm(datetime(2026, 7, 22, 12, tzinfo=timezone.utc).timetuple())
        self.assertEqual(items[0].published_at, expected)
        self.assertEqual(items[0].title, "Story")


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_uses_backoff_and_returns_result(self):
        operation = AsyncMock(side_effect=[RuntimeError("one"), RuntimeError("two"), "ok"])
        sleep = AsyncMock()

        result = await retry_async(
            "test",
            operation,
            policy=RetryPolicy(
                attempts=3,
                initial_delay_seconds=2,
                backoff_factor=3,
                max_delay_seconds=5,
            ),
            sleep=sleep,
        )

        self.assertEqual(result, "ok")
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [2, 5])


class AIProviderValueTests(unittest.TestCase):
    def test_reasoning_is_removed_and_attribution_is_structured(self):
        result = AIResult(clean_model_content("<think>hidden</think>\nanswer"), "Groq", "model")

        self.assertEqual(result.text, "answer")
        self.assertEqual(result.attribution, "Groq (model)")
        self.assertIn("<!--MODEL:Groq (model)-->", result.as_legacy_text())


class AIProviderTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_and_provider_controls_are_added_to_payload(self):
        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def json(self):
                return {
                    "choices": [
                        {
                            "message": {"content": "complete answer"},
                            "finish_reason": "stop",
                        }
                    ]
                }

        class FakeSession:
            def __init__(self):
                self.payload = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def post(self, *_args, **kwargs):
                self.payload = kwargs["json"]
                return FakeResponse()

        session = FakeSession()
        with patch("core.ai_providers.aiohttp.ClientSession", return_value=session):
            result = await request_openai_compatible(
                provider="test",
                endpoint="https://example.com/chat",
                api_key="secret",
                models=[
                    ModelSpec(
                        "reasoning-model",
                        reasoning_effort="none",
                        reasoning_format="hidden",
                    )
                ],
                text="hello",
                system="",
                json_mode=False,
                timeout_seconds=10,
                max_output_tokens=100,
                extra_payload={"thinking": {"type": "disabled"}},
            )

        self.assertEqual(result.text, "complete answer")
        self.assertEqual(session.payload["reasoning_effort"], "none")
        self.assertEqual(session.payload["reasoning_format"], "hidden")
        self.assertEqual(session.payload["thinking"], {"type": "disabled"})

    async def test_length_limited_response_falls_through_to_next_model(self):
        class FakeResponse:
            status = 200

            def __init__(self, data):
                self.data = data

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def json(self):
                return self.data

        class FakeSession:
            def __init__(self):
                self.responses = [
                    {
                        "choices": [
                            {
                                "message": {"content": "truncated ans"},
                                "finish_reason": "length",
                            }
                        ]
                    },
                    {
                        "choices": [
                            {
                                "message": {"content": "complete answer"},
                                "finish_reason": "stop",
                            }
                        ]
                    },
                ]
                self.models = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def post(self, *_args, **kwargs):
                self.models.append(kwargs["json"]["model"])
                return FakeResponse(self.responses.pop(0))

        session = FakeSession()
        with patch("core.ai_providers.aiohttp.ClientSession", return_value=session):
            result = await request_openai_compatible(
                provider="test",
                endpoint="https://example.com/chat",
                api_key="secret",
                models=[ModelSpec("thinking"), ModelSpec("fallback")],
                text="hello",
                system="",
                json_mode=False,
                timeout_seconds=10,
                max_output_tokens=100,
            )

        self.assertEqual(result.text, "complete answer")
        self.assertEqual(result.model, "fallback")
        self.assertEqual(session.models, ["thinking", "fallback"])

    async def test_payload_too_large_does_not_retry_same_payload_on_other_models(self):
        class FakeResponse:
            status = 413

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def text(self):
                return "request too large"

        class FakeSession:
            def __init__(self):
                self.calls = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def post(self, *_args, **_kwargs):
                self.calls += 1
                return FakeResponse()

        session = FakeSession()
        with patch("core.ai_providers.aiohttp.ClientSession", return_value=session):
            with self.assertRaises(ProviderError):
                await request_openai_compatible(
                    provider="test",
                    endpoint="https://example.com/chat",
                    api_key="secret",
                    models=[ModelSpec("one"), ModelSpec("two")],
                    text="hello",
                    system="",
                    json_mode=False,
                    timeout_seconds=10,
                    max_output_tokens=100,
                )

        self.assertEqual(session.calls, 1)


class UrlSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_loopback_url_is_rejected(self):
        with self.assertRaisesRegex(UnsafeUrlError, "本机"):
            await _validate_public_url("http://127.0.0.1/admin")

    async def test_url_credentials_are_rejected(self):
        with self.assertRaisesRegex(UnsafeUrlError, "登录凭证"):
            await _validate_public_url("https://user:pass@example.com/")


if __name__ == "__main__":
    unittest.main(verbosity=2)
