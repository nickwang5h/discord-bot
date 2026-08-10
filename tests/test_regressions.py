import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.advanced_news import (
    ANALYSIS_BATCH_SIZE,
    ANALYSIS_MAX_OUTPUT_TOKENS,
    AdvancedNews,
    _normalize_scored_items,
)
from cogs.ai_daily import AIDaily
from cogs.ask import (
    ASK_MODE_GEMINI_SEARCH,
    ASK_MODE_QWEN,
    ASK_MODE_QWEN_SEARCH,
    Ask,
    _answer_question,
    _plan_search_queries,
)
from cogs.link_summary import fetch_and_summarize
from core import ai_client
from core.ai_providers import AIResult
from core.bilibili_transcript import (
    BilibiliTranscript,
    BilibiliTranscriptError,
    _fetch_transcript,
    _normalize_segments,
    _resolve_short_url,
    is_bilibili_url,
)
from core.web_search import SearchSource
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

    def test_qwen_requests_disable_reasoning_output(self):
        qwen = next(spec for spec in ai_client.GROQ_MODELS if spec.model_id == "qwen/qwen3.6-27b")

        self.assertEqual(qwen.reasoning_effort, "none")
        self.assertEqual(qwen.reasoning_format, "hidden")

    def test_model_catalog_balances_quality_cost_and_retirement_risk(self):
        self.assertEqual(
            [spec.model_id for spec in ai_client.GROQ_MODELS],
            [
                "qwen/qwen3.6-27b",
                "openai/gpt-oss-120b",
                "openai/gpt-oss-20b",
            ],
        )
        self.assertEqual(
            [spec.reasoning_effort for spec in ai_client.GROQ_MODELS],
            ["none", "low", "low"],
        )
        self.assertEqual(
            [spec.model_id for spec in ai_client.ZHIPU_MODELS],
            ["glm-4.7-flash", "glm-4.5-flash"],
        )
        self.assertEqual(
            [spec.model_id for spec in ai_client.OPENROUTER_MODELS],
            [
                "nvidia/nemotron-3-super-120b-a12b:free",
                "nvidia/nemotron-3-ultra-550b-a55b:free",
                "openai/gpt-oss-20b:free",
                "nvidia/nemotron-nano-9b-v2:free",
            ],
        )
        self.assertFalse(any(spec.model_id.startswith("google/") for spec in ai_client.OPENROUTER_MODELS))
        self.assertEqual(ai_client.DEFAULT_GEMINI_MODEL, "gemini-3.6-flash")

    async def test_basic_generation_prefers_qwen_provider_over_gemini(self):
        gemini = AsyncMock(side_effect=AssertionError("Gemini should not be called"))
        groq = AsyncMock(return_value=AIResult("basic", "Groq", "qwen/qwen3.6-27b"))
        with (
            patch.object(ai_client, "model_available", True),
            patch.object(ai_client, "client", object()),
            patch.object(ai_client, "_ask_gemini", gemini),
            patch.object(ai_client, "_ask_groq", groq),
        ):
            result = await ai_client.generate_ai("hello")

        self.assertEqual(result.provider, "Groq")
        self.assertEqual(result.model, "qwen/qwen3.6-27b")
        groq.assert_awaited_once()
        gemini.assert_not_awaited()

    async def test_basic_generation_uses_gemini_only_after_other_providers_fail(self):
        unavailable = AsyncMock(side_effect=RuntimeError("unavailable"))
        gemini = AsyncMock(return_value=AIResult("last resort", "Gemini", "test"))

        with (
            patch.object(ai_client, "model_available", True),
            patch.object(ai_client, "client", object()),
            patch.object(ai_client, "_ask_groq", unavailable),
            patch.object(ai_client, "_ask_zhipu", unavailable),
            patch.object(ai_client, "_ask_openrouter", unavailable),
            patch.object(ai_client, "_ask_gemini", gemini),
        ):
            result = await ai_client.generate_ai("hello")

        self.assertEqual(result.provider, "Gemini")
        self.assertEqual(unavailable.await_count, 3)
        gemini.assert_awaited_once_with(
            "hello",
            "用简洁中文总结要点，分条列出。",
            with_search=False,
            json_mode=False,
            max_output_tokens=4096,
        )

    async def test_search_generation_still_prefers_gemini(self):
        gemini = AsyncMock(return_value=AIResult("current", "Gemini", "test"))
        groq = AsyncMock(side_effect=AssertionError("Groq should not be called"))

        with (
            patch.object(ai_client, "model_available", True),
            patch.object(ai_client, "client", object()),
            patch.object(ai_client, "_ask_gemini", gemini),
            patch.object(ai_client, "_ask_groq", groq),
        ):
            result = await ai_client.generate_ai("latest", use_search=True)

        self.assertEqual(result.provider, "Gemini")
        gemini.assert_awaited_once_with(
            "latest",
            "用简洁中文总结要点，分条列出。",
            with_search=True,
            json_mode=False,
            max_output_tokens=4096,
        )
        groq.assert_not_awaited()

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


class AskSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_planner_keeps_original_and_adds_english_query(self):
        generate = AsyncMock(
            return_value=AIResult(
                '{"english_query":"2026 Fields Medal winners and award date"}',
                "Groq",
                "qwen/qwen3.6-27b",
            )
        )

        with patch("cogs.ask.ai_client.generate_ai", generate):
            queries = await _plan_search_queries("今年菲尔兹奖")

        self.assertEqual(
            queries,
            ["今年菲尔兹奖", "2026 Fields Medal winners and award date"],
        )
        self.assertTrue(generate.await_args.kwargs["json_mode"])
        self.assertFalse(generate.await_args.kwargs["use_search"])
        self.assertLessEqual(generate.await_args.kwargs["max_output_tokens"], 200)

    async def test_query_planner_failure_falls_back_to_unchanged_question(self):
        with patch(
            "cogs.ask.ai_client.generate_ai",
            AsyncMock(side_effect=ai_client.AIServiceUnavailable("offline")),
        ):
            queries = await _plan_search_queries("今年菲尔兹奖")

        self.assertEqual(queries, ["今年菲尔兹奖"])

    def test_ask_command_exposes_three_answer_mode_choices(self):
        mode_parameter = next(parameter for parameter in Ask.ask.parameters if parameter.name == "mode")

        self.assertFalse(mode_parameter.required)
        self.assertEqual(
            [choice.value for choice in mode_parameter.choices],
            [ASK_MODE_QWEN, ASK_MODE_QWEN_SEARCH, ASK_MODE_GEMINI_SEARCH],
        )

    async def test_search_results_are_grounded_by_qwen_without_gemini_search(self):
        sources = [
            SearchSource(
                title="Latest update",
                url="https://news.google.com/rss/articles/one",
                snippet="Current facts",
                kind="Google News",
            )
        ]
        generate = AsyncMock(return_value=AIResult("基于材料的回答 [S1]", "Groq", "qwen/qwen3.6-27b"))
        legacy_search = AsyncMock(side_effect=AssertionError("Gemini Search should not be called"))

        with (
            patch.dict(
                _answer_question.__globals__,
                {
                    "_plan_search_queries": AsyncMock(
                        return_value=["今天有什么新闻？", "latest news today"]
                    )
                },
            ),
            patch("cogs.ask.web_search.search_web", AsyncMock(return_value=sources)),
            patch("cogs.ask.ai_client.generate_ai", generate),
            patch("cogs.ask.ai_client.ask_ai", legacy_search),
        ):
            answer = await _answer_question("今天有什么新闻？", mode=ASK_MODE_QWEN_SEARCH)

        self.assertIn("基于材料的回答 [S1]", answer)
        self.assertIn("https://news.google.com/rss/articles/one", answer)
        self.assertIn("<!--MODEL:Groq (qwen/qwen3.6-27b)-->", answer)
        self.assertFalse(generate.await_args.kwargs["use_search"])
        legacy_search.assert_not_awaited()

    async def test_empty_qwen_web_results_do_not_spend_gemini_search_quota(self):
        ask = AsyncMock(side_effect=AssertionError("Gemini Search should not be called"))

        with (
            patch.dict(
                _answer_question.__globals__,
                {
                    "_plan_search_queries": AsyncMock(
                        return_value=["最新消息", "latest updates"]
                    )
                },
            ),
            patch("cogs.ask.web_search.search_web", AsyncMock(return_value=[])),
            patch("cogs.ask.ai_client.ask_ai", ask),
        ):
            answer = await _answer_question("最新消息", mode=ASK_MODE_QWEN_SEARCH)

        self.assertIn("网页检索暂不可用", answer)
        ask.assert_not_awaited()

    async def test_gemini_search_mode_bypasses_qwen_web_fetch_and_is_strict(self):
        ask = AsyncMock(return_value="strict Gemini result")
        fetch = AsyncMock(side_effect=AssertionError("Qwen web search should not run"))

        with (
            patch("cogs.ask.web_search.search_web", fetch),
            patch("cogs.ask.ai_client.ask_ai", ask),
        ):
            answer = await _answer_question("最新消息", mode=ASK_MODE_GEMINI_SEARCH)

        self.assertEqual(answer, "strict Gemini result")
        self.assertTrue(ask.await_args.kwargs["use_search"])
        self.assertFalse(ask.await_args.kwargs["fallback_offline"])
        self.assertEqual(ask.await_args.kwargs["max_output_tokens"], 1600)
        fetch.assert_not_awaited()

    async def test_search_disabled_keeps_basic_qwen_routing(self):
        ask = AsyncMock(return_value="basic result")
        fetch = AsyncMock(side_effect=AssertionError("web search should not run"))

        with (
            patch("cogs.ask.web_search.search_web", fetch),
            patch("cogs.ask.ai_client.ask_ai", ask),
        ):
            answer = await _answer_question("解释量子纠缠", mode=ASK_MODE_QWEN)

        self.assertEqual(answer, "basic result")
        self.assertFalse(ask.await_args.kwargs["use_search"])
        fetch.assert_not_awaited()


