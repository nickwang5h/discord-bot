"""Long-lived knowledge index: ``<STATE_ROOT>/data/knowledge.sqlite3``.

Documents live in ``docs``; ``docs_fts`` is an ordinary FTS5 table whose rowid
matches ``docs.rowid`` and stores ``index_form()`` text (CJK bigrams). Ordinary,
not external-content or contentless: the indexed text is not the original text,
and production SQLite 3.40 has no ``contentless_delete``. Every document write
updates both tables in one transaction.

When this SQLite build lacks FTS5 the store still writes and lists documents;
only ``search()`` raises ``KnowledgeUnavailable``. Once FTS5 is available again
the index is rebuilt from ``docs`` on open. All methods are synchronous and
serialized by one lock; async callers wrap them in ``asyncio.to_thread``.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.knowledge.text import INDEX_VERSION, fts_query, index_form, query_terms

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1"
DAY = 86400
TOKENIZE = "unicode61 remove_diacritics 2"
KINDS = {"news", "inbox"}
MAX_BODY_CHARS = {"news": 4_000, "inbox": 50_000}
MAX_TITLE_CHARS = 500

NEWS_TTL = 90 * DAY
PINNED_NEWS_TTL = 365 * DAY
MAX_NEWS_DOCS = 60_000
INBOX_WARN_DOCS = 5_000
DROPPED_INBOX_TTL = 30 * DAY
BUDGET_TTL_DAYS = 14
USAGE_TTL_DAYS = 104 * 7
REPORT_TTL = 104 * 7 * DAY
VACUUM_PAGES = 2000

REPORT_FINAL = {"delivered", "skipped", "failed", "uncertain"}


class KnowledgeUnavailable(RuntimeError):
    """Full-text search is unavailable (no FTS5 in this SQLite build)."""


@dataclass(frozen=True)
class KnowledgeDoc:
    id: str  # 'news:<article_id>' | 'inbox:<item_id>'
    kind: str
    title: str
    body: str
    added_at: float
    source: str = ""
    category: str = ""
    board: str = ""
    url: str = ""
    canonical_url: str = ""
    published_at: float | None = None
    state: str | None = None
    state_at: float | None = None
    version: str = ""


_DOC_FIELDS = tuple(KnowledgeDoc.__dataclass_fields__)
# Fields a re-sync may change. added_at, pinned and keep_until belong to the store.
_MUTABLE = ("kind", "source", "category", "board", "url", "canonical_url", "title", "body",
            "published_at", "state", "state_at", "version")


def probe_fts5() -> bool:
    """True when this SQLite build can create and query the FTS table we need."""
    try:
        probe = sqlite3.connect(":memory:")
        try:
            probe.execute(f"CREATE VIRTUAL TABLE t USING fts5(title, body, tokenize='{TOKENIZE}')")
            probe.execute("INSERT INTO t(rowid, title, body) VALUES (1, 'a', '储能 b')")
            row = probe.execute("SELECT rowid, bm25(t, 3.0, 1.0) FROM t WHERE t MATCH '\"储能\"'").fetchone()
            return row is not None and row[0] == 1
        finally:
            probe.close()
    except sqlite3.Error:
        return False


class KnowledgeStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self._open()
        except BaseException:
            self.db.close()
            raise

    def _open(self) -> None:
        # Only effective before the first table exists, which is exactly when it matters.
        self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS docs (
                rowid INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL CHECK(kind IN ('news','inbox')),
                source TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
                board TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
                canonical_url TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, body TEXT NOT NULL,
                published_at REAL, added_at REAL NOT NULL, state TEXT, state_at REAL,
                pinned INTEGER NOT NULL DEFAULT 0, keep_until REAL,
                version TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS docs_canonical_url ON docs(canonical_url);
            CREATE INDEX IF NOT EXISTS docs_kind_added ON docs(kind, added_at);
            CREATE INDEX IF NOT EXISTS docs_source ON docs(source);
            CREATE TABLE IF NOT EXISTS budgets (
                day TEXT PRIMARY KEY, calls INTEGER NOT NULL, output_tokens INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS usage (
                day TEXT NOT NULL, name TEXT NOT NULL, count INTEGER NOT NULL,
                PRIMARY KEY (day, name));
            CREATE TABLE IF NOT EXISTS reports (
                week TEXT PRIMARY KEY, status TEXT NOT NULL, channel_id TEXT NOT NULL,
                message_id TEXT, payload TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL, updated_at REAL NOT NULL);
        """)
        version = self._meta("schema_version")
        if version is not None and version != SCHEMA_VERSION:
            raise RuntimeError("Unsupported knowledge database schema")
        self.fts_available = probe_fts5()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
            # One runtime owns this DB. A restart never resumes a report send.
            self.db.execute("UPDATE reports SET status='uncertain' WHERE status='sending'")
            self.db.execute("UPDATE reports SET status='failed' WHERE status='building'")
            if self.fts_available:
                self._ensure_fts()
            else:
                logger.warning("SQLite FTS5 不可用：知识库只写入，不提供全文检索")
                self._set_meta("fts5", "0")

    # -- meta -----------------------------------------------------------------

    def _meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value))

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            return self._meta(key)

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self.db:
            self._set_meta(key, value)

    # -- FTS ------------------------------------------------------------------

    def _ensure_fts(self) -> None:
        existed = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='docs_fts'").fetchone() is not None
        if not existed:
            self.db.execute(f"CREATE VIRTUAL TABLE docs_fts USING fts5(title, body, tokenize='{TOKENIZE}')")
        # Rebuild when the index is new, was skipped while FTS5 was missing, or the
        # text form changed. Plain FTS5 'rebuild' is not available for this table type.
        if not existed or self._meta("fts5") != "1" or self._meta("index_version") != INDEX_VERSION:
            self.db.execute("DELETE FROM docs_fts")
            rows = self.db.execute("SELECT rowid, title, body FROM docs")
            self.db.executemany(
                "INSERT INTO docs_fts(rowid, title, body) VALUES (?, ?, ?)",
                ((row["rowid"], index_form(row["title"]), index_form(row["body"])) for row in rows))
            self._set_meta("index_version", INDEX_VERSION)
        self._set_meta("fts5", "1")

    def _fts_put(self, rowid: int, title: str, body: str) -> None:
        if self.fts_available:
            self.db.execute("DELETE FROM docs_fts WHERE rowid=?", (rowid,))
            self.db.execute("INSERT INTO docs_fts(rowid, title, body) VALUES (?, ?, ?)",
                            (rowid, index_form(title), index_form(body)))

    def _fts_delete_where(self, where: str, params: Sequence[Any] = ()) -> None:
        if self.fts_available:
            self.db.execute(f"DELETE FROM docs_fts WHERE rowid IN (SELECT rowid FROM docs WHERE {where})", params)

    # -- documents ------------------------------------------------------------

    @staticmethod
    def _clean(doc: KnowledgeDoc) -> dict[str, Any]:
        if doc.kind not in KINDS:
            raise ValueError(f"未知文档类型: {doc.kind}")
        if not doc.id.startswith(f"{doc.kind}:"):
            raise ValueError(f"文档 id 必须以 {doc.kind}: 开头")
        values = asdict(doc)
        values["title"] = (doc.title or "").strip()[:MAX_TITLE_CHARS]
        values["body"] = (doc.body or "").strip()[:MAX_BODY_CHARS[doc.kind]]
        return values

    def upsert_docs(self, docs: Iterable[KnowledgeDoc]) -> int:
        """Insert or update documents; returns how many rows actually changed.

        Re-writing an identical document is a no-op, so sync can replay safely.
        """
        changed = 0
        with self._lock, self.db:
            for doc in docs:
                values = self._clean(doc)
                row = self.db.execute("SELECT * FROM docs WHERE id=?", (values["id"],)).fetchone()
                if row is None:
                    cursor = self.db.execute(
                        f"INSERT INTO docs ({', '.join(_DOC_FIELDS)}) VALUES ({', '.join('?' * len(_DOC_FIELDS))})",
                        [values[field] for field in _DOC_FIELDS])
                    self._fts_put(cursor.lastrowid, values["title"], values["body"])
                    changed += 1
                    continue
                if all(row[field] == values[field] for field in _MUTABLE):
                    continue
                self.db.execute(
                    f"UPDATE docs SET {', '.join(f'{field}=?' for field in _MUTABLE)} WHERE rowid=?",
                    [values[field] for field in _MUTABLE] + [row["rowid"]])
                if row["title"] != values["title"] or row["body"] != values["body"]:
                    self._fts_put(row["rowid"], values["title"], values["body"])
                changed += 1
        return changed

    def delete_docs(self, ids: Iterable[str]) -> int:
        ids = list(dict.fromkeys(ids))
        deleted = 0
        with self._lock, self.db:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                marks = ",".join("?" * len(chunk))
                self._fts_delete_where(f"id IN ({marks})", chunk)
                deleted += self.db.execute(f"DELETE FROM docs WHERE id IN ({marks})", chunk).rowcount
        return deleted

    def get(self, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()
            return dict(row) if row else None

    def recent(self, *, kind: str | None = None, source: str | None = None,
               since: float | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Newest documents by ``added_at``; works without FTS5."""
        clauses, params = [], []
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind)
        if source is not None:
            clauses.append("source=?")
            params.append(source)
        if since is not None:
            clauses.append("added_at>=?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self.db.execute(f"SELECT * FROM docs {where} ORDER BY added_at DESC, rowid DESC LIMIT ?",
                                   (*params, max(0, int(limit))))
            return [dict(row) for row in rows]

    def search(self, query: str | Sequence[str], *, kind: str | None = None,
               since: float | None = None, limit: int = 40, mode: str = "or") -> list[dict[str, Any]]:
        """bm25-ranked documents (title weighted 3x). ``score`` is higher-is-better.

        ``query`` is free text or a list of terms; each term is matched as an escaped
        phrase, so user input never reaches FTS5 syntax. ``since`` filters on
        ``COALESCE(published_at, added_at)``. Raises ``KnowledgeUnavailable``
        without FTS5.
        """
        if not self.fts_available:
            raise KnowledgeUnavailable("全文检索不可用：SQLite 未提供 FTS5")
        terms = query_terms(query) if isinstance(query, str) else list(query)
        expression = fts_query(terms, mode=mode)
        if not expression:
            return []
        clauses, params = ["docs_fts MATCH ?"], [expression]
        if kind is not None:
            clauses.append("d.kind=?")
            params.append(kind)
        if since is not None:
            clauses.append("COALESCE(d.published_at, d.added_at)>=?")
            params.append(since)
        with self._lock:
            try:
                rows = self.db.execute(
                    f"""SELECT d.*, -bm25(docs_fts, 3.0, 1.0) AS score FROM docs_fts
                        JOIN docs d ON d.rowid = docs_fts.rowid
                        WHERE {' AND '.join(clauses)} ORDER BY score DESC, d.rowid DESC LIMIT ?""",
                    (*params, max(0, int(limit)))).fetchall()
            except sqlite3.OperationalError as error:
                # The expression is built from quoted phrases only; failing here means
                # the FTS index itself is unusable, not that the query was hostile.
                logger.warning("知识库全文检索失败: %s", error)
                raise KnowledgeUnavailable("全文检索暂不可用") from error
        return [dict(row) for row in rows]

    def pin_keys(self, canonical_urls: Iterable[str], *, exclusive: bool = True) -> int:
        """Pin news whose canonical URL was pushed, got feedback, or was saved.

        With ``exclusive`` the given set is the complete pin set and other news are
        unpinned. Returns the number of rows whose pin flag changed.
        """
        urls = sorted({url for url in canonical_urls if url})
        with self._lock, self.db:
            self.db.execute("CREATE TEMP TABLE IF NOT EXISTS pin_urls (url TEXT PRIMARY KEY)")
            self.db.execute("DELETE FROM temp.pin_urls")
            self.db.executemany("INSERT INTO temp.pin_urls VALUES (?)", ((url,) for url in urls))
            changed = self.db.execute("""UPDATE docs SET pinned=1 WHERE kind='news' AND pinned=0
                AND canonical_url IN (SELECT url FROM temp.pin_urls)""").rowcount
            if exclusive:
                changed += self.db.execute("""UPDATE docs SET pinned=0 WHERE kind='news' AND pinned=1
                    AND canonical_url NOT IN (SELECT url FROM temp.pin_urls)""").rowcount
            self.db.execute("DELETE FROM temp.pin_urls")
        return changed

    def set_keep_until(self, doc_id: str, keep_until: float | None) -> bool:
        """Explicit retention floor for one document; None restores the default rules."""
        with self._lock, self.db:
            return self.db.execute("UPDATE docs SET keep_until=? WHERE id=?", (keep_until, doc_id)).rowcount == 1

    # -- retention ------------------------------------------------------------

    def cleanup(self, *, now: float | None = None) -> dict[str, Any]:
        """Apply the retention policy (design §5.2 / §9 Q6) and vacuum incrementally.

        News: 90 days, pinned 365 days, at most 60,000 (unpinned go first). Inbox
        pending/done never expire (over 5,000 only warns); dropped inbox items leave
        the index 30 days after ``state_at``. ``keep_until`` in the future protects
        a document from age expiry. Budgets 14 days, usage counters and reports 104 weeks.
        """
        now = time.time() if now is None else now
        protected = "(keep_until IS NOT NULL AND keep_until > :now)"
        age = "COALESCE(published_at, added_at)"
        rules = {
            "news_expired": f"""kind='news' AND NOT {protected} AND (
                (pinned=0 AND {age} < :now - {NEWS_TTL}) OR (pinned=1 AND {age} < :now - {PINNED_NEWS_TTL}))""",
            "news_over_cap": f"""kind='news' AND rowid NOT IN (SELECT rowid FROM docs WHERE kind='news'
                ORDER BY (pinned=1 OR {protected}) DESC, {age} DESC, rowid DESC LIMIT {MAX_NEWS_DOCS})""",
            "inbox_dropped": f"""kind='inbox' AND state='dropped' AND NOT {protected}
                AND COALESCE(state_at, added_at) < :now - {DROPPED_INBOX_TTL}""",
        }
        result: dict[str, Any] = {}
        with self._lock:
            with self.db:
                for name, where in rules.items():
                    self._fts_delete_where(where, {"now": now})
                    result[name] = self.db.execute(f"DELETE FROM docs WHERE {where}", {"now": now}).rowcount
                result["budgets"] = self.db.execute(
                    f"DELETE FROM budgets WHERE day < date(?, 'unixepoch', '-{BUDGET_TTL_DAYS} days')",
                    (now,)).rowcount
                result["usage"] = self.db.execute(
                    f"DELETE FROM usage WHERE day < date(?, 'unixepoch', '-{USAGE_TTL_DAYS} days')",
                    (now,)).rowcount
                result["reports"] = self.db.execute(
                    "DELETE FROM reports WHERE created_at < ?", (now - REPORT_TTL,)).rowcount
                inbox = self.db.execute(
                    "SELECT COUNT(*) FROM docs WHERE kind='inbox' AND COALESCE(state, '') != 'dropped'").fetchone()[0]
            result["inbox_over_cap"] = inbox > INBOX_WARN_DOCS
            if result["inbox_over_cap"]:
                logger.warning("知识库收件箱文档 %s 条，超过 %s 条提示线", inbox, INBOX_WARN_DOCS)
            self.db.execute(f"PRAGMA incremental_vacuum({VACUUM_PAGES})").fetchall()
        return result

    def stats(self) -> dict[str, Any]:
        with self._lock:
            counts = {row["kind"]: row["n"] for row in self.db.execute(
                "SELECT kind, COUNT(*) AS n FROM docs GROUP BY kind")}
            pinned = self.db.execute("SELECT COUNT(*) FROM docs WHERE pinned=1").fetchone()[0]
            page_count = self.db.execute("PRAGMA page_count").fetchone()[0]
            page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
        return {"docs": sum(counts.values()), "news": counts.get("news", 0), "inbox": counts.get("inbox", 0),
                "pinned": pinned, "fts5": self.fts_available, "bytes": page_count * page_size}

    # -- model budget ---------------------------------------------------------

    def reserve_budget(self, day: str, tokens: int, *, max_calls: int, max_tokens: int) -> bool:
        with self._lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO budgets VALUES (?,0,0)", (day,))
            updated = self.db.execute("""UPDATE budgets SET calls=calls+1, output_tokens=output_tokens+?
                WHERE day=? AND calls < ? AND output_tokens+? <= ?""", (tokens, day, max_calls, tokens, max_tokens))
            return updated.rowcount == 1

    # -- command usage (weekly report) ----------------------------------------

    def bump_usage(self, name: str, day: str) -> None:
        """Count one use of ``name`` (e.g. 'recall') on local date ``day`` (YYYY-MM-DD)."""
        with self._lock, self.db:
            self.db.execute("INSERT INTO usage VALUES (?, ?, 1) "
                            "ON CONFLICT(day, name) DO UPDATE SET count=count+1", (day, name))

    def usage_count(self, name: str, since_day: str, until_day: str) -> int:
        """Uses of ``name`` on days ``since_day`` <= day < ``until_day``."""
        with self._lock:
            row = self.db.execute("SELECT COALESCE(SUM(count), 0) FROM usage WHERE name=? AND day>=? AND day<?",
                                  (name, since_day, until_day)).fetchone()
            return int(row[0])

    # -- weekly report (at-most-once) ----------------------------------------

    def claim_report(self, week: str, channel_id: int | str) -> bool:
        """Claim an ISO week once; a week that was ever claimed is never claimed again."""
        now = time.time()
        with self._lock, self.db:
            cursor = self.db.execute("""INSERT OR IGNORE INTO reports
                (week, status, channel_id, created_at, updated_at) VALUES (?, 'building', ?, ?, ?)""",
                (week, str(channel_id), now, now))
            return cursor.rowcount == 1

    def report_intent(self, week: str, payload: Any) -> None:
        with self._lock, self.db:
            updated = self.db.execute("""UPDATE reports SET status='sending', payload=?, updated_at=?
                WHERE week=? AND status='building'""", (json.dumps(payload, ensure_ascii=False), time.time(), week))
            if updated.rowcount != 1:
                raise RuntimeError("周报投递状态冲突")

    def complete_report(self, week: str, message_id: int | str | None, *, status: str = "delivered") -> None:
        if status not in REPORT_FINAL:
            raise ValueError(f"未知周报状态: {status}")
        with self._lock, self.db:
            updated = self.db.execute("""UPDATE reports SET status=?, message_id=?, updated_at=?
                WHERE week=? AND status IN ('building', 'sending')""",
                (status, str(message_id) if message_id else None, time.time(), week))
            if updated.rowcount != 1:
                raise RuntimeError("周报投递状态冲突")

    def get_report(self, week: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM reports WHERE week=?", (week,)).fetchone()
            return dict(row) if row else None

    def close(self) -> None:
        with self._lock:
            self.db.close()
