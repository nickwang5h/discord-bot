import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.knowledge import store as store_module
from core.knowledge.store import (
    DAY, MAX_BODY_CHARS, KnowledgeDoc, KnowledgeStore, KnowledgeUnavailable, probe_fts5,
)
from core.knowledge.text import fts_query, index_form, passage, query_terms

NOW = 1_800_000_000.0


def news(n, title, body="", *, source="Reuters", age_days=0, url=None, version="v1"):
    url = url or f"https://example.com/{n}"
    return KnowledgeDoc(id=f"news:{n}", kind="news", title=title, body=body, source=source,
                        url=url, canonical_url=url, published_at=NOW - age_days * DAY,
                        added_at=NOW - age_days * DAY, version=version)


def inbox(n, title, body="", *, state="pending", state_at=None, added_at=NOW):
    return KnowledgeDoc(id=f"inbox:{n}", kind="inbox", title=title, body=body, source="inbox",
                        added_at=added_at, state=state, state_at=state_at)


class TextTests(unittest.TestCase):
    def test_cjk_runs_become_overlapping_bigrams(self):
        self.assertEqual(index_form("储能电站"), "储能 能电 电站")
        self.assertEqual(index_form("关税"), "关税")
        self.assertEqual(index_form("美国对华关税 Tariff ＡＩ"), "美国 国对 对华 华关 关税 tariff ai")
        self.assertEqual(index_form("单 字"), "单 字")

    def test_query_uses_same_form_as_index(self):
        self.assertEqual(fts_query(["储能电站"]), '"储能 能电 电站"')
        self.assertEqual(fts_query("储能 battery"), '"储能" OR "battery"')
        self.assertEqual(fts_query(["储能", "电站"], mode="and"), '"储能" AND "电站"')
        self.assertEqual(fts_query(["电"]), '"电" *')

    def test_fts_syntax_is_neutralized(self):
        hostile = ['"', '*', '(', ')', 'AND', 'OR', 'NOT', 'NEAR(a b, 2)', 'title:x', '^start',
                   'a"b', '储能"*(', '-x', '+y', '{title body}:z']
        expression = fts_query(hostile)
        for phrase in expression.split(" OR "):
            self.assertRegex(phrase, r'^"[^"*()^:{}]+"( \*)?$')
        self.assertEqual(fts_query(['"', '*', '(', '()', '   ']), "")
        self.assertEqual(query_terms('" * ( 储能 储能'), ["储能"])

    def test_passage_windows_around_first_term(self):
        text = "甲" * 1000 + "关税上调" + "乙" * 1000
        window = passage(text, ["关税"], limit=600)
        self.assertLessEqual(len(window), 600)
        self.assertIn("关税上调", window)
        self.assertEqual(passage("short", ["x"]), "short")
        self.assertTrue(passage("x" * 700, ["missing"]).startswith("x"))


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "data" / "knowledge.sqlite3"

    def open(self):
        store = KnowledgeStore(self.path)
        self.addCleanup(store.close)
        return store

    def fts_rows(self, store):
        return store.db.execute("SELECT COUNT(*) FROM docs_fts").fetchone()[0]


