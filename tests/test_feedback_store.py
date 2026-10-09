import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from core.feedback import boards
from core.feedback.boards import board_of, source_catalog
from core.feedback.entities import extract_entities
from core.feedback.store import DAY, FeedbackStore, item_key, snapshot
from core.inbox import InboxStore
from core.news import personal
from core.news.models import Subscription
from core.news.reader import DeliveryReader, PoolReader
from core.news.store import NewsStore

SUBS = [
    Subscription('general', 'general', ('general',), ('08:45',), 1),
    Subscription('following', 'following', ('following',), ('08:20',), 2),
]
NOW = 1_800_000_000.0


def payload_item(n, *, publisher='BBC World', category='World', title=None, zh='中文标题', tags=None, url=None):
    url = url or f'https://example.com/story-{n}?utm_source=rss'
    item = {'id': f'R{n:02}', 'title': zh, 'url': url, 'summary': '摘要', '_article_id': f'a{n}',
            '_version': f'v{n}', '_delivery_key': f'd{n}',
            '_evidence': {'url': url, 'title': title or f'Original story {n} about Ottawa transit',
                          'content': 'x' * 500, 'rss_summary': 'short', 'publisher': publisher,
                          'category': category}}
    if tags is not None:
        item['tags'] = tags
    return item


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.news_path = root / 'news.sqlite3'
        self.news = NewsStore(self.news_path)
        self.news.import_history([])
        self.addCleanup(self.news.close)
        self.reader = DeliveryReader(self.news_path)
        self.store = FeedbackStore(root / 'data' / 'feedback.sqlite3', now=0)
        self.addCleanup(self.store.close)

    def publish(self, subscription, items, *, message_id=900, at=NOW, period=None, status='delivered'):
        run_id = self.news.claim(subscription, period or f'p{at}', 1)
        self.news.intent(run_id, items)
        if status == 'uncertain':
            self.news.set_status(run_id, 'uncertain')
            return run_id
        with patch('core.news.store.time.time', return_value=at):
            self.news.complete(run_id, message_id, status=status)
        return run_id


