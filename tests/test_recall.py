import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import ai_client
from core.ai_providers import AIResult
from core.knowledge import recall as recall_module
from core.knowledge.recall import (
    ANSWER_ROUTE, ANSWER_SYSTEM, PLAN_ROUTE, PLAN_SCHEMA, RecallError, cited_numbers, fallback_terms,
    parse_plan, recall,
)
from core.knowledge.store import DAY, KnowledgeDoc, KnowledgeStore, KnowledgeUnavailable

NOW = 1_800_000_000.0
DAY_KEY = "2027-01-15"  # UTC date of NOW


def news(n, title, body="", *, source="Reuters", age_days=1, url=None):
    url = url or f"https://news.example.com/{source.lower()}/{n}"
    return KnowledgeDoc(id=f"news:{n}", kind="news", title=title, body=body, source=source, url=url,
                        canonical_url=url, published_at=NOW - age_days * DAY, added_at=NOW - age_days * DAY)


def inbox(n, title, body="", *, url=""):
    return KnowledgeDoc(id=f"inbox:{n}", kind="inbox", title=title, body=body, source="inbox", url=url,
                        canonical_url=url, added_at=NOW - DAY, state="pending")


SEED = [
    news(1, "Ontario expands battery storage procurement",
         "The IESO said Ontario will procure more battery storage capacity to support the grid."),
    news(2, "US tariffs on Canadian steel rise again",
         "Washington raised tariffs on Canadian steel and aluminum, Ottawa promised a response.",
         source="AP"),
    news(3, "Sudbury mine reopens", "A nickel mine near Sudbury reopened after two years.", source="CBC"),
    inbox(1, "储能电站笔记", "安大略省的储能电站项目进展：IESO 第二轮采购，电池储能为主。",
          url="https://blog.example.org/storage"),
    inbox(2, "关税读书笔记", "美国对加拿大钢铝加征关税，渥太华准备反制。"),
]


class FakeGenerate:
    """Records calls; replies per route with text, an exception, or a callable."""

    def __init__(self, plan=None, answer="根据材料 [S1]。", provider="Claude"):
        self.replies = {PLAN_ROUTE: plan, ANSWER_ROUTE: answer}
        self.provider = provider
        self.calls = []

    async def __call__(self, text, **kwargs):
        self.calls.append({"text": text, **kwargs})
        reply = self.replies[kwargs["route"]]
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            reply = reply(text)
        if isinstance(reply, dict):
            reply = json.dumps(reply, ensure_ascii=False)
        return AIResult(reply, self.provider, "claude-opus-5-5" if self.provider == "Claude" else "qwen")

    def routes(self):
        return [call["route"] for call in self.calls]


PLAN_STORAGE = {"zh": ["储能"], "en": ["battery storage"], "fallback_terms": ["grid"]}


class RecallTestCase(unittest.TestCase):
    def setUp(self):
        recall_module.logger.disabled = True
        self.addCleanup(setattr, recall_module.logger, "disabled", False)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = KnowledgeStore(Path(self.tmp.name) / "knowledge.sqlite3")
        if not self.store.fts_available:
            self.skipTest("SQLite FTS5 unavailable")
        self.store.upsert_docs(SEED)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_recall(self, question, generate, **kwargs):
        kwargs.setdefault("now", NOW)
        return asyncio.run(recall(question, self.store, generate=generate, **kwargs))