class KnowledgeStoreTests(StoreTestCase):
    def test_schema_meta_and_pragmas(self):
        self.assertTrue(probe_fts5())
        store = self.open()
        self.assertTrue(store.fts_available)
        self.assertEqual(store.get_meta("schema_version"), "1")
        self.assertEqual(store.get_meta("fts5"), "1")
        self.assertEqual(store.db.execute("PRAGMA auto_vacuum").fetchone()[0], 2)  # incremental
        tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"meta", "docs", "docs_fts", "budgets", "reports"} <= tables)
        store.set_meta("news_cursor", "42")
        self.assertEqual(store.get_meta("news_cursor"), "42")

    def test_rejects_unknown_schema_version(self):
        store = self.open()
        store.set_meta("schema_version", "99")
        store.close()
        with self.assertRaises(RuntimeError):
            KnowledgeStore(self.path)

    def test_upsert_is_idempotent_and_keeps_fts_in_step(self):
        store = self.open()
        doc = news(1, "储能招标", "安大略长时储能采购")
        self.assertEqual(store.upsert_docs([doc]), 1)
        self.assertEqual(store.upsert_docs([doc]), 0)
        self.assertEqual(self.fts_rows(store), 1)
        added_at = store.get("news:1")["added_at"]

        updated = KnowledgeDoc(**{**doc.__dict__, "title": "关税新闻", "body": "加拿大反制关税",
                                  "version": "v2", "added_at": NOW + 5})
        self.assertEqual(store.upsert_docs([updated]), 1)
        self.assertEqual(self.fts_rows(store), 1)
        self.assertEqual(store.get("news:1")["added_at"], added_at)
        self.assertEqual(store.search("储能"), [])
        self.assertEqual([hit["id"] for hit in store.search("关税")], ["news:1"])

        self.assertEqual(store.delete_docs(["news:1", "news:missing"]), 1)
        self.assertEqual(self.fts_rows(store), 0)
        self.assertIsNone(store.get("news:1"))

    def test_upsert_validates_and_truncates(self):
        store = self.open()
        with self.assertRaises(ValueError):
            store.upsert_docs([KnowledgeDoc(id="news:1", kind="video", title="t", body="", added_at=NOW)])
        with self.assertRaises(ValueError):
            store.upsert_docs([KnowledgeDoc(id="inbox:1", kind="news", title="t", body="", added_at=NOW)])
        store.upsert_docs([news(2, "t", "x" * 10_000), inbox(3, "t", "y" * 60_000)])
        self.assertEqual(len(store.get("news:2")["body"]), MAX_BODY_CHARS["news"])
        self.assertEqual(len(store.get("inbox:3")["body"]), MAX_BODY_CHARS["inbox"])

    def test_chinese_two_character_words_hit(self):
        store = self.open()
        store.upsert_docs([
            news(1, "安大略储能招标结果公布", "独立电力系统运营商公布长时储能采购"),
            news(2, "美国宣布对加拿大钢铝加征关税", "加拿大政府表示将采取反制措施"),
            news(3, "Ottawa transit update", "LRT service resumes"),
            inbox(4, "读书笔记", "关于储能电站安全的长文"),
        ])
        self.assertEqual({hit["id"] for hit in store.search("储能")}, {"news:1", "inbox:4"})
        self.assertEqual([hit["id"] for hit in store.search("关税")], ["news:2"])
        self.assertEqual([hit["id"] for hit in store.search("储能电站")], ["inbox:4"])
        self.assertEqual([hit["id"] for hit in store.search("储能", kind="inbox")], ["inbox:4"])
        # Title is weighted above body.
        self.assertEqual(store.search("储能")[0]["id"], "news:1")
        self.assertEqual(store.search(["储能", "电站"], mode="and")[0]["id"], "inbox:4")
        # A lone character matches as a bigram prefix (关税, 关于).
        self.assertEqual({hit["id"] for hit in store.search("关")}, {"news:2", "inbox:4"})

    def test_english_search_and_filters(self):
        store = self.open()
        store.upsert_docs([
            news(1, "Battery storage auction in Ontario", "IESO results", age_days=1),
            news(2, "Tariffs on Canadian steel", "Retaliation planned", age_days=40),
            news(3, "Café résumé", "Diacritics fold"),
        ])
        self.assertEqual([hit["id"] for hit in store.search("TARIFFS")], ["news:2"])
        self.assertEqual(store.search("tariffs", since=NOW - 10 * DAY), [])
        self.assertEqual([hit["id"] for hit in store.search("cafe resume", mode="and")], ["news:3"])
        hits = store.search("battery steel")
        self.assertEqual({hit["id"] for hit in hits}, {"news:1", "news:2"})
        self.assertTrue(all(hit["score"] > 0 for hit in hits))
        self.assertEqual(len(store.search("battery steel", limit=1)), 1)

    def test_hostile_queries_do_not_raise(self):
        store = self.open()
        store.upsert_docs([news(1, "AND OR NOT near", "储能 (test) * \"quoted\"")])
        for query in ['"', '*', '(', ')', 'AND', 'NEAR', 'NEAR(储能 test)', '储能" OR "x', 'title:储能',
                      '^储能', 'a AND (b OR', '"""', '-储能', '{title}:x', 'NOT 储能', '\x00']:
            with self.subTest(query=query):
                self.assertIsInstance(store.search(query), list)
        self.assertEqual([hit["id"] for hit in store.search("AND")], ["news:1"])
        self.assertEqual(store.search('" * ('), [])

    def test_recent_reads_by_time_and_source(self):
        store = self.open()
        store.upsert_docs([news(1, "a", age_days=3, source="A"), news(2, "b", age_days=1, source="B"),
                           inbox(3, "c", added_at=NOW - 2 * DAY)])
        self.assertEqual([d["id"] for d in store.recent()], ["news:2", "inbox:3", "news:1"])
        self.assertEqual([d["id"] for d in store.recent(source="A")], ["news:1"])
        self.assertEqual([d["id"] for d in store.recent(kind="news", since=NOW - 2 * DAY)], ["news:2"])

    def test_pin_keys_is_exclusive_by_default(self):
        store = self.open()
        store.upsert_docs([news(1, "a"), news(2, "b")])
        self.assertEqual(store.pin_keys(["https://example.com/1", ""]), 1)
        self.assertEqual(store.get("news:1")["pinned"], 1)
        self.assertEqual(store.pin_keys(["https://example.com/2"]), 2)
        self.assertEqual((store.get("news:1")["pinned"], store.get("news:2")["pinned"]), (0, 1))
        self.assertEqual(store.pin_keys(["https://example.com/1"], exclusive=False), 1)
        self.assertEqual(store.stats()["pinned"], 2)
        # A re-sync does not reset store-owned fields.
        store.upsert_docs([news(1, "a2")])
        self.assertEqual(store.get("news:1")["pinned"], 1)

    def test_budget_and_report_lifecycle(self):
        store = self.open()
        self.assertTrue(store.reserve_budget("2027-01-01", 100, max_calls=1, max_tokens=500))
        self.assertFalse(store.reserve_budget("2027-01-01", 100, max_calls=1, max_tokens=500))
        self.assertTrue(store.claim_report("2027-W01", 123))
        self.assertFalse(store.claim_report("2027-W01", 123))
        store.report_intent("2027-W01", {"embeds": 2})
        with self.assertRaises(RuntimeError):
            store.report_intent("2027-W01", {})
        store.close()
        store = self.open()  # restart during a send
        self.assertEqual(store.get_report("2027-W01")["status"], "uncertain")
        with self.assertRaises(RuntimeError):
            store.complete_report("2027-W01", 1)
        self.assertTrue(store.claim_report("2027-W02", 123))
        store.report_intent("2027-W02", [])
        store.complete_report("2027-W02", 999)
        self.assertEqual(store.get_report("2027-W02")["message_id"], "999")