class VerdictRuleTests(Fixture):
    def log(self):
        return [tuple(r) for r in self.store.db.execute('SELECT key, verdict, weight, via FROM feedback_log ORDER BY id')]

    def test_exact_overrides_coarse_but_coarse_never_overrides_exact(self):
        self.assertEqual(self.store.record('k', 'known', via='reaction', weight=0.25, coarse=True, message_id=1, emoji='👌'),
                         ('known', True))
        self.assertEqual(self.store.record('k', 'new', via='button'), ('new', True))
        self.assertEqual(self.store.record('k', 'skip', via='reaction', weight=0.5, coarse=True), ('new', False))
        row = self.store.current('k')
        self.assertEqual((row['verdict'], row['weight'], row['coarse'], row['via']), ('new', 1.0, 0, 'button'))
        self.assertEqual(self.log(), [('k', 'known', 0.25, 'reaction'), ('k', 'new', 1.0, 'button')])

    def test_latest_coarse_replaces_coarse(self):
        self.store.record('k', 'known', via='reaction', weight=0.5, coarse=True)
        self.assertEqual(self.store.record('k', 'skip', via='reaction', weight=0.2, coarse=True), ('skip', True))
        self.assertEqual(self.store.current('k')['weight'], 0.2)

    def test_same_button_undoes_and_other_button_changes_verdict(self):
        self.store.record('k', 'new', via='button', message_id=5, now=10)
        self.assertEqual(self.store.record('k', 'known', via='button', now=20), ('known', True))
        row = self.store.current('k')
        self.assertEqual((row['verdict'], row['created_at'], row['updated_at']), ('known', 10, 20))
        self.assertEqual(self.store.record('k', 'known', via='button', now=30), (None, True))
        self.assertIsNone(self.store.current('k'))
        self.assertEqual(self.log(), [('k', 'new', 1.0, 'button'), ('k', 'known', 1.0, 'button'),
                                      ('k', None, None, 'button')])

    def test_button_undoes_an_exact_reaction_verdict_but_repeat_reaction_is_noop(self):
        self.store.record('k', 'new', via='reaction', message_id=7, emoji='🆕')
        self.assertEqual(self.store.record('k', 'new', via='reaction', message_id=7, emoji='🆕'), ('new', False))
        self.assertEqual(self.store.record('k', 'new', via='button'), (None, True))
        self.assertEqual(len(self.log()), 2)

    def test_reaction_removal_only_undoes_its_own_verdict(self):
        self.store.record('k', 'new', via='reaction', message_id=7, emoji='🆕')
        self.assertEqual(self.store.undo('k', via='reaction', message_id=7, emoji='👌'), ('new', False))
        self.assertEqual(self.store.undo('k', via='reaction', message_id=7, emoji='🆕'), (None, True))
        self.store.record('k', 'skip', via='button')
        self.assertEqual(self.store.undo('k', via='reaction', message_id=7, emoji='🚫'), ('skip', False))
        self.assertEqual(self.store.undo('missing', via='button'), (None, False))
        self.assertEqual([r[1] for r in self.log()], ['new', None, 'skip'])

    def test_invalid_input_is_rejected(self):
        for kwargs in ({'verdict': 'save', 'via': 'button'}, {'verdict': 'new', 'via': 'slash'},
                       {'verdict': 'new', 'via': 'button', 'weight': 0.5},
                       {'verdict': 'new', 'via': 'reaction', 'weight': 0, 'coarse': True}):
            with self.assertRaises(ValueError):
                self.store.record('k', **kwargs)
        self.assertEqual(self.log(), [])

    def test_saves_and_verdict_lookup(self):
        self.store.record('a', 'new', via='button')
        self.store.record('b', 'skip', via='reaction', weight=0.5, coarse=True)
        self.store.record_save('a', 'inbox1')
        self.assertEqual(self.store.saved('a'), 'inbox1')
        self.assertEqual(self.store.verdicts(['a', 'b', 'c']),
                         {'a': {'verdict': 'new', 'coarse': False, 'via': 'button'},
                          'b': {'verdict': 'skip', 'coarse': True, 'via': 'reaction'}})

    def test_schema_and_version_gate(self):
        tables = {r[0] for r in self.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {'meta', 'items', 'exposures', 'feedback', 'feedback_log', 'saves'})
        path = Path(self.tmp.name) / 'data' / 'feedback.sqlite3'
        with self.store.db:
            self.store.db.execute("UPDATE meta SET value='9' WHERE key='schema_version'")
        with self.assertRaises(RuntimeError):
            FeedbackStore(path)


