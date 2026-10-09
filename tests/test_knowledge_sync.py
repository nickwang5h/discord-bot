import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from core.feedback.store import FeedbackStore, item_key
from core.inbox import DONE, DROPPED, PENDING, InboxStore
from core.knowledge import sync
from core.knowledge.store import DAY, KnowledgeStore
from core.news.models import Article, Subscription
from core.news.reader import DeliveryReader, PoolReader
from core.news.store import NewsStore

NOW = 1_800_000_000.0
SAVED = datetime.fromtimestamp(NOW, timezone.utc)
SUBS = [Subscription('following', 'following', ('following',), ('08:20',), 2)]


def article(n, *, first_seen, source='BBC World', category='World', url=None, content=None):
    return Article(f'a{n}', source, url or f'https://example.com/story-{n}?utm_source=rss', f'Story {n}',
                   content if content is not None else f'raw rss content {n}', category, first_seen - 60,
                   first_seen, 'v1')


def payload_item(n, url):
    return {'id': f'R{n}', 'title': '中文', 'url': url, '_article_id': f'a{n}', '_version': 'v1', '_delivery_key': f'd{n}',
            '_evidence': {'url': url, 'title': f'Story {n}', 'content': 'c', 'publisher': 'BBC World',
                          'category': 'World'}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.news_path = root / 'news.sqlite3'
        self.news = NewsStore(self.news_path)
        self.news.import_history([])
        self.addCleanup(self.news.close)
        self.pool = PoolReader(self.news_path)
        self.knowledge = KnowledgeStore(root / 'data' / 'knowledge.sqlite3')
        self.addCleanup(self.knowledge.close)
        self.inbox = InboxStore(root / 'inbox')
        self.feedback = FeedbackStore(root / 'data' / 'feedback.sqlite3', now=0)
        self.addCleanup(self.feedback.close)

    def add_articles(self, *articles):
        self.news.upsert_articles(list(articles), now=NOW)

    def news_ids(self):
        return sorted(doc['id'] for doc in self.knowledge.recent(kind='news', limit=1000))


class NewsSyncTests(Fixture):
    def test_incremental_cursor_and_idempotent_replay(self):
        self.add_articles(article(1, first_seen=NOW - 300), article(2, first_seen=NOW - 300))
        first = sync.sync_news(self.knowledge, self.pool, now=NOW)
        self.assertEqual((first['read'], first['changed'], first['cursor']), (2, 2, NOW - 300))
        self.assertEqual(self.knowledge.get_meta('news_cursor'), repr(NOW - 300))
        # The cursor timestamp is re-read (a batch may land in parts); rewriting is a no-op.
        again = sync.sync_news(self.knowledge, self.pool, now=NOW)
        self.assertEqual((again['read'], again['changed']), (2, 0))
        # A late part of the same batch (same first_seen) and a newer batch both arrive.
        self.add_articles(article(3, first_seen=NOW - 300), article(4, first_seen=NOW - 100))
        third = sync.sync_news(self.knowledge, self.pool, now=NOW)
        self.assertEqual((third['changed'], third['cursor']), (2, NOW - 100))
        self.assertEqual(self.news_ids(), ['news:a1', 'news:a2', 'news:a3', 'news:a4'])

    def test_pages_never_lose_rows_of_a_split_timestamp(self):
        batch = [article(n, first_seen=NOW - 200) for n in range(5)]
        later = [article(n, first_seen=NOW - 100) for n in range(5, 8)]
        self.add_articles(*batch, *later)
        result = sync.sync_news(self.knowledge, self.pool, now=NOW, page=2)
        self.assertEqual(result['cursor'], NOW - 100)
        self.assertEqual(len(self.news_ids()), 8)
        self.assertEqual(self.knowledge.stats()['news'], 8)

    def test_page_budget_continues_on_the_next_call(self):
        self.add_articles(*(article(n, first_seen=NOW - 100 + n) for n in range(6)))
        first = sync.sync_news(self.knowledge, self.pool, now=NOW, page=2, max_pages=1)
        self.assertEqual(first['read'], 2)
        self.assertEqual(len(self.news_ids()), 2)
        sync.sync_news(self.knowledge, self.pool, now=NOW, page=2)
        self.assertEqual(len(self.news_ids()), 6)

    def test_document_fields_board_and_raw_rss_body(self):
        self.add_articles(
            article(1, first_seen=NOW, source='CNBC', category='Finance', content='raw <b>RSS</b> text'),
            article(2, first_seen=NOW, source='Ottawa Citizen', category='Ottawa'),
            article(3, first_seen=NOW, source='The Verge', category='AI-Tech'),
            article(4, first_seen=NOW, source='Nature', category='Science'))
        sync.sync_news(self.knowledge, self.pool, now=NOW)
        doc = self.knowledge.get('news:a1')
        self.assertEqual((doc['kind'], doc['title'], doc['body'], doc['source'], doc['category']),
                         ('news', 'Story 1', 'raw <b>RSS</b> text', 'CNBC', 'Finance'))
        self.assertEqual(doc['url'], 'https://example.com/story-1?utm_source=rss')
        self.assertEqual(doc['canonical_url'], 'https://example.com/story-1')
        self.assertEqual((doc['added_at'], doc['published_at'], doc['version']), (NOW, NOW - 60, 'v1'))
        boards = {i: self.knowledge.get(f'news:a{i}')['board'] for i in range(1, 5)}
        self.assertEqual(boards, {1: 'invest', 2: 'ottawa', 3: 'ai_tech', 4: 'general'})

    def test_empty_or_missing_pool(self):
        missing = PoolReader(Path(self.tmp.name) / 'absent.sqlite3')
        self.assertEqual(sync.sync_news(self.knowledge, missing, now=NOW), {'read': 0, 'changed': 0, 'cursor': 0.0})


class InboxSyncTests(Fixture):
    def save(self, n, *, url=True, at=SAVED, **kwargs):
        item, _ = self.inbox.save(url=f'https://example.com/saved-{n}?utm_medium=x' if url else None,
                                  title=f'收藏 {n}', body=f'正文 {n}', summary=f'摘要 {n}', now=at, **kwargs)
        return item

    def test_entries_become_documents_without_the_header(self):
        item = self.save(1, note='关于储能', source='CBC Ottawa', origin='https://discord.com/channels/1/2/3')
        thought = self.save(2, url=False, origin='https://discord.com/channels/1/2/4')
        self.inbox.save(url='https://example.com/saved-1', title='x', body='', note='后来补的备注')
        result = sync.sync_inbox(self.knowledge, self.inbox, now=NOW)
        self.assertEqual((result['items'], result['changed'], result['unchanged']), (2, 2, False))
        doc = self.knowledge.get(f"inbox:{item['id']}")
        self.assertEqual((doc['kind'], doc['title'], doc['source'], doc['state']), ('inbox', '收藏 1', 'CBC Ottawa', PENDING))
        self.assertEqual((doc['url'], doc['canonical_url']), ('https://example.com/saved-1',) * 2)
        self.assertEqual((doc['added_at'], doc['state_at']), (NOW, NOW))
        self.assertNotIn('# 收藏 1', doc['body'])
        self.assertNotIn('来源：', doc['body'])
        self.assertNotIn('保存：', doc['body'])
        self.assertTrue(doc['body'].startswith('> 备注：关于储能\n> 备注：后来补的备注'))
        for part in ('## 摘要', '摘要 1', '## 正文', '正文 1'):
            self.assertIn(part, doc['body'])
        # Without a page URL the citation is the saved message's jump link.
        other = self.knowledge.get(f"inbox:{thought['id']}")
        self.assertEqual((other['url'], other['canonical_url']), ('https://discord.com/channels/1/2/4', ''))

    def test_unchanged_inbox_is_skipped_and_state_changes_sync(self):
        item = self.save(1)
        sync.sync_inbox(self.knowledge, self.inbox, now=NOW)
        self.assertTrue(sync.sync_inbox(self.knowledge, self.inbox, now=NOW)['unchanged'])
        done_at = SAVED + timedelta(days=2)
        self.inbox.set_state(item['id'], DONE, now=done_at)
        result = sync.sync_inbox(self.knowledge, self.inbox, now=NOW + 2 * DAY)
        self.assertEqual(result['changed'], 1)
        doc = self.knowledge.get(f"inbox:{item['id']}")
        self.assertEqual((doc['state'], doc['state_at'], doc['added_at']), (DONE, NOW + 2 * DAY, NOW))

    def test_appended_note_changes_the_fingerprint(self):
        item = self.save(1)
        sync.sync_inbox(self.knowledge, self.inbox, now=NOW)
        path = self.inbox.path(item)
        with path.open('a', encoding='utf-8') as file:
            file.write('\n> 备注：新备注\n')
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 10**9))
        self.assertEqual(sync.sync_inbox(self.knowledge, self.inbox, now=NOW)['changed'], 1)
        self.assertIn('新备注', self.knowledge.get(f"inbox:{item['id']}")['body'])

    def test_legacy_entry_without_state_at_uses_saved_at(self):
        item = self.save(1)
        self.inbox._index.update(lambda index: {**index, item['id']: {**index[item['id']], 'state': DONE}})
        sync.sync_inbox(self.knowledge, self.inbox, now=NOW)
        doc = self.knowledge.get(f"inbox:{item['id']}")
        self.assertEqual((doc['state'], doc['state_at']), (DONE, NOW))

    def test_dropped_items_leave_after_thirty_days_and_are_not_rewritten(self):
        item = self.save(1)
        self.inbox.set_state(item['id'], DROPPED, now=SAVED + timedelta(days=1))
        sync.sync_inbox(self.knowledge, self.inbox, now=NOW + DAY)
        doc_id = f"inbox:{item['id']}"
        self.assertEqual(self.knowledge.get(doc_id)['state'], DROPPED)
        self.knowledge.cleanup(now=NOW + 20 * DAY)
        self.assertIsNotNone(self.knowledge.get(doc_id))
        self.knowledge.cleanup(now=NOW + 32 * DAY)
        self.assertIsNone(self.knowledge.get(doc_id))
        # Another inbox change forces a full pass; the expired dropped item stays out.
        self.save(2, at=SAVED + timedelta(days=32))
        result = sync.sync_inbox(self.knowledge, self.inbox, now=NOW + 32 * DAY)
        self.assertEqual((result['skipped'], result['changed']), (1, 1))
        self.assertIsNone(self.knowledge.get(doc_id))
        # Saving it again reopens it, and it returns.
        self.inbox.save(url='https://example.com/saved-1', title='x', body='', now=SAVED + timedelta(days=33))
        sync.sync_inbox(self.knowledge, self.inbox, now=NOW + 33 * DAY)
        self.assertEqual(self.knowledge.get(doc_id)['state'], PENDING)

    def test_missing_file_is_skipped_and_keeps_the_existing_document(self):
        item = self.save(1)
        other = self.save(2)
        sync.sync_inbox(self.knowledge, self.inbox, now=NOW)
        self.inbox.path(item).unlink()
        self.inbox.set_state(other['id'], DONE)
        with self.assertLogs('core.knowledge.sync', 'WARNING'):
            result = sync.sync_inbox(self.knowledge, self.inbox, now=NOW)
        self.assertEqual((result['missing'], result['changed']), (1, 1))
        self.assertEqual(self.knowledge.get(f"inbox:{item['id']}")['body'].count('正文 1'), 1)