class BilibiliSummaryTests(unittest.IsolatedAsyncioTestCase):
    def test_bilibili_url_allowlist_and_segment_normalization(self):
        self.assertTrue(is_bilibili_url("https://www.bilibili.com/video/BV1234567890"))
        self.assertTrue(is_bilibili_url("https://b23.tv/example"))
        self.assertFalse(is_bilibili_url("https://www.bilibili.com.evil.test/video/BV1234567890"))
        self.assertFalse(is_bilibili_url("http://www.bilibili.com/video/BV1234567890"))

        segments = _normalize_segments(
            {
                "body": [
                    {"from": 0, "to": 1.5, "content": "第一段"},
                    {"from": 1, "to": 2.5, "content": "第二段"},
                ]
            }
        )
        self.assertEqual(segments[1]["start_ms"], 1500)
        self.assertEqual([item["text"] for item in segments], ["第一段", "第二段"])

    async def test_short_link_redirect_is_revalidated(self):
        class FakeResponse:
            def __init__(self, location):
                self.status = 302
                self.headers = {"Location": location}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        class FakeSession:
            def __init__(self, location):
                self.location = location

            def get(self, *_args, **_kwargs):
                return FakeResponse(self.location)

        canonical = await _resolve_short_url(
            FakeSession("https://www.bilibili.com/video/BV1234567890"),
            "https://b23.tv/example",
        )
        self.assertIn("BV1234567890", canonical)

        with self.assertRaisesRegex(BilibiliTranscriptError, "不允许"):
            await _resolve_short_url(
                FakeSession("https://evil.test/video/BV1234567890"),
                "https://b23.tv/example",
            )

    async def test_fixed_three_request_caption_flow(self):
        responses = [
            {
                "code": 0,
                "data": {
                    "aid": 42,
                    "title": "测试视频",
                    "pages": [
                        {"page": 1, "cid": 98, "duration": 60},
                        {"page": 2, "cid": 99, "duration": 120, "part": "第二部分"},
                    ],
                },
            },
            {
                "code": 0,
                "data": {
                    "subtitle": {
                        "subtitles": [
                            {
                                "lan": "ai-zh",
                                "subtitle_url": "//aisubtitle.hdslb.com/example.json",
                            }
                        ]
                    }
                },
            },
            {"body": [{"from": 0, "to": 2, "content": "字幕正文"}]},
        ]
        fetch = AsyncMock(side_effect=responses)

        result = await _fetch_transcript(
            object(),
            "https://www.bilibili.com/video/BV1234567890?p=2",
            cookie="secret-cookie",
            fetch_json=fetch,
        )

        self.assertEqual(fetch.await_count, 3)
        self.assertEqual(result.video_id, "BV1234567890")
        self.assertEqual(result.text, "字幕正文")
        self.assertEqual(result.title, "第二部分")
        self.assertIn("cid=99", fetch.await_args_list[1].args[1])
        self.assertIn("Cookie", fetch.await_args_list[0].kwargs["headers"])
        self.assertNotIn("Cookie", fetch.await_args_list[2].kwargs["headers"])

    async def test_subtitle_redirect_host_fails_closed(self):
        fetch = AsyncMock(
            side_effect=[
                {
                    "code": 0,
                    "data": {
                        "aid": 42,
                        "pages": [{"page": 1, "cid": 99, "duration": 120}],
                    },
                },
                {
                    "code": 0,
                    "data": {
                        "subtitle": {
                            "subtitles": [
                                {"lan": "ai-zh", "subtitle_url": "https://evil.test/subtitle.json"}
                            ]
                        }
                    },
                },
            ]
        )

        with self.assertRaisesRegex(BilibiliTranscriptError, "允许的域名"):
            await _fetch_transcript(
                object(),
                "https://www.bilibili.com/video/BV1234567890",
                cookie=None,
                fetch_json=fetch,
            )

    async def test_link_summary_routes_bilibili_to_transcript_adapter(self):
        transcript = BilibiliTranscript(
            video_id="BV1234567890",
            title="测试视频",
            language="ai-zh",
            source="自动字幕",
            text="这是一段足够长的测试字幕。" * 10,
            segment_count=10,
        )
        summarize = AsyncMock(return_value="- 摘要")

        with (
            patch.dict(
                fetch_and_summarize.__globals__,
                {
                    "fetch_bilibili_transcript": AsyncMock(return_value=transcript),
                    "fetch_public_html": AsyncMock(side_effect=AssertionError("not HTML")),
                },
            ),
            patch.object(fetch_and_summarize.__globals__["settings"], "get_secret", return_value=None),
            patch.object(fetch_and_summarize.__globals__["ai_client"], "ask_ai", summarize),
        ):
            success, embed = await fetch_and_summarize(
                "https://www.bilibili.com/video/BV1234567890"
            )

        self.assertTrue(success)
        self.assertIn("B站视频", embed.title)
        self.assertIn("不可信", summarize.await_args.kwargs["system"])