class RetentionTests(StoreTestCase):
    def test_age_rules(self):
        store = self.open()
        store.upsert_docs([
            news(1, "fresh", age_days=10),
            news(2, "old", age_days=91),
            news(3, "old pinned", age_days=200, url="https://example.com/p"),
            news(4, "too old pinned", age_days=366, url="https://example.com/q"),
            news(5, "kept explicitly", age_days=120),
            inbox(6, "pending forever", added_at=NOW - 5000 * DAY),
            inbox(7, "done forever", state="done", state_at=NOW - 5000 * DAY, added_at=NOW - 5000 * DAY),
            inbox(8, "dropped recently", state="dropped", state_at=NOW - 10 * DAY, added_at=NOW - 400 * DAY),
            inbox(9, "dropped long ago", state="dropped", state_at=NOW - 31 * DAY),
            inbox(10, "dropped legacy", state="dropped", added_at=NOW - 31 * DAY),
        ])
        store.pin_keys(["https://example.com/p", "https://example.com/q"])
        store.set_keep_until("news:5", NOW + DAY)
        with store.db:
            store.db.execute("INSERT INTO budgets VALUES ('2000-01-01', 1, 1)")
            store.db.execute("INSERT INTO reports VALUES ('2000-W01','delivered','1',NULL,'',0,0)")
        result = store.cleanup(now=NOW)
        remaining = {doc["id"] for doc in store.recent(limit=100)}
        self.assertEqual(remaining, {"news:1", "news:3", "news:5", "inbox:6", "inbox:7", "inbox:8"})
        self.assertEqual(result["news_expired"], 2)
        self.assertEqual(result["inbox_dropped"], 2)
        self.assertEqual((result["budgets"], result["reports"]), (1, 1))
        self.assertFalse(result["inbox_over_cap"])
        self.assertEqual(self.fts_rows(store), len(remaining))
        self.assertEqual([hit["id"] for hit in store.search("dropped")], ["inbox:8"])

    def test_news_cap_drops_unpinned_oldest_first(self):
        store = self.open()
        store.upsert_docs([news(i, f"n{i}", age_days=i, url=f"https://example.com/{i}") for i in range(1, 7)])
        store.pin_keys(["https://example.com/6"])
        with patch.object(store_module, "MAX_NEWS_DOCS", 3):
            result = store.cleanup(now=NOW)
        self.assertEqual(result["news_over_cap"], 3)
        self.assertEqual({d["id"] for d in store.recent()}, {"news:6", "news:1", "news:2"})
        self.assertEqual(self.fts_rows(store), 3)

    def test_inbox_over_cap_only_warns(self):
        store = self.open()
        store.upsert_docs([inbox(i, f"i{i}") for i in range(3)])
        with patch.object(store_module, "INBOX_WARN_DOCS", 2), self.assertLogs(store_module.logger, "WARNING"):
            result = store.cleanup(now=NOW)
        self.assertTrue(result["inbox_over_cap"])
        self.assertEqual(store.stats()["inbox"], 3)


