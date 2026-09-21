"""Single-process SQLite state. Discord delivery is deliberately not a DB transaction."""
import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from core.news.models import Article

MAX_ARTICLES = 3000
RAW_TTL = 7 * 86400


class NewsStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS articles (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, url TEXT NOT NULL,
                title TEXT NOT NULL, content TEXT NOT NULL, category TEXT NOT NULL,
                published_at REAL, first_seen REAL NOT NULL, version TEXT NOT NULL,
                last_seen REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS article_versions (
                article_id TEXT NOT NULL, version TEXT NOT NULL, evidence TEXT NOT NULL,
                observed_at REAL NOT NULL, PRIMARY KEY(article_id, version));
            CREATE TABLE IF NOT EXISTS processing (
                cache_key TEXT PRIMARY KEY, topic TEXT NOT NULL, rule_version TEXT NOT NULL,
                params TEXT NOT NULL, model_text TEXT NOT NULL, attribution TEXT NOT NULL,
                created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS processed_items (
                cache_key TEXT NOT NULL, article_id TEXT NOT NULL, article_version TEXT NOT NULL,
                result TEXT NOT NULL, PRIMARY KEY(cache_key, article_id));
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, subscription TEXT NOT NULL, period TEXT NOT NULL,
                channel_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '[]',
                message_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(subscription, period));
            CREATE TABLE IF NOT EXISTS deliveries (
                subscription TEXT NOT NULL, identity TEXT NOT NULL, url TEXT NOT NULL,
                article_version TEXT NOT NULL, run_id INTEGER, channel_id TEXT NOT NULL,
                message_id TEXT, status TEXT NOT NULL, payload TEXT NOT NULL,
                delivered_at REAL NOT NULL, PRIMARY KEY(subscription, identity));
            CREATE INDEX IF NOT EXISTS delivery_url ON deliveries(subscription, url);
            CREATE TABLE IF NOT EXISTS budgets (
                day TEXT PRIMARY KEY, calls INTEGER NOT NULL, output_tokens INTEGER NOT NULL);
        ''')
        version = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if version and version['value'] != '1':
            self.close()
            raise RuntimeError('Unsupported news database schema')
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', '1')")
            # Only one news runtime owns this DB. A restart never resumes a send.
            self.db.execute("UPDATE runs SET status='uncertain' WHERE status='sending'")
            self.db.execute("UPDATE runs SET status='failed' WHERE status='building'")

    def close(self):
        self.db.close()

    @property
    def ready(self):
        return self.db.execute("SELECT 1 FROM meta WHERE key='history_imported'").fetchone() is not None

    def upsert_articles(self, articles, *, now=None):
        now = time.time() if now is None else now
        with self.db:
            for article in articles:
                self.db.execute('''INSERT INTO articles VALUES
                    (:id,:source,:url,:title,:content,:category,:published_at,:first_seen,:version,:last_seen)
                    ON CONFLICT(id) DO UPDATE SET title=excluded.title, content=excluded.content,
                    category=excluded.category, published_at=excluded.published_at,
                    version=excluded.version, last_seen=excluded.last_seen''',
                    {**asdict(article), 'last_seen': now})
                first_seen = self.db.execute('SELECT first_seen FROM articles WHERE id=?', (article.id,)).fetchone()[0]
                self.db.execute('INSERT OR IGNORE INTO article_versions VALUES (?,?,?,?)',
                    (article.id, article.version, json.dumps({**asdict(article), 'first_seen': first_seen}, ensure_ascii=False), now))
            self.db.execute('DELETE FROM article_versions WHERE observed_at < ?', (now - RAW_TTL,))
            self.db.execute('''DELETE FROM article_versions WHERE rowid NOT IN
                (SELECT rowid FROM article_versions ORDER BY observed_at DESC LIMIT ?)''', (MAX_ARTICLES * 2,))
            self.db.execute('DELETE FROM articles WHERE COALESCE(published_at, first_seen) < ?', (now - RAW_TTL,))
            self.db.execute('''DELETE FROM articles WHERE id NOT IN
                (SELECT id FROM articles ORDER BY COALESCE(published_at,first_seen) DESC LIMIT ?)''', (MAX_ARTICLES,))
            self.db.execute('DELETE FROM processing WHERE created_at < ?', (now - RAW_TTL,))
            self.db.execute('DELETE FROM processed_items WHERE cache_key NOT IN (SELECT cache_key FROM processing)')
            self.db.execute('DELETE FROM budgets WHERE day < date(?, \'unixepoch\', \'-7 days\')', (now,))
        return len(articles)

    def articles(self, sources, *, age, now=None):
        now = time.time() if now is None else now
        rows = self.db.execute('''SELECT * FROM articles WHERE COALESCE(published_at,first_seen)
            BETWEEN ? AND ? ORDER BY COALESCE(published_at,first_seen) DESC, id''', (now - age, now + 3600))
        return [Article(**{k: row[k] for k in Article.__dataclass_fields__})
                for row in rows if row['source'] in sources]

    def history(self, subscription, limit=200):
        rows = self.db.execute('''SELECT payload,delivered_at FROM deliveries WHERE subscription=?
            ORDER BY delivered_at DESC LIMIT ?''', (subscription, limit))
        result = []
        for row in rows:
            item = json.loads(row['payload'])
            evidence = item.get('_evidence', item)
            result.append({**evidence, 'delivered_at': row['delivered_at'],
                           'topic_result': {k: v for k, v in item.items() if not k.startswith('_')}})
        return result

    def seen(self, subscription, item, identity=None):
        # Unknown legacy evidence blocks the URL conservatively, not every topic.
        if self.db.execute('''SELECT 1 FROM deliveries WHERE subscription=? AND url=?
                AND article_version IN (?, '*') LIMIT 1''',
                (subscription, item['url'], item['_version'])).fetchone():
            return True
        return identity is not None and self.db.execute(
            'SELECT 1 FROM deliveries WHERE subscription=? AND identity=?',
            (subscription, identity)).fetchone() is not None

    def cached(self, key):
        row = self.db.execute('SELECT model_text,attribution FROM processing WHERE cache_key=?', (key,)).fetchone()
        return tuple(row) if row else None

    def cache(self, key, topic, version, params, model_text, attribution, candidates, selected):
        by_id = {item['_article_id']: item for item in selected}
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO processing VALUES (?,?,?,?,?,?,?)',
                (key, topic, version, json.dumps(params, sort_keys=True), model_text, attribution, time.time()))
            self.db.executemany('INSERT OR REPLACE INTO processed_items VALUES (?,?,?,?)',
                [(key, c['_article_id'], c['_version'], json.dumps(by_id.get(c['_article_id']), ensure_ascii=False))
                 for c in candidates])

    def reserve_budget(self, day, tokens, *, max_calls, max_tokens):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO budgets VALUES (?,0,0)', (day,))
            updated = self.db.execute('''UPDATE budgets SET calls=calls+1, output_tokens=output_tokens+?
                WHERE day=? AND calls < ? AND output_tokens+? <= ?''',
                (tokens, day, max_calls, tokens, max_tokens))
            return updated.rowcount == 1

    def claim(self, subscription, period, channel_id):
        if not self.ready:
            raise RuntimeError('新闻历史尚未初始化；先停止旧任务并显式导入历史')
        now = time.time()
        with self.db:
            if self.db.execute("SELECT 1 FROM runs WHERE subscription=? AND status IN ('sending','uncertain')",
                               (subscription,)).fetchone():
                return None
            cursor = self.db.execute('''INSERT OR IGNORE INTO runs
                (subscription,period,channel_id,status,created_at,updated_at) VALUES (?,?,?,'building',?,?)''',
                (subscription, period, str(channel_id), now, now))
            return cursor.lastrowid if cursor.rowcount else None

    def set_status(self, run_id, status):
        with self.db:
            self.db.execute('UPDATE runs SET status=?,updated_at=? WHERE id=?', (status, time.time(), run_id))

    def intent(self, run_id, selected):
        with self.db:
            updated = self.db.execute("UPDATE runs SET status='sending',payload=?,updated_at=? WHERE id=? AND status='building'",
                                     (json.dumps(selected, ensure_ascii=False), time.time(), run_id))
            if updated.rowcount != 1:
                raise RuntimeError('新闻投递状态冲突')

    def complete(self, run_id, message_id, *, status='delivered'):
        if status not in {'delivered', 'skipped'}:
            raise ValueError('Invalid resolution')
        with self.db:
            row = self.db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None or row['status'] not in {'sending', 'uncertain'}:
                raise ValueError('只能确认待核查的发送')
            now = time.time()
            for item in json.loads(row['payload']):
                self.db.execute('INSERT OR IGNORE INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (row['subscription'], item['_delivery_key'], item['url'], item['_version'],
                     run_id, row['channel_id'], str(message_id) if message_id else None, status,
                     json.dumps(item, ensure_ascii=False), now))
            self.db.execute('UPDATE runs SET status=?,message_id=?,updated_at=? WHERE id=?',
                            (status, str(message_id) if message_id else None, now, run_id))

    def get_run(self, run_id):
        row = self.db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        return dict(row) if row else None

    def status(self, subscription):
        row = self.db.execute('SELECT id,period,status,message_id FROM runs WHERE subscription=? ORDER BY id DESC LIMIT 1',
                              (subscription,)).fetchone()
        pending = self.db.execute("SELECT id FROM runs WHERE subscription=? AND status IN ('sending','uncertain') ORDER BY id LIMIT 1",
                                  (subscription,)).fetchone()
        return {'latest': dict(row) if row else None, 'pending': pending['id'] if pending else None}

    def import_history(self, records):
        """Explicit, atomic, idempotent import. Caller supplies validated legacy snapshots."""
        with self.db:
            for subscription, channel, item in records:
                self.db.execute('INSERT OR IGNORE INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (subscription, item['_delivery_key'], item['url'], item['_version'], None,
                     str(channel or ''), None, 'legacy', json.dumps(item, ensure_ascii=False), item['delivered_at']))
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('history_imported','1')")