class FullChainTests(RecallTestCase):
    def test_plan_search_answer_and_links(self):
        generate = FakeGenerate(plan=PLAN_STORAGE, answer="安大略在扩大电池储能采购 [S1]，笔记也提到 IESO [S2]。")
        result = self.run_recall("安大略储能最近怎么样？", generate)

        self.assertEqual(result.status, "ok")
        self.assertEqual(generate.routes(), [PLAN_ROUTE, ANSWER_ROUTE])
        plan_call, answer_call = generate.calls
        self.assertEqual(plan_call["json_schema"], PLAN_SCHEMA)
        self.assertTrue(plan_call["json_mode"])
        self.assertFalse(plan_call["use_search"])
        self.assertLessEqual(len(plan_call["text"]), ai_client.CLAUDE_ROUTES[PLAN_ROUTE].max_input_chars)
        self.assertNotIn("json_schema", answer_call)
        self.assertFalse(answer_call.get("json_mode", False))
        self.assertEqual(answer_call["system"], ANSWER_SYSTEM)
        self.assertLessEqual(len(answer_call["text"]), ai_client.CLAUDE_ROUTES[ANSWER_ROUTE].max_input_chars)
        self.assertIn("[S1]", answer_call["text"])
        self.assertIn("[S2]", answer_call["text"])

        self.assertEqual(result.terms, ("储能", "battery storage"))
        self.assertFalse(result.plan_fallback)
        self.assertEqual(result.plan_provider, "Claude")
        self.assertEqual(result.hits, 2)
        self.assertEqual(result.evidence_count, 2)
        self.assertEqual((result.provider, result.model, result.degraded), ("Claude", "claude-opus-5-5", False))
        # The inbox note is boosted 1.5x; both are AND hits of their language.
        self.assertEqual([s.number for s in result.sources], [1, 2])
        urls = {s.doc_id: s.url for s in result.sources}
        self.assertEqual(urls, {"inbox:1": "https://blog.example.org/storage",
                                "news:1": "https://news.example.com/reuters/1"})
        rendered = result.render()
        self.assertIn("### 来源", rendered)
        self.assertIn("(https://news.example.com/reuters/1)", rendered)
        self.assertIn("Reuters", rendered)
        self.assertIn("收藏", rendered)

    def test_urls_never_reach_the_model(self):
        generate = FakeGenerate(plan={"zh": ["关税"], "en": ["tariffs", "battery storage"], "fallback_terms": []})
        result = self.run_recall("关税和储能", generate)
        self.assertEqual(result.status, "ok")
        sent = "\n".join(call["text"] + call.get("system", "") for call in generate.calls)
        self.assertNotIn("http", sent)
        self.assertNotIn("example.com", sent)
        self.assertNotIn("example.org", sent)

    def test_answer_system_marks_evidence_untrusted_and_local(self):
        self.assertIn("不可信", ANSWER_SYSTEM)
        self.assertIn("个人收藏", ANSWER_SYSTEM)
        self.assertIn("[S1]", ANSWER_SYSTEM)

    def test_free_chain_answer_is_marked_degraded(self):
        generate = FakeGenerate(plan=PLAN_STORAGE, provider="Groq")
        result = self.run_recall("储能", generate)
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.degraded)
        self.assertEqual(result.attribution, "Groq (qwen)")

    def test_scope_and_days_filter(self):
        self.store.upsert_docs([news(9, "Old battery storage story", "battery storage", age_days=200)])
        generate = FakeGenerate(plan=PLAN_STORAGE)
        result = self.run_recall("储能", generate, scope="news", days=90)
        ids = [s.doc_id for s in result.evidence]
        self.assertEqual(ids, ["news:1"])
        result = self.run_recall("储能", generate, scope="news", days=365)
        self.assertEqual(sorted(s.doc_id for s in result.evidence), ["news:1", "news:9"])
        with self.assertRaises(ValueError):
            self.run_recall("储能", generate, scope="web")