class Fts5UnavailableTests(StoreTestCase):
    def test_store_degrades_and_rebuilds_when_fts_returns(self):
        with patch.object(store_module, "probe_fts5", return_value=False), \
                self.assertLogs(store_module.logger, "WARNING"):
            store = KnowledgeStore(self.path)
        try:
            self.assertFalse(store.fts_available)
            self.assertEqual(store.get_meta("fts5"), "0")
            self.assertFalse(store.stats()["fts5"])
            self.assertEqual(store.upsert_docs([news(1, "储能招标"), inbox(2, "关税笔记")]), 2)
            self.assertEqual(store.delete_docs(["inbox:2"]), 1)
            self.assertEqual([d["id"] for d in store.recent(source="Reuters")], ["news:1"])
            with self.assertRaises(KnowledgeUnavailable):
                store.search("储能")
            store.upsert_docs([news(3, "关税", age_days=200)])
            self.assertEqual(store.cleanup(now=NOW)["news_expired"], 1)
            tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master")}
            self.assertNotIn("docs_fts", tables)
        finally:
            store.close()

        store = self.open()  # FTS5 is back: index rebuilt from docs
        self.assertTrue(store.fts_available)
        self.assertEqual(store.get_meta("fts5"), "1")
        self.assertEqual([hit["id"] for hit in store.search("储能")], ["news:1"])
        self.assertEqual(store.search("关税"), [])

    def test_stale_index_after_fts_outage_is_rebuilt(self):
        store = self.open()
        store.upsert_docs([news(1, "储能"), news(2, "关税")])
        store.close()
        with patch.object(store_module, "probe_fts5", return_value=False), \
                self.assertLogs(store_module.logger, "WARNING"):
            degraded = KnowledgeStore(self.path)
        degraded.delete_docs(["news:2"])  # docs_fts still has row 2
        degraded.upsert_docs([news(3, "关税新规")])
        degraded.close()
        store = self.open()
        self.assertEqual(self.fts_rows(store), 2)
        self.assertEqual([hit["id"] for hit in store.search("关税")], ["news:3"])

    def test_probe_reports_missing_module(self):
        class Broken:
            def execute(self, *args):
                raise sqlite3.OperationalError("no such module: fts5")

            def close(self):
                pass

        with patch.object(store_module.sqlite3, "connect", return_value=Broken()):
            self.assertFalse(probe_fts5())

    def test_query_failure_becomes_unavailable(self):
        store = self.open()
        store.upsert_docs([news(1, "储能")])
        with store.db:
            store.db.execute("DROP TABLE docs_fts")
        with self.assertRaises(KnowledgeUnavailable), self.assertLogs(store_module.logger, "WARNING"):
            store.search("储能")


if __name__ == "__main__":
    unittest.main()