class SyncTests(Fixture):
    def exposures(self):
        return [dict(r) for r in self.store.db.execute('SELECT * FROM exposures ORDER BY id')]

    def test_sync_snapshots_items_and_marks_personal_by_subscription(self):
        shared = [payload_item(1), payload_item(2, publisher='CNBC', category='Finance', title='Markets wait as Fed holds rates')]
        mine = [payload_item(3, publisher='CBC Ottawa', category='Ottawa', tags=['OC Transpo', '轻轨', 'x', 'y'])]
        run_a = self.publish('general', shared, message_id=901)
        run_b = self.publish('following', mine, message_id=902, at=NOW + 1)
        self.publish('retired', [payload_item(4)], message_id=903, at=NOW + 2)
        self.assertEqual(self.store.sync_exposures(self.reader, SUBS), 4)
        rows = {r['key']: r for r in self.exposures()}
        first = rows[item_key('https://example.com/story-1')]
        self.assertEqual((first['ref_kind'], first['ref_id'], first['subscription'], first['personal'],
                          first['message_id'], first['position'], first['item_count'], first['shown_at']),
                         ('run', str(run_a), 'general', 0, '901', 0, 2, NOW))
        self.assertEqual(rows[item_key('https://example.com/story-2')]['position'], 1)
        self.assertEqual(rows[item_key('https://example.com/story-3')]['personal'], 1)
        self.assertEqual(rows[item_key('https://example.com/story-3')]['ref_id'], str(run_b))
        self.assertEqual(rows[item_key('https://example.com/story-4')]['personal'], 0)  # unknown → shared
        item = dict(self.store.db.execute('SELECT * FROM items WHERE key=?',
                                          (item_key('https://example.com/story-3'),)).fetchone())
        self.assertEqual(item['canonical_url'], 'https://example.com/story-3')
        self.assertEqual(item['url'], 'https://example.com/story-3?utm_source=rss')
        self.assertEqual((item['title'], item['title_zh'], item['source'], item['category'], item['board']),
                         ('Original story 3 about Ottawa transit', '中文标题', 'CBC Ottawa', 'Ottawa', 'ottawa'))
        self.assertEqual(json.loads(item['tags']), ['OC Transpo', '轻轨', 'x'])
        self.assertEqual(len(item['excerpt']), 300)
        finance = dict(self.store.db.execute('SELECT board, tags FROM items WHERE key=?',
                                             (item_key('https://example.com/story-2'),)).fetchone())
        self.assertEqual(finance['board'], 'invest')
        self.assertEqual(json.loads(finance['tags']), ['Fed'])  # deterministic entities, no model tags

    def test_cursor_is_incremental_and_idempotent(self):
        self.publish('general', [payload_item(1)], at=NOW)
        self.assertEqual(self.store.sync_exposures(self.reader, SUBS), 1)
        self.assertEqual(self.store.exposure_cursor, NOW)
        self.assertEqual(self.store.sync_exposures(self.reader, SUBS), 0)
        self.publish('following', [payload_item(2, category='AI-Tech')], at=NOW + 60, message_id=950)
        self.assertEqual(self.store.sync_exposures(self.reader, SUBS), 1)
        self.assertEqual(self.store.exposure_cursor, NOW + 60)
        self.assertEqual(len(self.exposures()), 2)

    def test_small_pages_never_skip_items_sharing_a_timestamp(self):
        self.publish('general', [payload_item(n) for n in range(1, 4)], at=NOW)
        self.publish('general', [payload_item(n) for n in range(4, 6)], at=NOW + 5, message_id=950)
        self.assertEqual(self.store.sync_exposures(self.reader, SUBS, page=1), 5)

    def test_same_url_across_subscriptions_is_one_item_with_two_exposures(self):
        self.publish('general', [payload_item(1)], at=NOW, message_id=901)
        self.publish('following', [payload_item(1, url='https://example.com/story-1#frag', tags=['渥太华'])],
                     at=NOW + 10, message_id=902)
        self.store.sync_exposures(self.reader, SUBS)
        items = self.store.db.execute('SELECT * FROM items').fetchall()
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]['shown_count'], items[0]['first_shown_at'], items[0]['last_shown_at']),
                         (2, NOW, NOW + 10))
        self.assertEqual(json.loads(items[0]['tags']), ['渥太华'])  # model tags replace entity fallback
        self.assertEqual(len(self.exposures()), 2)

    def test_skipped_and_legacy_deliveries_are_not_exposures(self):
        self.publish('general', [payload_item(1)], status='skipped')
        self.news.import_history([('general', 1, {**payload_item(2), 'delivered_at': NOW})])
        self.assertEqual(self.store.sync_exposures(self.reader, SUBS), 0)
        self.assertEqual(self.store.exposure_cursor, NOW)

    def test_payload_without_url_is_ignored(self):
        self.assertIsNone(snapshot({'title': 'no link'}))
        self.assertIsNone(snapshot('junk'))


