import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.advanced_news import AdvancedNews
from cogs.ai_daily import AIDaily
from core import ai_client
from core.ai_providers import AIResult
from core.utils import create_ai_embed, normalize_markdown_tables


class MarkdownNormalizationTests(unittest.TestCase):
    def test_markdown_table_is_converted_to_bullets(self):
        source = (
            "### 📈 金融市场\n"
            "| 新闻 | 摘要 | 链接 |\n"
            "| --- | --- | --- |\n"
            "| 降息预期升温 | 债券收益率回落 | https://example.com/rates |\n"
            "| 科技股反弹 | 芯片板块领涨 | https://example.com/tech |"
        )

        result = normalize_markdown_tables(source)

        self.assertNotIn("| ---", result)
        self.assertIn("- **降息预期升温**", result)
        self.assertIn("**摘要**：债券收益率回落", result)
        self.assertIn("https://example.com/tech", result)

    def test_embed_keeps_model_footer_after_table_conversion(self):
        description = (
            "| 标题 | 摘要 |\n"
            "| --- | --- |\n"
            "| A | B |\n\n"
            "<!--MODEL:Groq (openai/gpt-oss-120b)-->"
        )

        embed = create_ai_embed("日报", description)

        self.assertEqual(embed.footer.text, "✨ Powered by Groq (openai/gpt-oss-120b)")
        self.assertIn("- **A** — B", embed.description)
        self.assertNotIn("<!--MODEL", embed.description)


class AIClientFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        ai_client.gemini_cooldown_until = 0.0

    async def test_missing_gemini_uses_configured_fallback(self):
        groq = AsyncMock(return_value=AIResult("fallback", "Groq", "test"))
        with (
            patch.object(ai_client, "model_available", False),
            patch.object(ai_client, "client", None),
            patch.object(ai_client, "_ask_groq", groq),
        ):
            result = await ai_client.ask_ai("hello")

        self.assertIn("fallback", result)
        groq.assert_awaited_once()

    async def test_rate_limit_does_not_retry_gemini_offline(self):
        generate = AsyncMock(side_effect=Exception("429 RESOURCE_EXHAUSTED retry in 12s"))
        fake_client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
        groq = AsyncMock(return_value=AIResult("fallback", "Groq", "test"))

        with (
            patch.object(ai_client, "model_available", True),
            patch.object(ai_client, "client", fake_client),
            patch.object(ai_client, "_ask_groq", groq),
        ):
            result = await ai_client.ask_ai("hello", use_search=True)

        self.assertIn("fallback", result)
        self.assertEqual(generate.await_count, 1)
        self.assertGreater(ai_client.gemini_cooldown_until, time.time())

    async def test_existing_cooldown_skips_gemini(self):
        generate = AsyncMock(side_effect=AssertionError("Gemini should not be called"))
        fake_client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
        groq = AsyncMock(return_value=AIResult("fallback", "Groq", "test"))
        ai_client.gemini_cooldown_until = time.time() + 30

        with (
            patch.object(ai_client, "model_available", True),
            patch.object(ai_client, "client", fake_client),
            patch.object(ai_client, "_ask_groq", groq),
        ):
            result = await ai_client.ask_ai("hello", use_search=True)

        self.assertIn("fallback", result)
        generate.assert_not_awaited()

    async def test_scheduled_call_can_raise_when_all_providers_fail(self):
        unavailable = AsyncMock(side_effect=RuntimeError("unavailable"))
        with (
            patch.object(ai_client, "model_available", False),
            patch.object(ai_client, "client", None),
            patch.object(ai_client, "_ask_groq", unavailable),
            patch.object(ai_client, "_ask_zhipu", unavailable),
            patch.object(ai_client, "_ask_openrouter", unavailable),
        ):
            with self.assertRaises(ai_client.AIServiceUnavailable):
                await ai_client.ask_ai("hello", raise_on_failure=True)


class DigestDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_retry_still_sends_only_once(self):
        cog = object.__new__(AIDaily)
        cog._delivery_lock = asyncio.Lock()
        embed = create_ai_embed("日报", "- item")
        cog._build_daily_embed = AsyncMock(side_effect=[RuntimeError("temporary"), embed])
        channel = SimpleNamespace(send=AsyncMock())

        async def immediate_retry(_task_name, build, **_kwargs):
            try:
                return await build()
            except RuntimeError:
                return await build()

        with patch("core.jobs.retry_async", side_effect=immediate_retry):
            await cog._run_daily(channel)

        self.assertEqual(cog._build_daily_embed.await_count, 2)
        channel.send.assert_awaited_once_with(embed=embed)

    async def test_overlapping_daily_trigger_is_skipped(self):
        cog = object.__new__(AIDaily)
        cog._delivery_lock = asyncio.Lock()
        embed = create_ai_embed("日报", "- item")
        started = asyncio.Event()
        release = asyncio.Event()

        async def build():
            started.set()
            await release.wait()
            return embed

        cog._build_daily_embed = AsyncMock(side_effect=build)
        channel = SimpleNamespace(send=AsyncMock())

        async def no_wait_retry(_task_name, builder, **_kwargs):
            return await builder()

        with patch("core.jobs.retry_async", side_effect=no_wait_retry):
            first_run = asyncio.create_task(cog._run_daily(channel))
            await started.wait()
            await cog._run_daily(channel)
            release.set()
            await first_run

        cog._build_daily_embed.assert_awaited_once()
        channel.send.assert_awaited_once_with(embed=embed)

    async def test_cache_failure_after_send_does_not_retry_delivery(self):
        cog = object.__new__(AdvancedNews)
        cog._digest_delivery_lock = asyncio.Lock()
        embed = create_ai_embed("精读", "- item")
        cog._build_scheduled_digest = AsyncMock(return_value=(embed, ["https://example.com"]))
        channel = SimpleNamespace(send=AsyncMock())

        async def no_wait_retry(_task_name, build, **_kwargs):
            return await build()

        with (
            patch("core.jobs.retry_async", side_effect=no_wait_retry),
            patch("cogs.advanced_news.news_cache.mark_as_pushed", side_effect=RuntimeError("disk error")),
        ):
            with self.assertRaisesRegex(RuntimeError, "disk error"):
                await cog._run_scheduled_digest(channel, "测试")

        cog._build_scheduled_digest.assert_awaited_once()
        channel.send.assert_awaited_once_with(embed=embed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