class PinTests(Fixture):
    def setUp(self):
        super().setUp()
        self.add_articles(*(article(n, first_seen=NOW) for n in range(1, 7)))
        sync.sync_news(self.knowledge, self.pool, now=NOW)
        self.delivery = DeliveryReader(self.news_path)

    def url(self, n):
        return f'https://example.com/story-{n}?utm_source=rss'

    def pinned(self):
        return {doc['id'] for doc in self.knowledge.recent(kind='news', limit=100) if doc['pinned']}

    def test_exposure_feedback_and_saves_pin_news(self):
        # Exposure: delivered (shown) items 1 and 2.
        run_id = self.news.claim('following', 'p1', 2)
        self.news.intent(run_id, [payload_item(1, self.url(1)), payload_item(2, self.url(2))])
        with patch('core.news.store.time.time', return_value=NOW):
            self.news.complete(run_id, 900)
        self.feedback.sync_exposures(self.delivery, SUBS)
        # Feedback whose exposure is gone (only the item snapshot and verdict remain).
        self.feedback.expose_inbox({'id': 'x3', 'url': self.url(3), 'title': 't', 'saved_at': SAVED.isoformat()})
        self.feedback.record(item_key(self.url(3)), 'new', via='button')
        with self.feedback.db:
            self.feedback.db.execute('DELETE FROM exposures WHERE key=?', (item_key(self.url(3)),))
        # 📥 save recorded in the feedback store; the inbox entry was dropped later.
        saved, _ = self.inbox.save(url=self.url(4), title='t4', body='b', now=SAVED)
        self.feedback.record_save('no-snapshot-key', saved['id'])
        self.inbox.set_state(saved['id'], DROPPED)
        # Same URL saved in the inbox (no feedback store trace).
        self.inbox.save(url=self.url(5), title='t5', body='b', now=SAVED)
        # A dropped inbox entry that was never 📥-saved does not pin.
        dropped, _ = self.inbox.save(url=self.url(6), title='t6', body='b', now=SAVED)
        self.inbox.set_state(dropped['id'], DROPPED)

        self.assertEqual(sync.sync_pins(self.knowledge, self.feedback, self.inbox), 5)
        self.assertEqual(self.pinned(), {'news:a1', 'news:a2', 'news:a3', 'news:a4', 'news:a5'})
        self.assertEqual(sync.sync_pins(self.knowledge, self.feedback, self.inbox), 0)

    def test_pin_set_is_exclusive_and_pinned_news_lives_365_days(self):
        self.inbox.save(url=self.url(1), title='t1', body='b', now=SAVED)
        other, _ = self.inbox.save(url=self.url(2), title='t2', body='b', now=SAVED)
        sync.sync_pins(self.knowledge, self.feedback, self.inbox)
        self.assertEqual(self.pinned(), {'news:a1', 'news:a2'})
        self.inbox.set_state(other['id'], DROPPED)
        self.assertEqual(sync.sync_pins(self.knowledge, self.feedback, self.inbox), 1)
        self.assertEqual(self.pinned(), {'news:a1'})
        self.knowledge.cleanup(now=NOW + 100 * DAY)
        self.assertEqual(self.news_ids(), ['news:a1'])
        self.knowledge.cleanup(now=NOW + 366 * DAY)
        self.assertEqual(self.news_ids(), [])