class MessageMappingTests(Fixture):
    def test_synced_run_maps_message_to_ordered_items_with_state(self):
        self.publish('following', [payload_item(1, category='Sudbury'), payload_item(2)], message_id=901)
        self.store.sync_exposures(self.reader, SUBS)
        key = item_key('https://example.com/story-2')
        self.store.record(key, 'new', via='button')
        items = self.store.items_for_message(901)
        self.assertEqual([i['position'] for i in items], [0, 1])
        self.assertEqual([i['board'] for i in items], ['sudbury', 'general'])
        self.assertEqual((items[1]['verdict'], items[1]['coarse'], items[0]['verdict']), ('new', False, None))
        self.assertTrue(items[0]['personal'])
        self.assertEqual(self.store.items_for_ref('run', items[0]['ref_id']), items)

    def test_unsynced_run_is_read_through_the_reader(self):
        run_id = self.publish('following', [payload_item(1)], message_id=901)
        self.assertEqual(self.store.items_for_message(901), [])
        items = self.store.items_for_message(901, reader=self.reader, subscriptions=SUBS)
        self.assertEqual([(i['ref_id'], i['personal'], i['message_id']) for i in items], [(str(run_id), True, '901')])
        # A later sync of the same delivery adds nothing.
        self.assertEqual(self.store.sync_exposures(self.reader, SUBS), 0)

    def test_button_ref_on_uncertain_run_still_maps_but_unsent_runs_do_not(self):
        uncertain = self.publish('following', [payload_item(1), payload_item(2)], status='uncertain')
        items = self.store.items_for_ref('run', uncertain, reader=self.reader, subscriptions=SUBS)
        self.assertEqual([i['position'] for i in items], [0, 1])
        self.assertIsNone(items[0]['message_id'])
        skipped = self.publish('general', [payload_item(3)], status='skipped', period='other')
        self.assertEqual(self.store.items_for_ref('run', skipped, reader=self.reader), [])
        self.assertEqual(self.store.items_for_ref('run', 999, reader=self.reader), [])
        self.assertEqual(self.store.items_for_ref('watch', 1), [])
        with self.assertRaises(ValueError):
            self.store.items_for_ref('bogus', 1)

    def test_inbox_card_maps_to_one_exact_item(self):
        inbox = InboxStore(Path(self.tmp.name) / 'inbox')
        card, _ = inbox.save(url='https://example.com/story-7?utm_medium=x', title='OPG picks GE Hitachi', body='b',
                             now=datetime(2026, 10, 5, tzinfo=timezone.utc))
        inbox.set_card(card['id'], 55, 777)
        items = self.store.items_for_message(777, inbox=inbox)
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]['key'], items[0]['ref_kind'], items[0]['item_count'], items[0]['personal']),
                         (item_key('https://example.com/story-7'), 'inbox', 1, True))
        self.assertEqual(items[0]['tags'], ['OPG', 'GE Hitachi'])
        self.assertEqual(self.store.items_for_message(778, inbox=inbox), [])

    def test_inbox_card_keeps_richer_news_snapshot(self):
        self.publish('following', [payload_item(1, publisher='CBC Ottawa', category='Ottawa')])
        self.store.sync_exposures(self.reader, SUBS)
        key = self.store.expose_inbox({'id': 'abc', 'url': 'https://example.com/story-1', 'title': 't',
                                       'saved_at': '2026-10-05T09:00:00+00:00', 'card_message_id': 5})
        self.assertEqual(key, item_key('https://example.com/story-1'))
        row = self.store.db.execute('SELECT source, board, shown_count FROM items WHERE key=?', (key,)).fetchone()
        self.assertEqual(tuple(row), ('CBC Ottawa', 'ottawa', 2))