class PlanFallbackTests(RecallTestCase):
    def test_plan_failure_uses_deterministic_terms(self):
        generate = FakeGenerate(plan=ai_client.AIServiceUnavailable("down"))
        result = self.run_recall("储能电站最近有什么进展", generate)
        self.assertTrue(result.plan_fallback)
        self.assertIsNone(result.plan_provider)
        self.assertEqual(result.terms, ("储能", "能电", "电站"))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.evidence[0].doc_id, "inbox:1")
        self.assertEqual(generate.routes(), [PLAN_ROUTE, ANSWER_ROUTE])

    def test_invalid_or_empty_plan_falls_back(self):
        for reply in ("not json", {"zh": [], "en": [], "fallback_terms": ["x"]}, "[1, 2]"):
            generate = FakeGenerate(plan=reply)
            result = self.run_recall("US tariffs Canada", generate)
            self.assertTrue(result.plan_fallback, reply)
            self.assertEqual(result.terms, ("us", "tariffs", "canada"))
            self.assertEqual(result.evidence[0].doc_id, "news:2")

    def test_plan_timeout_falls_back(self):
        async def slow(text, **kwargs):
            if kwargs["route"] == PLAN_ROUTE:
                await asyncio.sleep(10)
            return AIResult("答 [S1]", "Claude", "m")

        with patch.object(recall_module, "PLAN_TIMEOUT_SECONDS", 0.01):
            result = self.run_recall("tariffs", slow)
        self.assertTrue(result.plan_fallback)
        self.assertEqual(result.status, "ok")

    def test_broad_terms_only_when_specific_terms_miss(self):
        generate = FakeGenerate(plan={"zh": ["抽水蓄能"], "en": ["pumped hydro"], "fallback_terms": ["nickel"]})
        result = self.run_recall("抽水蓄能", generate)
        self.assertEqual(result.terms, ("抽水蓄能", "pumped hydro", "nickel"))
        self.assertEqual([s.doc_id for s in result.evidence], ["news:3"])

        generate = FakeGenerate(plan={"zh": ["储能"], "en": [], "fallback_terms": ["nickel"]})
        result = self.run_recall("储能", generate)
        self.assertEqual(result.terms, ("储能",))
        self.assertNotIn("news:3", [s.doc_id for s in result.evidence])

    def test_fallback_terms_and_parse_plan(self):
        self.assertEqual(fallback_terms("储能电站最近有什么进展"), ["储能", "能电", "电站"])
        self.assertEqual(fallback_terms("What did Reuters say about US tariffs?"),
                         ["reuters", "say", "us", "tariffs"])
        self.assertEqual(fallback_terms("我之前收藏的关于 OpenAI 的文章"), ["openai"])
        self.assertEqual(fallback_terms("什么？"), [])
        plan = parse_plan(json.dumps({"zh": ["a", "b", "c", "d", "e", 3, " ", "x" * 41],
                                      "en": ["Tariff", "tariff", '"AND"'], "fallback_terms": "bad"}))
        self.assertEqual(plan, {"zh": ["a", "b", "c", "d"], "en": ["Tariff", "AND"], "fallback_terms": []})


