import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cogs import advanced_news
from cogs.advanced_news import (
    AdvancedNews, MAX_CANDIDATES, _prepare_candidates,
    _normalize_recommendations, _render_recommendations,
)
from core import news_cache
from core.ai_providers import AIResult
from core.storage import JsonStore


def source(index=1, **extra):
    return {"title": f"Original story {index}", "url": f"https://example.com/{index}",
            "content": "<p>A concrete observation, with a comparison.</p>",
            "source": "Science", "publisher": "Test publisher", "published_at": time.time(), **extra}


def selection(*ids):
    return json.dumps({"items": [
        {"id": identity, "title": "一个陌生领域的观察", "summary": "原文比较了不同情境中的结果。",
         "why_read": "可以想一想：环境变化是否会改变原有判断？"} for identity in ids
    ]}, ensure_ascii=False)


class ExplorationTests(unittest.IsolatedAsyncioTestCase):
    def test_raw_evidence_is_required_and_dates_are_preserved(self):
        legacy = {"title": "Old model summary", "url": "https://example.com/old", "summary": "invented"}
        items = [legacy, source(1), source(2, published_at=time.time() - 4 * 86400),
                 source(3, published_at=float("nan")), source(4, url="https://user:password@example.com")]
        result = _prepare_candidates(items)
        self.assertEqual(len(result), 1)
        self.assertNotIn("<p>", result[0]["content"])
        self.assertIsNotNone(result[0]["published_at"])
        unknown_date = _prepare_candidates([source(5, published_at=None)])[0]
        self.assertIsNone(unknown_date["published_at"])

    def test_candidate_budget_interleaves_publishers(self):
        items = [source(i, publisher="Many items") for i in range(70)]
        items.append(source(99, publisher="Another field"))
        result = _prepare_candidates(items)
        self.assertEqual(len(result), MAX_CANDIDATES)
        self.assertEqual(result[1]["publisher"], "Another field")

    def test_output_cannot_invent_ids_links_or_overflow(self):
        candidates = _prepare_candidates([source()])
        for text in [selection("R99"), selection("R01", "R01"), '{"items": "bad"}']:
            with self.assertRaises(ValueError):
                _normalize_recommendations(text, candidates)
        payload = json.loads(selection("R01"))
        for value in ["https://invented.example", "长" * 161, "<b>伪造</b>", 1]:
            payload["items"][0]["summary"] = value
            with self.assertRaises(ValueError):
                _normalize_recommendations(json.dumps(payload), candidates)
        self.assertEqual(_normalize_recommendations('{"items": []}', candidates), [])

    def test_rendering_preserves_original_link_and_distinguishes_reading_reason(self):
        candidates = _prepare_candidates([source(url="https://example.com/a(b)")])
        selected = _normalize_recommendations(selection("R01"), candidates)
        body = _render_recommendations(selected)
        self.assertIn("https://example.com/a%28b%29", body)
        self.assertIn("RSS 摘要", body)
        self.assertIn("值得一读", body)
        self.assertNotIn("Original story", body)
        self.assertLess(len(body), 3800)

    async def test_hourly_collection_never_calls_model(self):
        cog = object.__new__(AdvancedNews)
        cog._fetch_lock = asyncio.Lock()
        generate = AsyncMock(side_effect=AssertionError("No model during collection"))
        with tempfile.TemporaryDirectory() as directory, patch.object(
            news_cache, "_cache_store", JsonStore(Path(directory) / "cache.json", list)
        ), patch.object(advanced_news.data_ingester, "fetch_all_sources", AsyncMock(return_value=[source()])), patch.object(
            advanced_news.ai_client, "generate_ai", generate
        ):
            await cog._process_hourly_fetch()
            self.assertEqual(len(news_cache.load_cache()), 1)
            self.assertIn("content", news_cache.load_cache()[0])
            generate.assert_not_awaited()

    async def test_only_rendered_items_marked_after_successful_delivery(self):
        cog = object.__new__(AdvancedNews)
        cog._digest_delivery_lock = asyncio.Lock()
        generate = AsyncMock(return_value=AIResult(selection("R02"), "Test", "model"))
        channel = AsyncMock()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            news_cache, "_cache_store", JsonStore(Path(directory) / "cache.json", list)
        ), patch.object(advanced_news.ai_client, "generate_ai", generate):
            news_cache.add_items([source(1), source(2)])
            channel.send.side_effect = RuntimeError("send failed")
            with self.assertRaises(RuntimeError):
                await cog._run_scheduled_digest(channel, "测试")
            self.assertTrue(all(not item.get("pushed") for item in news_cache.load_cache()))
            channel.send.side_effect = None
            channel.send.reset_mock()
            await cog._run_scheduled_digest(channel, "测试")
            channel.send.assert_awaited_once()
            embed = channel.send.await_args.kwargs["embed"]
            pushed = [item for item in news_cache.load_cache() if item.get("pushed")]
            self.assertEqual(len(pushed), 1)
            self.assertIn(pushed[0]["url"], embed.description)
            self.assertEqual(embed.description.count("]("), 1)
            self.assertIn("视野拾遗", embed.title)
            self.assertEqual(embed.footer.text, "✨ Powered by Test (model)")
            model_input = json.loads(generate.await_args.args[0])
            self.assertTrue(all("url" not in item for item in model_input["candidates"]))
            self.assertIn("content", model_input["candidates"][0])
            self.assertNotIn("summary", model_input["candidates"][0])

    async def test_empty_selection_sends_nothing(self):
        cog = object.__new__(AdvancedNews)
        cog._digest_delivery_lock = asyncio.Lock()
        channel = AsyncMock()
        with patch.object(news_cache, "get_unpushed_items", return_value=[source()]), patch.object(
            news_cache, "load_cache", return_value=[]
        ), patch.object(advanced_news.ai_client, "generate_ai", AsyncMock(return_value=AIResult('{"items": []}', "Test", "model"))):
            await cog._run_scheduled_digest(channel, "测试")
        channel.send.assert_not_awaited()
