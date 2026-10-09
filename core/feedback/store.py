"""Owner feedback on pushed items, and the exposures it is measured against.

One SQLite file under the state root. Exposures are copied from the news database
through a read-only reader; this module never writes `news.sqlite3`. Each item (keyed by
its canonical URL) has at most one effective verdict: an exact verdict (button, or a
reaction on a one-item message) beats a coarse one (a reaction shared by n items,
weight 1/n); every change is appended to `feedback_log`.
"""
import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from core.feedback.boards import board_of
from core.feedback.entities import extract_entities
from core.inbox import canonical_url
from core.news.personal import PERSONAL_GROUPS
from core.news.subscriptions import PERSONAL_TOPICS

SCHEMA_VERSION = '1'
VERDICTS = ('new', 'known', 'skip')
VIAS = ('button', 'reaction')
REF_KINDS = ('run', 'watch', 'inbox')
DAY = 86400
ITEM_TTL = 730 * DAY        # items, exposures, feedback
LOG_TTL = 365 * DAY         # feedback_log
SENT_RUN_STATES = {'sending', 'uncertain', 'delivered'}
EXCERPT_CHARS = 300
MAX_TAGS = 3
MAX_TAG_CHARS = 24

SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS items (
  key TEXT PRIMARY KEY, canonical_url TEXT NOT NULL, url TEXT NOT NULL,
  title TEXT NOT NULL, title_zh TEXT, source TEXT NOT NULL, category TEXT NOT NULL,
  board TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]', excerpt TEXT NOT NULL,
  first_shown_at REAL NOT NULL, last_shown_at REAL NOT NULL, shown_count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS exposures (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, ref_kind TEXT NOT NULL, ref_id TEXT NOT NULL,
  subscription TEXT, personal INTEGER NOT NULL, channel_id TEXT, message_id TEXT,
  position INTEGER, item_count INTEGER NOT NULL, shown_at REAL NOT NULL,
  UNIQUE(key, ref_kind, ref_id));