class NoModelOutcomeTests(RecallTestCase):
    def test_zero_hits_does_not_answer(self):
        generate = FakeGenerate(plan={"zh": ["量子计算"], "en": ["quantum computing"], "fallback_terms": []})
        result = self.run_recall("量子计算", generate)
        self.assertEqual(result.status, "no_results")
        self.assertEqual(generate.routes(), [PLAN_ROUTE])
        self.assertIn("量子计算", result.answer)
        self.assertIn("quantum computing", result.answer)
        self.assertEqual((result.hits, result.sources), (0, ()))

    def test_fts_unavailable_calls_no_model(self):
        generate = FakeGenerate(plan=PLAN_STORAGE)
        self.store.fts_available = False
        result = self.run_recall("储能", generate)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(generate.calls, [])
        self.assertIn("检索不可用", result.answer)

    def test_search_failure_reports_unavailable(self):
        generate = FakeGenerate(plan=PLAN_STORAGE)
        with patch.object(self.store, "search", side_effect=KnowledgeUnavailable("broken")):
            result = self.run_recall("储能", generate)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(generate.routes(), [PLAN_ROUTE])

    def test_budget_full_skips_both_model_steps(self):
        limits = {"daily_calls": 2, "daily_output_tokens": 28000}
        for _ in range(2):
            self.assertTrue(self.store.reserve_budget(DAY_KEY, 1, max_calls=2, max_tokens=28000))
        generate = FakeGenerate(plan=PLAN_STORAGE)
        result = self.run_recall("储能电站", generate, limits=limits)
        self.assertEqual(result.status, "budget_exhausted")
        self.assertEqual(generate.calls, [])
        self.assertTrue(result.plan_fallback)
        self.assertGreater(result.hits, 0)
        self.assertIn("额度", result.answer)
        self.assertEqual(result.sources, ())

    def test_plan_and_answer_reserve_separately(self):
        limits = {"daily_calls": 2, "daily_output_tokens": 28000}
        self.assertTrue(self.store.reserve_budget(DAY_KEY, 1, max_calls=2, max_tokens=28000))
        generate = FakeGenerate(plan=PLAN_STORAGE)
        result = self.run_recall("储能", generate, limits=limits)
        self.assertEqual(generate.routes(), [PLAN_ROUTE])
        self.assertEqual(result.status, "budget_exhausted")
        row = self.store.db.execute("SELECT calls, output_tokens FROM budgets WHERE day=?", (DAY_KEY,)).fetchone()
        self.assertEqual((row["calls"], row["output_tokens"]), (2, 1 + recall_module.PLAN_OUTPUT_TOKENS))

        self.store.db.execute("DELETE FROM budgets")
        self.store.db.commit()
        result = self.run_recall("储能", FakeGenerate(plan=PLAN_STORAGE))
        row = self.store.db.execute("SELECT calls, output_tokens FROM budgets WHERE day=?", (DAY_KEY,)).fetchone()
        self.assertEqual((row["calls"], row["output_tokens"]),
                         (2, recall_module.PLAN_OUTPUT_TOKENS + recall_module.ANSWER_OUTPUT_TOKENS))
        self.assertEqual(result.status, "ok")

    def test_zero_hits_reserves_no_answer_budget(self):
        self.run_recall("量子", FakeGenerate(plan={"zh": ["量子"], "en": [], "fallback_terms": []}))
        row = self.store.db.execute("SELECT calls FROM budgets WHERE day=?", (DAY_KEY,)).fetchone()
        self.assertEqual(row["calls"], 1)


class AnswerFailureTests(RecallTestCase):
    def test_model_failure_raises_readable_error_with_fallback(self):
        for error in (ai_client.AIServiceUnavailable("all down"), RuntimeError("boom")):
            generate = FakeGenerate(plan=PLAN_STORAGE, answer=error)
            with self.assertRaises(RecallError) as caught:
                self.run_recall("储能", generate)
            self.assertNotIn("boom", str(caught.exception))
            self.assertIs(caught.exception.__cause__, error)
            fallback = caught.exception.fallback
            self.assertEqual(fallback.status, "answer_failed")
            self.assertEqual([s.doc_id for s in fallback.sources], ["inbox:1", "news:1"])
            self.assertIn("https://news.example.com/reuters/1", fallback.render())

    def test_empty_answer_is_a_readable_error(self):
        with self.assertRaises(RecallError):
            self.run_recall("储能", FakeGenerate(plan=PLAN_STORAGE, answer="   "))