class StatsTests(Fixture):
    def test_stats_weight_verdicts_and_group(self):
        self.publish('following', [payload_item(1, publisher='CBC Ottawa', category='Ottawa', tags=['轻轨']),
                                   payload_item(2, publisher='CBC Ottawa', category='Ottawa', tags=['轻轨', '市政'])],
                     message_id=901, at=NOW)
        self.publish('general', [payload_item(n, publisher='CNBC', category='Finance') for n in range(3, 7)],
                     message_id=902, at=NOW + 1)
        self.store.sync_exposures(self.reader, SUBS)
        k = [item_key(f'https://example.com/story-{n}') for n in range(1, 7)]
        self.store.record(k[0], 'new', via='button', now=NOW + 10)
        self.store.record(k[1], 'known', via='button', now=NOW + 10)
        for key in k[2:]:
            self.store.record(key, 'new', via='reaction', weight=0.25, coarse=True, message_id=902, now=NOW + 10)
        self.store.record(k[5], 'skip', via='button', now=NOW + 10)

        by_source = {row['name']: row for row in self.store.stats(NOW - DAY)['rows']}
        cnbc = by_source['CNBC']
        self.assertEqual((cnbc['exposures'], cnbc['rated'], cnbc['exact']), (4, 4, 1))
        self.assertAlmostEqual(cnbc['new'], 0.75)
        self.assertEqual((cnbc['skip'], cnbc['new_rate']), (1.0, 1.0))
        ottawa = by_source['CBC Ottawa']
        self.assertEqual((ottawa['new'], ottawa['known'], ottawa['new_rate']), (1.0, 1.0, 0.5))
        totals = self.store.stats(NOW - DAY, by=None)
        self.assertEqual(totals['rows'], [])
        self.assertEqual((totals['totals']['exposures'], totals['totals']['rated']), (6, 6))
        boards_ = {row['name']: row['exposures'] for row in self.store.stats(NOW - DAY, by='board')['rows']}
        self.assertEqual(boards_, {'invest': 4, 'ottawa': 2})
        tags = {row['name']: row['exposures'] for row in self.store.stats(NOW - DAY, by='tag', personal=True)['rows']}
        self.assertEqual(tags, {'轻轨': 2, '市政': 1})
        shared = self.store.stats(NOW - DAY, personal=False)['totals']
        self.assertEqual((shared['exposures'], shared['rated']), (4, 4))
        self.assertEqual(self.store.stats(NOW + 100)['totals']['exposures'], 0)
        profile_rows = self.store.feedback_since(NOW)
        self.assertEqual(len(profile_rows), 6)
        self.assertTrue(profile_rows[0]['personal'])
        with self.assertRaises(ValueError):
            self.store.stats(0, by='colour')

    def test_exposures_before_tracking_started_are_not_a_denominator(self):
        late = FeedbackStore(Path(self.tmp.name) / 'late.sqlite3', now=NOW + 5)
        self.addCleanup(late.close)
        self.publish('general', [payload_item(1)], at=NOW)
        late.sync_exposures(self.reader, SUBS)
        self.assertEqual(late.stats(0)['totals']['exposures'], 0)
        self.assertEqual(late.stats(0)['since'], NOW + 5)


class CleanupTests(Fixture):
    def test_retention_rules(self):
        self.publish('general', [payload_item(1), payload_item(2)], at=NOW)
        self.store.sync_exposures(self.reader, SUBS)
        k1, k2 = item_key('https://example.com/story-1'), item_key('https://example.com/story-2')
        self.store.record(k1, 'new', via='button', now=NOW + 400 * DAY)
        self.store.record(k2, 'new', via='button', now=NOW)
        counts = self.store.cleanup(now=NOW + 731 * DAY)
        self.assertEqual(counts, {'exposures': 2, 'feedback': 1, 'feedback_log': 1, 'items': 1})
        self.assertIsNotNone(self.store.current(k1))
        self.assertEqual([r[0] for r in self.store.db.execute('SELECT key FROM items')], [k1])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM feedback_log').fetchone()[0], 1)