CREATE INDEX IF NOT EXISTS exposures_msg ON exposures(message_id);
CREATE INDEX IF NOT EXISTS exposures_time ON exposures(shown_at);
CREATE INDEX IF NOT EXISTS exposures_ref ON exposures(ref_kind, ref_id);
CREATE TABLE IF NOT EXISTS feedback (
  key TEXT PRIMARY KEY,
  verdict TEXT NOT NULL CHECK(verdict IN ('new','known','skip')),
  weight REAL NOT NULL, coarse INTEGER NOT NULL, via TEXT NOT NULL,
  message_id TEXT, emoji TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS feedback_log (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, verdict TEXT, weight REAL,
  via TEXT NOT NULL, message_id TEXT, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS saves (key TEXT PRIMARY KEY, inbox_id TEXT NOT NULL, at REAL NOT NULL);
'''


class Outcome(NamedTuple):
    verdict: str | None     # the effective verdict after the call
    changed: bool           # False: nothing written (no-op, or coarse refused by an exact verdict)


def item_key(url):
    """Same canonical URL (as the inbox computes it), same key across subscriptions."""
    return hashlib.sha256(canonical_url(url).encode('utf-8')).hexdigest()[:32]


def _text(value, limit):
    return ' '.join(str(value or '').split())[:limit]


def _model_tags(value):
    if not isinstance(value, list):
        return None
    tags = [_text(tag, MAX_TAG_CHARS) for tag in value if isinstance(tag, str) and tag.strip()]
    return tags[:MAX_TAGS] or None


def _personal(subscription_id, subscriptions):
    """Personal = a personal topic or personal groups; an unknown subscription counts as shared."""
    sub = subscriptions.get(subscription_id)
    if sub is None:
        return False
    return sub.topic in PERSONAL_TOPICS or any(group in PERSONAL_GROUPS for group in sub.source_groups)


def snapshot(item):
    """The item columns for one news payload entry, or None when it has no usable URL."""
    if not isinstance(item, dict):
        return None
    evidence = item.get('_evidence') if isinstance(item.get('_evidence'), dict) else {}
    url = evidence.get('url') or item.get('url')
    if not isinstance(url, str) or not url.startswith(('https://', 'http://')):
        return None
    title = _text(evidence.get('title') or item.get('title') or url, 300)
    rendered = _text(item.get('digest_title') or item.get('title'), 200)
    title_zh = rendered if rendered and rendered != title else None
    source = _text(evidence.get('publisher') or item.get('publisher') or item.get('source'), 60) or '未知来源'
    category = _text(evidence.get('category') or item.get('category'), 40)
    tags = _model_tags(item.get('tags'))
    return {
        'key': item_key(url), 'canonical_url': canonical_url(url), 'url': url,
        'title': title, 'title_zh': title_zh, 'source': source, 'category': category,
        'board': board_of(source, category),
        'tags': tags if tags is not None else (extract_entities(title) or extract_entities(title_zh)),
        'model_tags': tags is not None,
        'excerpt': _text(evidence.get('content') or evidence.get('rss_summary') or item.get('content'),
                         EXCERPT_CHARS),
    }


class FeedbackStore:
    def __init__(self, path: Path, *, now=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Callers may hop threads (asyncio.to_thread); one lock serialises all access.
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript(SCHEMA)
        version = self._meta('schema_version')
        if version is not None and version != SCHEMA_VERSION:
            self.close()
            raise RuntimeError('Unsupported feedback database schema')
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
            # Exposures before this moment never had buttons; stats ignore them as a denominator.
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('tracking_since', ?)",
                            (repr(time.time() if now is None else float(now)),))

    def close(self):
        with self._lock:
            self.db.close()

    def _meta(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row['value'] if row else None

    def _set_meta(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (key, str(value)))

    @property
    def tracking_since(self):
        with self._lock:
            return float(self._meta('tracking_since'))

    @property
    def exposure_cursor(self):
        with self._lock:
            return float(self._meta('exposure_cursor') or 0)

    # ---- verdicts -------------------------------------------------------------------

    def _log(self, key, verdict, weight, via, message_id, now):
        self.db.execute('INSERT INTO feedback_log (key, verdict, weight, via, message_id, at) VALUES (?,?,?,?,?,?)',
                        (key, verdict, weight, via, message_id, now))

    def current(self, key):
        with self._lock:
            row = self.db.execute('SELECT * FROM feedback WHERE key=?', (key,)).fetchone()
            return dict(row) if row else None

    def verdicts(self, keys):
        """`{key: {'verdict', 'coarse', 'via'}}` for the keys that have feedback (button state)."""
        keys = list(keys)
        if not keys:
            return {}
        with self._lock:
            rows = self.db.execute(f'SELECT key, verdict, coarse, via FROM feedback WHERE key IN '
                                   f'({",".join("?" * len(keys))})', keys)
            return {row['key']: {'verdict': row['verdict'], 'coarse': bool(row['coarse']), 'via': row['via']}
                    for row in rows}

    def record(self, key, verdict, *, via, weight=1.0, coarse=False, message_id=None, emoji=None, now=None):
        """Apply one click or reaction under the one-verdict-per-item rules.

        - no verdict yet: store it;
        - coarse never replaces an exact verdict (refused, nothing written);
        - exact replaces anything; coarse replaces coarse (the latest reaction wins);
        - a button pressing the current exact verdict again is an undo;
        - an identical repeat (same verdict, weight and coarseness) is a no-op.
        """
        if not isinstance(key, str) or not key or len(key) > 64:
            raise ValueError('无效的条目 key')
        if verdict not in VERDICTS or via not in VIAS:
            raise ValueError('无效的反馈')
        weight = float(weight)
        if not 0 < weight <= 1 or (not coarse and weight != 1.0):
            raise ValueError('权重无效：精确反馈为 1，粗反馈在 (0, 1]')
        now = time.time() if now is None else now
        message_id = str(message_id) if message_id is not None else None
        with self._lock, self.db:
            row = self.db.execute('SELECT * FROM feedback WHERE key=?', (key,)).fetchone()
            if row is not None:
                if coarse and not row['coarse']:
                    return Outcome(row['verdict'], False)
                # The button shows any exact verdict as selected, so pressing it again undoes it.
                if via == 'button' and not coarse and not row['coarse'] and row['verdict'] == verdict:
                    self.db.execute('DELETE FROM feedback WHERE key=?', (key,))
                    self._log(key, None, None, via, message_id, now)
                    return Outcome(None, True)
                if row['verdict'] == verdict and row['weight'] == weight and bool(row['coarse']) == bool(coarse):
                    return Outcome(verdict, False)
            self.db.execute('''INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET verdict=excluded.verdict, weight=excluded.weight,
                coarse=excluded.coarse, via=excluded.via, message_id=excluded.message_id,
                emoji=excluded.emoji, updated_at=excluded.updated_at''',
                (key, verdict, weight, int(bool(coarse)), via, message_id, emoji, now, now))
            self._log(key, verdict, weight, via, message_id, now)
            return Outcome(verdict, True)

    def undo(self, key, *, via, message_id=None, emoji=None, now=None):
        """Remove the verdict, but only if it came from this `via` (and, when given, this
        message and emoji): removing a reaction never erases a later button verdict."""
        if via not in VIAS:
            raise ValueError('无效的反馈来源')
        now = time.time() if now is None else now
        message_id = str(message_id) if message_id is not None else None
        with self._lock, self.db:
            row = self.db.execute('SELECT * FROM feedback WHERE key=?', (key,)).fetchone()
            if (row is None or row['via'] != via
                    or (message_id is not None and row['message_id'] != message_id)
                    or (emoji is not None and row['emoji'] != emoji)):
                return Outcome(row['verdict'] if row else None, False)
            self.db.execute('DELETE FROM feedback WHERE key=?', (key,))
            self._log(key, None, None, via, message_id, now)
            return Outcome(None, True)

    def record_save(self, key, inbox_id, *, now=None):
        """📥: remember that this item went to the inbox (independent of the verdict)."""
        with self._lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO saves VALUES (?,?,?)',
                            (key, str(inbox_id), time.time() if now is None else now))

    def saved(self, key):
        with self._lock:
            row = self.db.execute('SELECT inbox_id FROM saves WHERE key=?', (key,)).fetchone()
            return row['inbox_id'] if row else None

    # ---- exposures ------------------------------------------------------------------

    def _expose(self, snap, *, ref_kind, ref_id, subscription, personal, channel_id, message_id,
                position, item_count, shown_at):
        """Insert one exposure (idempotent) and refresh the item snapshot. Returns True if new."""
        ref_id = str(ref_id)
        existing = self.db.execute('SELECT id, message_id FROM exposures WHERE key=? AND ref_kind=? AND ref_id=?',
                                   (snap['key'], ref_kind, ref_id)).fetchone()
        if existing is not None:
            if existing['message_id'] is None and message_id is not None:
                self.db.execute('UPDATE exposures SET message_id=?, channel_id=COALESCE(channel_id, ?) WHERE id=?',
                                (str(message_id), channel_id and str(channel_id), existing['id']))
            return False
        self.db.execute('''INSERT INTO exposures (key, ref_kind, ref_id, subscription, personal, channel_id,
            message_id, position, item_count, shown_at) VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (snap['key'], ref_kind, ref_id, subscription, int(bool(personal)),
             str(channel_id) if channel_id is not None else None,
             str(message_id) if message_id is not None else None, position, item_count, shown_at))
        tags = json.dumps(snap['tags'], ensure_ascii=False)
        self.db.execute('''INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)
            ON CONFLICT(key) DO UPDATE SET
              title_zh=COALESCE(excluded.title_zh, items.title_zh),
              tags=CASE WHEN ? OR items.tags='[]' THEN excluded.tags ELSE items.tags END,
              first_shown_at=MIN(items.first_shown_at, excluded.first_shown_at),
              last_shown_at=MAX(items.last_shown_at, excluded.last_shown_at),
              shown_count=items.shown_count+1''',
            (snap['key'], snap['canonical_url'], snap['url'], snap['title'], snap['title_zh'], snap['source'],
             snap['category'], snap['board'], tags, snap['excerpt'], shown_at, shown_at,
             int(snap.get('model_tags', False))))
        return True

    def _expose_run(self, run_id, items, *, subscription, personal, channel_id, message_id, shown_at):
        new = 0
        for position, item in enumerate(items):
            snap = snapshot(item)
            if snap is None:
                continue
            new += self._expose(snap, ref_kind='run', ref_id=run_id, subscription=subscription, personal=personal,
                                channel_id=channel_id, message_id=message_id, position=position,
                                item_count=len(items), shown_at=shown_at)
        return new

    @staticmethod
    def _subscription_map(subscriptions):
        if isinstance(subscriptions, dict):
            return subscriptions
        return {sub.id: sub for sub in subscriptions or ()}

    def sync_exposures(self, reader, subscriptions, *, page=500):
        """Copy delivered news items after the cursor into items/exposures; returns new exposures.

        `reader` is a `DeliveryReader`; `subscriptions` the loaded `Subscription`s (list or
        `{id: Subscription}`). Only `delivered` rows count; legacy imports (no run) and
        manually skipped ones are passed over. Each page commits with its cursor.
        """
        subscriptions = self._subscription_map(subscriptions)
        cursor, total, strict = self.exposure_cursor, 0, False
        while True:
            # The first page re-reads the cursor timestamp; later pages start after it.
            rows = reader.deliveries_since(cursor, limit=page, strict=strict)
            if not rows:
                return total
            runs = {}
            with self._lock, self.db:
                for row in rows:
                    if row['status'] != 'delivered' or row['run_id'] is None:
                        continue
                    if row['run_id'] not in runs:
                        runs[row['run_id']] = reader.run_items(row['run_id'])
                    items = runs[row['run_id']]
                    position = next((i for i, item in enumerate(items)
                                     if item.get('_delivery_key') == row['identity']), None)
                    snap = snapshot(row['payload'])
                    if snap is None:
                        continue
                    total += self._expose(
                        snap, ref_kind='run', ref_id=row['run_id'], subscription=row['subscription'],
                        personal=_personal(row['subscription'], subscriptions), channel_id=row['channel_id'],
                        message_id=row['message_id'], position=position, item_count=max(len(items), 1),
                        shown_at=row['delivered_at'])
                last = rows[-1]['delivered_at']
                self._set_meta('exposure_cursor', repr(max(last, cursor)))
            cursor, strict = max(last, cursor), True

    def expose_inbox(self, item, *, now=None):
        """Snapshot an inbox card (one item) so feedback on it has a key; returns the key."""
        url = item.get('url') or ''
        if url:
            key, canonical = item_key(url), canonical_url(url)
        else:
            canonical = f"inbox:{item['id']}"
            key = hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]
        title = _text(item.get('title') or url or '未命名', 300)
        source = _text(item.get('source'), 60) or '收件箱'
        category = _text(item.get('category'), 40) or 'Inbox'
        try:
            shown_at = datetime.fromisoformat(item['saved_at']).timestamp()
        except (KeyError, TypeError, ValueError):
            shown_at = time.time() if now is None else now
        snap = {'key': key, 'canonical_url': canonical, 'url': url or canonical, 'title': title, 'title_zh': None,
                'source': source, 'category': category, 'board': board_of(source, category),
                'tags': extract_entities(title), 'excerpt': ''}
        with self._lock, self.db:
            self._expose(snap, ref_kind='inbox', ref_id=item['id'], subscription=None, personal=True,
                         channel_id=item.get('card_channel_id'), message_id=item.get('card_message_id'),
                         position=0, item_count=1, shown_at=shown_at)
        return key

    # ---- message → items ------------------------------------------------------------

    def _items(self, where, params):
        rows = self.db.execute(f'''SELECT e.key, e.ref_kind, e.ref_id, e.position, e.item_count, e.personal,
            e.subscription, e.channel_id, e.message_id, i.title, i.title_zh, i.url, i.source, i.board, i.tags,
            f.verdict, f.coarse FROM exposures e JOIN items i USING(key) LEFT JOIN feedback f USING(key)
            WHERE {where} ORDER BY e.ref_kind, e.ref_id, e.position, e.id''', params)
        result = []
        for row in rows:
            entry = dict(row)
            entry['personal'] = bool(entry['personal'])
            entry['coarse'] = bool(entry['coarse']) if entry['verdict'] else None
            entry['tags'] = json.loads(entry['tags'])
            result.append(entry)
        return result

    def _ingest_run(self, run, subscriptions):
        if run is None or run.get('status') not in SENT_RUN_STATES:
            return False
        items = [item for item in run.get('payload') or () if isinstance(item, dict)]
        with self._lock, self.db:
            self._expose_run(run['id'], items, subscription=run['subscription'],
                             personal=_personal(run['subscription'], self._subscription_map(subscriptions)),
                             channel_id=run.get('channel_id'), message_id=run.get('message_id'),
                             shown_at=run.get('updated_at') or time.time())
        return True

    def items_for_ref(self, kind, ref_id, *, reader=None, subscriptions=()):
        """Items behind a button reference (`run` id / `watch` delivery id), in rendering order.

        A run not yet synced is read once through `reader` (a `DeliveryReader`). Runs that
        were never sent (or were manually skipped) have no items. `watch` exposures arrive
        with the watch store (T10); until then this returns [] for them.
        """
        if kind not in REF_KINDS:
            raise ValueError('未知的引用类型')
        with self._lock:
            items = self._items('e.ref_kind=? AND e.ref_id=?', (kind, str(ref_id)))
        if items or kind != 'run' or reader is None:
            return items
        if not self._ingest_run(reader.get_run(int(ref_id)), subscriptions):
            return []
        with self._lock:
            return self._items('e.ref_kind=? AND e.ref_id=?', (kind, str(ref_id)))

    def items_for_message(self, message_id, *, reader=None, subscriptions=(), inbox=None):
        """Items shown in one Discord message: a news run (synced, or read through `reader`)
        or an inbox card (through `inbox`, an `InboxStore`). Empty when unknown."""
        message_id = str(message_id)
        with self._lock:
            items = self._items('e.message_id=?', (message_id,))
        if items:
            return items
        if reader is not None and self._ingest_run(reader.run_by_message(message_id), subscriptions):
            with self._lock:
                return self._items('e.message_id=?', (message_id,))
        if inbox is not None and message_id.isdecimal():
            card = inbox.by_card(int(message_id))
            if card is not None:
                self.expose_inbox(card)
                with self._lock:
                    return self._items('e.message_id=?', (message_id,))
        return []

    # ---- statistics -----------------------------------------------------------------

    def feedback_since(self, since):
        """Effective verdicts updated since `since`, with the item features (input for profiles)."""
        with self._lock:
            rows = self.db.execute('''SELECT f.key, f.verdict, f.weight, f.coarse, f.via, f.updated_at,
                i.source, i.category, i.board, i.tags,
                EXISTS(SELECT 1 FROM exposures e WHERE e.key=f.key AND e.personal=1) AS personal
                FROM feedback f JOIN items i USING(key) WHERE f.updated_at >= ? ORDER BY f.updated_at''',
                (since,)).fetchall()
        return [{**dict(row), 'coarse': bool(row['coarse']), 'personal': bool(row['personal']),
                 'tags': json.loads(row['tags'])} for row in rows]

    def stats(self, since, *, by='source', personal=None):
        """Weighted counts since `since`, grouped by `source`, `board`, `tag` or nothing (`None`).

        Exposures count distinct items shown since max(since, tracking_since); verdict sums
        use each verdict's weight (exact 1, coarse 1/n). `personal` limits both sides to
        items shown personally (True) or in shared channels (False).
        """
        if by not in {'source', 'board', 'tag', None}:
            raise ValueError('未知的分组方式')
        start = max(float(since), self.tracking_since)
        flag = '' if personal is None else 'AND e.personal = ?'
        params = () if personal is None else (int(bool(personal)),)
        with self._lock:
            exposed = self.db.execute(f'''SELECT i.key, i.source, i.board, i.tags FROM items i WHERE EXISTS
                (SELECT 1 FROM exposures e WHERE e.key=i.key AND e.shown_at >= ? {flag})''',
                (start, *params)).fetchall()
            judged = self.db.execute(f'''SELECT f.verdict, f.weight, f.coarse, i.source, i.board, i.tags
                FROM feedback f JOIN items i USING(key) WHERE f.updated_at >= ? AND EXISTS
                (SELECT 1 FROM exposures e WHERE e.key=f.key {flag})''', (float(since), *params)).fetchall()

        def groups(row):
            if by is None:
                return []
            if by == 'tag':
                return json.loads(row['tags'])
            return [row[by]]

        def blank(name):
            return {'name': name, 'exposures': 0, 'rated': 0, 'exact': 0, 'new': 0.0, 'known': 0.0, 'skip': 0.0}

        totals, table = blank(None), {}
        for row in exposed:
            for bucket in [totals, *(table.setdefault(g, blank(g)) for g in groups(row))]:
                bucket['exposures'] += 1
        for row in judged:
            for bucket in [totals, *(table.setdefault(g, blank(g)) for g in groups(row))]:
                bucket['rated'] += 1
                bucket['exact'] += 0 if row['coarse'] else 1
                bucket[row['verdict']] += row['weight']
        for bucket in [totals, *table.values()]:
            decided = bucket['new'] + bucket['known']
            bucket['new_rate'] = bucket['new'] / decided if decided else None
        rows = sorted(table.values(), key=lambda b: (-b['exposures'], -b['rated'], str(b['name'])))
        return {'since': start, 'totals': totals, 'rows': rows}

    def engaged(self):
        """What the knowledge index pins: `(canonical_urls, inbox_ids)`.

        URLs of items that were shown (any exposure), have a verdict or were saved;
        inbox ids of 📥 saves, so a save whose item snapshot is missing still pins
        through the inbox entry's URL.
        """
        with self._lock:
            urls = {row['canonical_url'] for row in self.db.execute(
                '''SELECT canonical_url FROM items WHERE key IN
                   (SELECT key FROM exposures UNION SELECT key FROM feedback UNION SELECT key FROM saves)''')}
            inbox_ids = {row['inbox_id'] for row in self.db.execute('SELECT inbox_id FROM saves')}
        return urls, inbox_ids

    # ---- retention ------------------------------------------------------------------

    def cleanup(self, *, now=None):
        """Drop exposures/feedback older than 730 days, log rows older than 365 days, and
        items no longer referenced by an exposure, a verdict or a save."""
        now = time.time() if now is None else now
        with self._lock, self.db:
            counts = {
                'exposures': self.db.execute('DELETE FROM exposures WHERE shown_at < ?', (now - ITEM_TTL,)).rowcount,
                'feedback': self.db.execute('DELETE FROM feedback WHERE updated_at < ?', (now - ITEM_TTL,)).rowcount,
                'feedback_log': self.db.execute('DELETE FROM feedback_log WHERE at < ?', (now - LOG_TTL,)).rowcount,
            }
            counts['items'] = self.db.execute('''DELETE FROM items WHERE last_shown_at < ?
                AND key NOT IN (SELECT key FROM exposures) AND key NOT IN (SELECT key FROM feedback)
                AND key NOT IN (SELECT key FROM saves)''', (now - ITEM_TTL,)).rowcount
            self._set_meta('last_cleanup', repr(now))
        return counts
