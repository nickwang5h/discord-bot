"""Outer model timeouts leave the free chain room to finish after Claude times out.

A personal route tries Claude first and, on any failure, the whole free chain in the
same call. These tests run that chain on a virtual clock: every stage uses its full
budget (Claude hangs until its own timeout, Groq/Zhipu/OpenRouter time out, Gemini
answers at the end of its budget), and the caller's outer cap must not cut it.
"""
import asyncio
import json
import selectors
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import ai_client, claude_budget
from core.ai_providers import AIResult, ProviderError
from core.feeds import FeedItem
from core.knowledge import recall as recall_module
from core.knowledge.recall import ANSWER_ROUTE, PLAN_ROUTE
from core.knowledge.store import DAY, KnowledgeDoc, KnowledgeStore
from core.news.ingest import normalize
from core.news.models import Subscription
from core.news import pipeline as pipeline_module
from core.news.pipeline import NewsPipeline
from core.news.store import NewsStore



class _VirtualSelector:
    """Returns ready I/O at once; otherwise jumps the clock to the next timer instead of sleeping."""

    def __init__(self):
        self._inner = selectors.DefaultSelector()
        self.now = 0.0

    def select(self, timeout=None):
        events = self._inner.select(0)
        if events or timeout == 0:
            return events
        if timeout is None:
            return self._inner.select(None)
        self.now += timeout
        return []

    def __getattr__(self, name):
        return getattr(self._inner, name)


class VirtualClockLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        self._virtual = _VirtualSelector()
        super().__init__(self._virtual)

    def time(self):
        return self._virtual.now


async def _inline_to_thread(func, /, *args, **kwargs):
    # Threads finish in real time, which a virtual clock cannot wait for; run them inline.
    return func(*args, **kwargs)


class SlowChain:
    """Every stage of a personal route uses its whole budget; Gemini answers last."""

    def __init__(self, answer):
        self.answer = answer
        self.stages = []

    async def _claude_create(self, **request):
        self.stages.append(("Claude", asyncio.get_running_loop().time()))
        await asyncio.Event().wait()  # never answers: Claude's own timeout ends it

    def _times_out(self, name, seconds):
        async def provider(text, system, **kwargs):
            self.stages.append((name, asyncio.get_running_loop().time()))
            await asyncio.sleep(seconds)
            raise ProviderError(f"{name} 总超时 ({seconds}s)")
        return provider

    async def _gemini(self, text, system, **kwargs):
        self.stages.append(("Gemini", asyncio.get_running_loop().time()))
        await asyncio.sleep(ai_client.GEMINI_TIMEOUT_SECONDS)
        return AIResult(self.answer(text), "Gemini", "gemini-test")

    def patches(self, usage_file):
        fake_claude = SimpleNamespace(messages=SimpleNamespace(create=self._claude_create))
        return [
            patch.object(claude_budget, "USAGE_FILE", usage_file),
            patch.object(ai_client, "claude_registered", True),
            patch.object(ai_client, "_claude_client", fake_claude),
            patch.object(ai_client, "_ask_groq", self._times_out("Groq", ai_client.GROQ_TIMEOUT_SECONDS)),
            patch.object(ai_client, "_ask_zhipu", self._times_out("Zhipu", ai_client.ZHIPU_TIMEOUT_SECONDS)),
            patch.object(ai_client, "_ask_openrouter",
                         self._times_out("OpenRouter", ai_client.OPENROUTER_TIMEOUT_SECONDS)),
            patch.object(ai_client, "_ask_gemini", self._gemini),
            patch.object(ai_client, "model_available", True),
            patch.object(ai_client, "client", object()),
            patch.object(ai_client, "gemini_cooldown_until", 0.0),
            patch("asyncio.to_thread", _inline_to_thread),
            patch.object(ai_client.logger, "disabled", True),
        ]


def worst_personal_chain_seconds():
    return (ai_client.CLAUDE_TIMEOUT_SECONDS + ai_client.GROQ_TIMEOUT_SECONDS + ai_client.ZHIPU_TIMEOUT_SECONDS
            + ai_client.OPENROUTER_TIMEOUT_SECONDS + ai_client.GEMINI_TIMEOUT_SECONDS)


class TimeoutTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_virtual(self, chain, make_coro):
        for target in chain.patches(Path(self.tmp.name) / "data" / "claude_usage.json"):
            target.start()
            self.addCleanup(target.stop)
        with asyncio.Runner(loop_factory=VirtualClockLoop) as runner:
            result = runner.run(make_coro())
            return result, runner.get_loop().time()

    def assert_whole_chain_ran(self, chain, finished_at):
        self.assertEqual([name for name, _ in chain.stages], ["Claude", "Groq", "Zhipu", "OpenRouter", "Gemini"])
        # Claude was cut by its own timeout, not by the caller's.
        self.assertEqual(chain.stages[1][1], ai_client.CLAUDE_TIMEOUT_SECONDS)
        self.assertGreaterEqual(finished_at, worst_personal_chain_seconds())