class BoardAndEntityTests(unittest.TestCase):
    def test_personal_sections_and_shared_categories_map_to_boards(self):
        self.assertEqual(set(boards.SECTION_BOARDS), set(personal.SECTIONS))
        expected = {'Ottawa': 'ottawa', 'Sudbury': 'sudbury', 'Investing': 'invest', 'AI-Tech': 'ai_tech',
                    'General': 'general', 'Finance': 'invest', 'Tech': 'ai_tech', 'AI': 'ai_tech',
                    'World': 'general', 'Canada': 'general', 'Science': 'general', 'Video': 'general',
                    'ai_tech': 'ai_tech', '': 'general', None: 'general'}
        for category, board in expected.items():
            self.assertEqual(board_of('any', category), board, category)

    def test_catalog_lists_shared_and_personal_sources(self):
        entries = [{'name': 'CBC Sudbury', 'category': 'Sudbury', 'url': 'https://x.example/rss'}]
        with patch('core.news.sources.personal_entries', return_value=(entries, [])):
            catalog = {row['name']: row for row in source_catalog()}
        self.assertEqual(catalog['CBC Sudbury'], {'name': 'CBC Sudbury', 'category': 'Sudbury',
                                                  'board': 'sudbury', 'personal': True})
        self.assertEqual((catalog['CNBC']['board'], catalog['CNBC']['personal']), ('invest', False))
        self.assertEqual(catalog['OpenAI']['board'], 'ai_tech')

    def test_entities(self):
        cases = {
            'Nvidia beats estimates as OpenAI and Bank of Canada react': ['OpenAI', 'Bank', 'Canada'],
            'OPG picks GE Hitachi for Darlington SMR': ['OPG', 'GE Hitachi', 'Darlington SMR'],
            'US tariffs hit Ontario steel: Doug Ford responds': ['US', 'Ontario', 'Doug Ford'],
            'The New York Times reports on Sudbury mining': ['New York Times', 'Sudbury'],
            'How The Fed Will Decide Rates In A Very Long Headline': [],
            'OpenAI Releases New iPhone App For Ottawa Users': ['OpenAI', 'iPhone'],
            'Nvidia 发布《AI 白皮书》，“固态电池”量产': ['AI 白皮书', '固态电池', 'Nvidia'],
            '安大略「储能」招标与《电力法》修订': ['储能', '电力法'],
            '': [],
        }
        for title, expected in cases.items():
            self.assertEqual(extract_entities(title), expected, title)
        self.assertEqual(len(extract_entities('Alpha met Bravo near Charlie and Delta in Echo')), 3)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_missing_database_reads_empty_and_is_not_created(self):
        path = Path(self.tmp.name) / 'absent' / 'news.sqlite3'
        self.assertEqual(DeliveryReader(path).deliveries_since(0), [])
        self.assertIsNone(DeliveryReader(path).get_run(1))
        self.assertIsNone(DeliveryReader(path).run_by_message(1))
        self.assertEqual(DeliveryReader(path).run_items(1), [])
        self.assertEqual(PoolReader(path).since(0), [])
        self.assertEqual(PoolReader(path).window(DAY, now=NOW), [])
        self.assertFalse(path.parent.exists())

    def test_uninitialised_database_reads_empty(self):
        path = Path(self.tmp.name) / 'empty.sqlite3'
        sqlite3.connect(path).close()
        self.assertEqual(DeliveryReader(path).deliveries_since(0), [])

    def test_connection_is_read_only(self):
        path = Path(self.tmp.name) / 'news.sqlite3'
        NewsStore(path).close()
        with self.assertRaises(sqlite3.OperationalError):
            DeliveryReader(path)._query("INSERT INTO meta VALUES ('x', 'y')")

    def test_pool_reader_since_and_window(self):
        from core.news.models import Article
        path = Path(self.tmp.name) / 'news.sqlite3'
        news = NewsStore(path)
        self.addCleanup(news.close)
        now = time.time()
        old = Article('a1', 'BBC World', 'https://e.com/1', 't1', 'c', 'World', now - 100, now - 100, 'v')
        new = Article('a2', 'CNBC', 'https://e.com/2', 't2', 'c', 'Finance', now - 10, now - 10, 'v')
        news.upsert_articles([old], now=now - 100)
        news.upsert_articles([new], now=now - 10)
        reader = PoolReader(path)
        self.assertEqual([a.id for a in reader.since(0)], ['a1', 'a2'])
        self.assertEqual([a.id for a in reader.since(now - 50)], ['a2'])
        self.assertEqual([a.id for a in reader.window(3600, now=now)], ['a2', 'a1'])
        self.assertEqual([a.id for a in reader.window(3600, now=now, sources={'CNBC'})], ['a2'])


if __name__ == '__main__':
    unittest.main()
