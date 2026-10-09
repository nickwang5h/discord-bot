"""Read-only views of `news.sqlite3` for consumers outside the news pipeline.

Every query opens a short-lived `mode=ro` connection: it never creates the file, never
changes its schema and never holds a lock between calls. A missing file or a database
the news runtime has not initialised yet reads as empty.
"""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from core.news.models import Article


class _ReadOnly:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _query(self, sql, params=()):
        if not self.path.is_file():
            return []
        try:
            uri = f'{self.path.resolve().as_uri()}?mode=ro'
            with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
                db.row_factory = sqlite3.Row
                return [dict(row) for row in db.execute(sql, params)]
        except sqlite3.OperationalError as error:
            # Tables appear when NewsStore first opens the file; until then there is nothing to read.
            if 'no such table' in str(error):
                return []
            raise


def _payload(text, default):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


class PoolReader(_ReadOnly):
    """Raw material currently in the pool (kept by the news store for seven days)."""

    @staticmethod
    def _articles(rows):
        return [Article(**{key: row[key] for key in Article.__dataclass_fields__}) for row in rows]

    def since(self, cursor, *, limit=1000):
        """Articles first seen after `cursor`, oldest first; one ingest batch shares one `first_seen`."""
        return self._articles(self._query(
            'SELECT * FROM articles WHERE first_seen > ? ORDER BY first_seen, id LIMIT ?',
            (cursor, limit)))

    def window(self, age, *, now, sources=None):
        """Articles published (or first seen) within `age` seconds before `now`, newest first."""
        rows = self._query('''SELECT * FROM articles WHERE COALESCE(published_at, first_seen)
            BETWEEN ? AND ? ORDER BY COALESCE(published_at, first_seen) DESC, id''', (now - age, now + 3600))
        articles = self._articles(rows)
        return articles if sources is None else [a for a in articles if a.source in sources]


class DeliveryReader(_ReadOnly):
    """Delivered news items and the runs (one Discord message each) that carried them."""

    def deliveries_since(self, cursor, *, limit=500, strict=False):
        """Delivery rows with `delivered_at >= cursor` (`>` when `strict`), oldest first,
        payload decoded.

        A page holds about `limit` rows but never splits one timestamp (one run's items
        share it), so the next page can start strictly after the last timestamp. The
        inclusive default re-reads rows at the cursor; callers must be idempotent.
        """
        op = '>' if strict else '>='
        rows = self._query(f'''SELECT subscription, identity, url, article_version, run_id, channel_id,
            message_id, status, payload, delivered_at FROM deliveries WHERE delivered_at {op} ?
            AND delivered_at <= COALESCE((SELECT delivered_at FROM deliveries WHERE delivered_at {op} ?
                ORDER BY delivered_at LIMIT 1 OFFSET ?), 9e999)
            ORDER BY delivered_at, run_id, identity''', (cursor, cursor, max(1, int(limit)) - 1))
        for row in rows:
            row['payload'] = _payload(row['payload'], {})
        return rows

    def get_run(self, run_id):
        rows = self._query('SELECT * FROM runs WHERE id=?', (int(run_id),))
        if not rows:
            return None
        run = rows[0]
        run['payload'] = _payload(run['payload'], [])
        return run

    def run_by_message(self, message_id):
        rows = self._query('SELECT id FROM runs WHERE message_id=? ORDER BY id DESC LIMIT 1', (str(message_id),))
        return self.get_run(rows[0]['id']) if rows else None

    def run_items(self, run_id):
        """The run's items in rendering order (empty when the run is unknown)."""
        run = self.get_run(run_id)
        return [item for item in run['payload'] if isinstance(item, dict)] if run else []