class SyncAllTests(Fixture):
    def test_all_steps_report_counts(self):
        self.add_articles(article(1, first_seen=NOW))
        self.inbox.save(url='https://example.com/story-1', title='t', body='b', now=SAVED)
        result = sync.sync_all(self.knowledge, pool_reader=self.pool, inbox_store=self.inbox,
                               feedback_store=self.feedback, now=NOW)
        self.assertEqual(result['errors'], {})
        self.assertEqual(result['news'], {'read': 1, 'changed': 1, 'cursor': NOW})
        self.assertEqual((result['inbox']['changed'], result['pins']), (1, 1))

    def test_one_failing_step_does_not_stop_the_others(self):
        self.add_articles(article(1, first_seen=NOW))
        self.inbox.save(url='https://example.com/story-1', title='t', body='b', now=SAVED)
        with patch.object(PoolReader, 'since', side_effect=RuntimeError('pool locked')), \
                self.assertLogs('core.knowledge.sync', 'ERROR'):
            result = sync.sync_all(self.knowledge, pool_reader=self.pool, inbox_store=self.inbox,
                                   feedback_store=self.feedback, now=NOW)
        self.assertIsNone(result['news'])
        self.assertIn('pool locked', result['errors']['news'])
        self.assertEqual(result['inbox']['changed'], 1)
        self.assertEqual(result['pins'], 0)  # the article never arrived, so nothing to pin yet
        with patch.object(self.inbox, 'items', side_effect=OSError('disk')), \
                self.assertLogs('core.knowledge.sync', 'ERROR'):
            result = sync.sync_all(self.knowledge, pool_reader=self.pool, inbox_store=self.inbox,
                                   feedback_store=self.feedback, now=NOW)
        self.assertEqual(set(result['errors']), {'inbox', 'pins'})
        self.assertEqual(result['news']['changed'], 1)

    def test_pins_need_both_stores(self):
        result = sync.sync_all(self.knowledge, inbox_store=self.inbox, now=NOW)
        self.assertEqual((result['news'], result['pins'], result['errors']), (None, None, {}))
        self.assertEqual(result['inbox']['items'], 0)


if __name__ == '__main__':
    unittest.main()