class CitationTests(RecallTestCase):
    def setUp(self):
        super().setUp()
        sources = ["Reuters", "AP", "CBC", "BBC", "Globe", "FT", "WSJ", "NYT"]
        self.store.upsert_docs([news(100 + i, f"Tariff update {i}", f"tariff detail number {i}", source=src)
                                for i, src in enumerate(sources)])
        self.plan = {"zh": [], "en": ["tariff"], "fallback_terms": []}

    def test_restores_cited_links_and_drops_unknown_numbers(self):
        result = self.run_recall("tariff", FakeGenerate(plan=self.plan, answer="甲 [S2]，乙 [S99]，丙 [s2] [S0]。"))
        self.assertEqual([s.number for s in result.sources], [2])
        self.assertNotIn("S99", result.answer)
        self.assertNotIn("S0]", result.answer)
        self.assertIn("[S2]", result.answer)
        self.assertTrue(result.sources[0].url.startswith("https://news.example.com/"))

    def test_no_citation_keeps_first_three(self):
        result = self.run_recall("tariff", FakeGenerate(plan=self.plan, answer="没有引用的回答。"))
        self.assertEqual([s.number for s in result.sources], [1, 2, 3])

    def test_at_most_six_links(self):
        answer = " ".join(f"[S{n}]" for n in range(8, 0, -1))
        result = self.run_recall("tariff", FakeGenerate(plan=self.plan, answer=answer))
        self.assertEqual([s.number for s in result.sources], [8, 7, 6, 5, 4, 3])
        self.assertEqual(cited_numbers("[S1] [S1] [S3]", 2), [1])

    def test_per_source_cap(self):
        self.store.upsert_docs([news(200 + i, f"Tariff Reuters {i}", "tariff", source="Reuters") for i in range(5)])
        result = self.run_recall("tariff", FakeGenerate(plan=self.plan))
        reuters = [s for s in result.evidence if s.source == "Reuters"]
        self.assertEqual(len(reuters), recall_module.PER_SOURCE)
        self.assertLessEqual(result.evidence_count, recall_module.MAX_EVIDENCE)

    def test_render_without_url_and_budget(self):
        result = self.run_recall("关税", FakeGenerate(plan={"zh": ["关税"], "en": [], "fallback_terms": []},
                                                    answer="很长" * 3000 + " [S1]"))
        self.assertEqual(result.sources[0].doc_id, "inbox:2")
        self.assertEqual(result.sources[0].url, "")
        rendered = result.render(max_chars=500)
        self.assertLessEqual(len(rendered), 500)
        self.assertIn("- [S1] 关税读书笔记 · 收藏", rendered)


class EvidenceLimitTests(RecallTestCase):
    def test_oversized_evidence_drops_whole_items(self):
        long_title = "Grid storage " + "x" * 450
        docs = [news(300 + i, f"{long_title} {i}", ("grid storage " + "y" * 50 + " ") * 40, source=f"S{i}")
                for i in range(12)]
        self.store.upsert_docs(docs)
        generate = FakeGenerate(plan={"zh": [], "en": ["grid storage"], "fallback_terms": []})
        result = self.run_recall("grid storage", generate)
        prompt = generate.calls[1]["text"]
        cap = ai_client.CLAUDE_ROUTES[ANSWER_ROUTE].max_input_chars
        self.assertLessEqual(len(prompt), cap)
        self.assertLess(result.evidence_count, 12)
        self.assertGreater(result.evidence_count, 0)
        self.assertIn(f"[S{result.evidence_count}]", prompt)
        self.assertNotIn(f"[S{result.evidence_count + 1}]", prompt)
        # Every included passage is complete (≤ 600 chars, not cut by the prompt cap).
        for source in result.evidence:
            body = self.store.get(source.doc_id)["body"]
            snippet = recall_module.passage(body, list(result.terms), limit=recall_module.PASSAGE_CHARS)
            self.assertLessEqual(len(snippet), 600)
            self.assertIn(snippet, prompt)


class SchemaTests(unittest.TestCase):
    def test_plan_schema_follows_claude_rules(self):
        def walk(node):
            if node.get("type") == "object":
                self.assertIs(node.get("additionalProperties"), False)
                self.assertEqual(set(node["required"]), set(node["properties"]))
            for key in ("minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum"):
                self.assertNotIn(key, node)
            for child in node.get("properties", {}).values():
                walk(child)
            if isinstance(node.get("items"), dict):
                walk(node["items"])

        walk(PLAN_SCHEMA)
        self.assertEqual(set(PLAN_SCHEMA["properties"]), {"zh", "en", "fallback_terms"})


if __name__ == "__main__":
    unittest.main()