def _following_answer(text):
    ids = [c["id"] for c in json.loads(text)["candidates"]][:2]
    items = [{"id": i, "title": f'中文标题{"甲乙"[n]}', "summary": "原文提供了具体观察。",
              "why_read": "可以想一想：条件是否改变结论？", "tags": ["渥太华"]} for n, i in enumerate(ids)]
    return json.dumps({"items": items}, ensure_ascii=False)


def _article(index):
    return normalize(FeedItem("World", "BBC World", f"Original story {index}", f"https://example.com/{index}",
                              f"A concrete observation {index}, with a comparison.", time.time()), time.time())


class NewsPersonalTopicTimeoutTests(TimeoutTestCase):
    def setUp(self):
        super().setUp()
        self.store = NewsStore(Path(self.tmp.name) / "news.sqlite3")
        self.addCleanup(self.store.close)
        self.store.import_history([])
        self.store.upsert_articles([_article(1), _article(2)])

    def preview(self, identity):
        pipeline = NewsPipeline(self.store)

        async def collect(*args, **kwargs):
            return None

        pipeline.ingester.collect = collect
        sub = Subscription(identity, identity, ("general", "discovery"), ("08:00",), 123)
        return pipeline.preview(sub, "2026-08-01/08:00")

    def test_following_free_chain_finishes_after_claude_times_out(self):
        chain = SlowChain(_following_answer)
        edition, finished_at = self.run_virtual(chain, lambda: self.preview("following"))
        self.assert_whole_chain_ran(chain, finished_at)
        self.assertIsNotNone(edition)
        self.assertEqual([item["title"] for item in edition.selected], ["中文标题甲", "中文标题乙"])

    def test_shared_topic_keeps_its_180_second_cap(self):
        async def hang(*args, **kwargs):
            await asyncio.Event().wait()

        async def cut_at():
            with self.assertRaises(TimeoutError):
                await self.preview("general")
            return None

        chain = SlowChain(_following_answer)
        with patch("core.news.pipeline.ai_client.generate_ai", hang):
            _, finished_at = self.run_virtual(chain, cut_at)
        self.assertEqual(finished_at, pipeline_module.SHARED_MODEL_TIMEOUT_SECONDS)
        self.assertEqual(pipeline_module.SHARED_MODEL_TIMEOUT_SECONDS, 180)
        self.assertGreaterEqual(pipeline_module.SHARED_MODEL_TIMEOUT_SECONDS, ai_client.FREE_CHAIN_TIMEOUT_SECONDS)


class RecallAnswerTimeoutTests(TimeoutTestCase):
    def setUp(self):
        super().setUp()
        recall_module.logger.disabled = True
        self.addCleanup(setattr, recall_module.logger, "disabled", False)
        self.store = KnowledgeStore(Path(self.tmp.name) / "knowledge.sqlite3")
        self.addCleanup(self.store.close)
        if not self.store.fts_available:
            self.skipTest("SQLite FTS5 unavailable")
        now = time.time()
        url = "https://news.example.com/reuters/1"
        self.store.upsert_docs([KnowledgeDoc(
            id="news:1", kind="news", title="Ontario expands battery storage procurement",
            body="The IESO said Ontario will procure more battery storage capacity.", source="Reuters",
            url=url, canonical_url=url, published_at=now - DAY, added_at=now - DAY)])

    def test_answer_free_chain_finishes_after_claude_times_out(self):
        async def generate(text, **kwargs):
            if kwargs["route"] == PLAN_ROUTE:
                plan = {"zh": [], "en": ["battery storage"], "fallback_terms": []}
                return AIResult(json.dumps(plan), "Claude", "claude-opus-5-5")
            self.assertEqual(kwargs["route"], ANSWER_ROUTE)
            return await ai_client.generate_ai(text, **kwargs)

        chain = SlowChain(lambda text: "安大略在扩大电池储能采购 [S1]。")
        result, finished_at = self.run_virtual(
            chain, lambda: recall_module.recall("安大略储能", self.store, generate=generate))
        self.assert_whole_chain_ran(chain, finished_at)
        self.assertEqual((result.status, result.provider), ("ok", "Gemini"))


class DerivedCapTests(unittest.TestCase):
    def test_caps_follow_from_the_stage_budgets(self):
        self.assertEqual(ai_client.FREE_CHAIN_TIMEOUT_SECONDS, 140)
        self.assertEqual(ai_client.PERSONAL_CHAIN_TIMEOUT_SECONDS, worst_personal_chain_seconds())
        self.assertEqual(pipeline_module.PERSONAL_MODEL_TIMEOUT_SECONDS, 240)
        self.assertEqual(recall_module.ANSWER_TIMEOUT_SECONDS, 240)
        self.assertEqual(recall_module.PLAN_TIMEOUT_SECONDS, 45)
        # Plan (45 s) and answer together stay far inside a deferred interaction's 15 minutes.
        self.assertLess(recall_module.PLAN_TIMEOUT_SECONDS + recall_module.ANSWER_TIMEOUT_SECONDS, 15 * 60)


if __name__ == "__main__":
    unittest.main()