class AdvancedNewsAnalysisTests(unittest.IsolatedAsyncioTestCase):
    def test_model_output_is_validated_and_source_fields_are_restored(self):
        source = {
            "title": "Original title",
            "url": "https://example.com/story",
            "source": "Tech",
        }
        candidate = {
            "title": "Invented title",
            "url": source["url"],
            "summary": "Useful summary",
            "topic": "AI",
            "connection_reason": "Connects AI and developer tooling",
            "relevance_score": 1.5,
            "novelty_score": -0.2,
            "quality_score": 0.9,
            "llm_interestingness": 0.8,
            "cross_domain_bridge": 0.7,
        }

        result = _normalize_scored_items([candidate], [source])

        self.assertEqual(result[0]["title"], "Original title")
        self.assertEqual(result[0]["source"], "Tech")
        self.assertEqual(result[0]["relevance_score"], 1.0)
        self.assertEqual(result[0]["novelty_score"], 0.0)
        self.assertLessEqual(result[0]["discovery_score"], 1.0)

    async def test_provider_outage_stops_remaining_analysis_batches(self):
        cog = object.__new__(AdvancedNews)
        cog._fetch_lock = asyncio.Lock()
        items = [
            {
                "title": f"Story {index}",
                "url": f"https://example.com/{index}",
                "source": "Tech",
                "content": "content",
            }
            for index in range(ANALYSIS_BATCH_SIZE + 1)
        ]
        generate = AsyncMock(side_effect=ai_client.AIServiceUnavailable("offline"))

        with (
            patch("cogs.advanced_news.data_ingester.fetch_all_sources", AsyncMock(return_value=items)),
            patch("cogs.advanced_news.news_cache.filter_new_items", return_value=items),
            patch("cogs.advanced_news.ai_client.generate_ai", generate),
        ):
            await cog._process_hourly_fetch()

        generate.assert_awaited_once()
        self.assertEqual(generate.await_args.kwargs["max_output_tokens"], ANALYSIS_MAX_OUTPUT_TOKENS)

    async def test_interval_loop_skips_immediate_startup_fetch(self):
        cog = object.__new__(AdvancedNews)
        cog._skip_initial_hourly_fetch = True
        cog._process_hourly_fetch = AsyncMock()

        await AdvancedNews.hourly_fetch.coro(cog)
        cog._process_hourly_fetch.assert_not_awaited()

        await AdvancedNews.hourly_fetch.coro(cog)
        cog._process_hourly_fetch.assert_awaited_once()


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
