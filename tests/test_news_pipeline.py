import asyncio
import datetime
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from core.ai_providers import AIResult
from core.feeds import FeedItem
from core.jobs import RetryPolicy
from core.news.ingest import Ingester, normalize
from core.news.migration import legacy_records
from core.news.models import Subscription
from core.news.pipeline import NewsPipeline
from core.news.sources import SHARED_GROUPS, sources_for
from core.news.store import NewsStore
from core.news.subscriptions import (
    DEFAULTS,
    load_subscriptions,
    parse_subscription,
    period_for,
)
from core.news.topics import TOPICS
from core.news.topics.general import GeneralTopic

ONCE = RetryPolicy(attempts=1)


def article(index=1, *, content=None, url=None, publisher='BBC World', published=None):
    return normalize(FeedItem('World', publisher, f'Original story {index}', url or f'https://example.com/{index}',
                     content or f'A concrete observation {index}, with a comparison.',
                     time.time() if published is None else published), time.time())


def subscription(identity='general', topic='general', **kwargs):
    return Subscription(identity, topic, ('general', 'discovery'), ('08:00',), 123, **kwargs)


async def generate(raw, **kwargs):
    data = json.loads(raw)
    candidates = data['candidates']
    identity = candidates[0]['id']
    item = {'id': identity, 'title': '有依据的中文标题', 'summary': '新进展：原文提供了新的具体观察。'}
    if identity.startswith('R'):
        item['why_read'] = '可以想一想：不同条件是否改变结论？'
    return AIResult(json.dumps({'items': [item]}, ensure_ascii=False), 'Test', 'model')


class PipelineFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'news.sqlite3'
        self.store = NewsStore(self.path)
        self.store.import_history([])
        self.pipeline = NewsPipeline(self.store)
        self.pipeline.ingester.collect = AsyncMock()
        self.channel = SimpleNamespace(id=123, send=AsyncMock(return_value=SimpleNamespace(id=456)))
        self.model_patch = patch('core.news.pipeline.ai_client.generate_ai', AsyncMock(side_effect=generate))
        self.model = self.model_patch.start()

    def tearDown(self):
        self.model_patch.stop()
        self.store.close()
        self.tmp.cleanup()

    async def publish(self, sub=None, period='2026-08-01/08:00'):
        return await self.pipeline.publish(sub or subscription(), period, self.channel, retry_policy=ONCE)

    def reopen(self):
        self.store.close()
        self.store = NewsStore(self.path)
        self.pipeline = NewsPipeline(self.store)
        self.pipeline.ingester.collect = AsyncMock()


class NewsPipelineTests(PipelineFixture):
    async def test_same_raw_material_is_available_to_two_independent_topics(self):
        self.store.upsert_articles([article()])
        self.assertIsNotNone(await self.publish())
        self.assertIsNotNone(await self.publish(subscription('discovery', 'discovery')))
        self.assertEqual(self.channel.send.await_count, 2)
        inputs = [json.loads(call.args[0]) for call in self.model.await_args_list]
        self.assertEqual(inputs[1]['candidates'][0]['content'], 'A concrete observation 1, with a comparison.')
        self.assertNotIn('summary', inputs[1]['candidates'][0])
        self.assertNotIn('url', inputs[0]['candidates'][0])
        self.assertNotIn('_evidence', inputs[1]['candidates'][0])
        self.assertEqual(len(self.store.history('general')), 1)
        self.assertEqual(len(self.store.history('discovery')), 1)

    async def test_subscriptions_do_not_share_delivery_history(self):
        self.store.upsert_articles([article()])
        await self.publish(subscription('one'))
        await self.publish(subscription('two'))
        self.assertEqual(self.channel.send.await_count, 2)
        await self.publish(subscription('one'), '2026-08-02/08:00')
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_same_link_new_evidence_reassessed_but_title_only_edit_not_sent(self):
        original = article()
        self.store.upsert_articles([original])
        await self.publish()
        changed = article(content='An approval has now added a new concrete result.')
        self.store.upsert_articles([changed])
        await self.publish(period='2026-08-02/08:00')
        self.assertEqual(self.channel.send.await_count, 2)
        title_edit = replace(changed, title='A rewritten headline', version='changed-title')
        self.store.upsert_articles([title_edit])
        await self.publish(period='2026-08-03/08:00')
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_success_and_period_dedup_survive_restart_and_channel_change(self):
        self.store.upsert_articles([article()])
        await self.publish()
        self.reopen()
        self.channel.id = 999
        await self.publish()
        await self.publish(period='2026-08-02/08:00')
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.store.status('general')['latest']['status'], 'empty')

    async def test_timeout_is_uncertain_and_blocks_later_periods_across_restart(self):
        self.store.upsert_articles([article()])
        self.channel.send.side_effect = asyncio.TimeoutError()
        with self.assertRaises(asyncio.TimeoutError):
            await self.publish()
        self.assertEqual(self.store.status('general')['latest']['status'], 'uncertain')
        self.assertEqual(self.store.history('general'), [])
        self.reopen()
        await self.publish(period='2026-08-02/08:00')
        self.channel.send.assert_awaited_once()
        pending = self.store.status('general')['pending']
        self.store.complete(pending, None, status='skipped')
        await self.publish(period='2026-08-03/08:00')
        self.channel.send.assert_awaited_once()

    async def test_post_send_write_failure_does_not_resend_and_can_be_confirmed(self):
        self.store.upsert_articles([article()])
        with patch.object(self.store, 'complete', side_effect=RuntimeError('disk error')):
            with self.assertRaisesRegex(RuntimeError, 'disk error'):
                await self.publish()
        self.channel.send.assert_awaited_once()
        self.reopen()
        pending = self.store.status('general')['pending']
        self.store.complete(pending, 456)
        await self.publish(period='2026-08-02/08:00')
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.store.get_run(pending)['message_id'], '456')

    async def test_crash_after_send_intent_and_before_send_is_also_uncertain(self):
        self.store.upsert_articles([article()])
        edition = await self.pipeline.build(subscription(), '2026-08-01/08:00')
        run = self.store.claim('general', '2026-08-01/08:00', 123)
        self.store.intent(run, edition.selected)
        self.reopen()
        self.assertEqual(self.store.get_run(run)['status'], 'uncertain')
        await self.publish(period='2026-08-02/08:00')
        self.channel.send.assert_not_awaited()

    async def test_generation_retry_sends_once_and_reserves_each_attempt(self):
        self.store.upsert_articles([article()])
        response = await generate(json.dumps({'candidates': [{'id': 'N01'}]}))
        self.model.side_effect = [RuntimeError('temporary'), response]
        await self.pipeline.publish(subscription(), '2026-08-01/08:00', self.channel,
                                    retry_policy=RetryPolicy(attempts=2, initial_delay_seconds=0))
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.model.await_count, 2)

    async def test_preview_uses_no_formal_run_or_history_and_does_not_require_migration(self):
        with self.store.db:
            self.store.db.execute("DELETE FROM meta WHERE key='history_imported'")
        self.store.upsert_articles([article()])
        result = await self.pipeline.preview(subscription(), '2026-08-01/08:00')
        self.assertIsNotNone(result)
        self.assertEqual(self.store.history('general'), [])
        self.assertIsNone(self.store.status('general')['latest'])
        self.channel.send.assert_not_awaited()
        with self.assertRaisesRegex(RuntimeError, '历史'):
            await self.publish()

    async def test_empty_candidates_and_empty_selections_never_send(self):
        await self.publish()
        self.model.assert_not_awaited()
        self.store.upsert_articles([article()])
        self.model.side_effect = None
        self.model.return_value = AIResult('{"items":[]}', 'Test', 'model')
        await self.publish(period='2026-08-02/08:00')
        self.channel.send.assert_not_awaited()

    async def test_failed_topic_does_not_block_other_topic_or_clear_history(self):
        class Broken(GeneralTopic):
            name = 'broken'

            def prepare(self, articles, subscription, history, edition):
                raise ValueError('bad topic')

        self.store.upsert_articles([article()])
        self.pipeline.topics = {**TOPICS, 'broken': Broken()}
        now = datetime.datetime(2026, 8, 1, 8, 0, tzinfo=datetime.UTC)
        await self.pipeline.publish_due([subscription('bad', 'broken'), subscription()], now, lambda _: self.channel)
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.store.status('bad')['latest']['status'], 'failed')

    async def test_overlapping_trigger_is_skipped(self):
        self.store.upsert_articles([article()])
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(raw, **kwargs):
            started.set()
            await release.wait()
            return await generate(raw, **kwargs)

        self.model.side_effect = slow
        task = asyncio.create_task(self.publish())
        await started.wait()
        await self.publish(period='2026-08-02/08:00')
        release.set()
        await task
        self.channel.send.assert_awaited_once()

    async def test_budget_is_shared_with_previews_and_persistent(self):
        self.store.upsert_articles([article()])
        self.pipeline.max_calls = 1
        await self.pipeline.preview(subscription(), '2026-08-01/08:00')
        self.store.upsert_articles([article(content='Changed evidence')])
        self.reopen()
        self.pipeline.max_calls = 1
        with self.assertRaisesRegex(RuntimeError, '预算'):
            await self.pipeline.preview(subscription(), '2026-08-02/08:00')
        self.model.assert_awaited_once()

    async def test_rule_upgrade_does_not_clear_delivery_identities(self):
        self.store.upsert_articles([article()])
        await self.publish()
        upgraded = GeneralTopic()
        upgraded.version = '2'
        self.pipeline.topics = {**TOPICS, 'general': upgraded}
        await self.publish(period='2026-08-02/08:00')
        self.channel.send.assert_awaited_once()
        self.assertEqual(len(self.store.history('general')), 1)

    async def test_cached_selection_is_revalidated_without_new_model_call(self):
        self.store.upsert_articles([article()])
        first = await self.pipeline.preview(subscription(), '2026-08-01/08:00')
        second = await self.pipeline.preview(subscription(), '2026-08-01/08:00')
        self.assertEqual(first.selected, second.selected)
        self.model.assert_awaited_once()
        row = self.store.db.execute('SELECT result FROM processed_items').fetchone()
        self.assertIn('digest_title', json.loads(row['result']))

    async def test_collection_is_once_per_union_not_per_subscription_or_model_cap(self):
        ingester = Ingester(self.store)
        items = [FeedItem('World', 'BBC World', f'Story {i}', f'https://example.com/{i}', 'Raw', time.time()) for i in range(40)]
        with patch('core.news.ingest.fetch_feeds', AsyncMock(return_value=items)) as fetch:
            await asyncio.gather(ingester.collect(), ingester.collect())
            fetch.assert_awaited_once()
            assert fetch.await_args is not None
            self.assertEqual(fetch.await_args.kwargs['max_items_per_source'], 40)
            urls = [source.url for source in fetch.await_args.args[0]]
            self.assertEqual(len(urls), len(set(urls)))
            self.assertEqual(len(self.store.articles({'BBC World'}, age=86400)), 40)
        self.model.assert_not_awaited()

    async def test_raw_update_preserves_first_seen_and_does_not_merge_media(self):
        first = article(content='Raw evidence one')
        self.store.upsert_articles([first, article(2, content='Raw evidence one')])
        changed = article(content='Raw evidence two')
        self.store.upsert_articles([changed])
        rows = self.store.articles({'BBC World'}, age=86400)
        self.assertEqual(len(rows), 2)
        updated = next(a for a in rows if a.url == first.url)
        self.assertEqual(updated.first_seen, first.first_seen)
        self.assertNotEqual(updated.version, first.version)
        versions = self.store.db.execute('SELECT evidence FROM article_versions WHERE article_id=?', (first.id,)).fetchall()
        self.assertEqual({json.loads(row['evidence'])['content'] for row in versions}, {'Raw evidence one', 'Raw evidence two'})


class FeedbackButtonDeliveryTests(PipelineFixture):
    """§2.8: buttons only through `view_factory`; the shared send call stays unchanged."""

    async def test_without_factory_send_arguments_are_unchanged(self):
        self.store.upsert_articles([article()])
        edition = await self.publish()
        self.channel.send.assert_awaited_once_with(embeds=edition.embeds)

    async def test_factory_runs_after_intent_and_sends_once_with_view(self):
        self.store.upsert_articles([article(1), article(2)])
        seen = []

        def factory(run_id, count):
            seen.append((run_id, count, self.store.get_run(run_id)['status']))
            return 'VIEW'

        sub = subscription('following', 'following')
        edition = await self.pipeline.publish(sub, '2026-08-01/08:00', self.channel,
                                              retry_policy=ONCE, view_factory=factory)
        run_id = self.store.status('following')['latest']['id']
        self.assertEqual(seen, [(run_id, len(edition.selected), 'sending')])
        self.channel.send.assert_awaited_once_with(embeds=edition.embeds, view='VIEW')
        self.assertEqual(self.store.get_run(run_id)['status'], 'delivered')

    async def test_factory_failure_sends_once_without_buttons(self):
        self.store.upsert_articles([article()])

        def factory(run_id, count):
            raise RuntimeError('no buttons')

        with self.assertLogs('core.news.pipeline', 'WARNING'):
            edition = await self.pipeline.publish(subscription(), '2026-08-01/08:00', self.channel,
                                                  retry_policy=ONCE, view_factory=factory)
        self.channel.send.assert_awaited_once_with(embeds=edition.embeds)
        self.assertEqual(self.store.status('general')['latest']['status'], 'delivered')

    async def test_publish_due_asks_for_a_factory_per_subscription(self):
        self.store.upsert_articles([article()])
        asked = []

        def views(sub):
            asked.append(sub.id)
            return None

        now = datetime.datetime(2026, 8, 1, 8, 0, tzinfo=datetime.UTC)
        await self.pipeline.publish_due([subscription()], now, lambda _: self.channel, views=views)
        self.assertEqual(asked, ['general'])
        self.channel.send.assert_awaited_once_with(embeds=ANY)
        self.assertEqual(set(self.channel.send.await_args.kwargs), {'embeds'})

    async def test_following_numbers_items_in_payload_order_and_discovery_does_not(self):
        self.store.upsert_articles([article(1), article(2)])

        async def two(raw, **kwargs):
            ids = [c['id'] for c in json.loads(raw)['candidates']][:2]
            items = [{'id': i, 'title': f'中文标题{n}', 'summary': '原文提供了具体观察。',
                      'why_read': '可以想一想：条件是否改变结论？', 'tags': ['渥太华']}
                     for n, i in enumerate(ids)]
            return AIResult(json.dumps({'items': items}, ensure_ascii=False), 'Test', 'model')

        self.model.side_effect = two
        edition = await self.pipeline.preview(subscription('following', 'following'), '2026-08-01/08:00')
        body = edition.embeds[0].description
        self.assertLess(body.index('① **[中文标题0]'), body.index('② **[中文标题1]'))
        self.assertEqual([item['title'] for item in edition.selected], ['中文标题0', '中文标题1'])
        discovery = TOPICS['discovery'].render(edition.selected, '早间', 'Test')[0].description
        self.assertNotIn('①', discovery)
        self.assertFalse(TOPICS['discovery'].personal)
        self.assertTrue(TOPICS['following'].personal)


class MigrationAndConfigurationTests(unittest.TestCase):
    def test_migration_is_idempotent_and_keeps_subscription_boundaries(self):
        general = [{'url': 'https://example.com/1', 'title': 'Old', 'rss_summary': 'Old raw',
                    'delivered_at': time.time()}]
        discovery = [{'url': 'https://example.com/2', 'title': 'Old', 'summary': 'AI summary', 'pushed': True},
                     {'url': 'https://example.com/3', 'title': 'Unsent'}]
        records = legacy_records(general, discovery)
        with tempfile.TemporaryDirectory() as directory:
            store = NewsStore(Path(directory) / 'news.sqlite3')
            try:
                self.assertFalse(store.ready)
                store.import_history(records)
                store.import_history(records)
                self.assertTrue(store.ready)
                self.assertEqual(len(store.history('general')), 1)
                self.assertEqual(len(store.history('discovery')), 1)
                item = {'url': 'https://example.com/2', '_version': 'new'}
                self.assertTrue(store.seen('discovery', item))
                self.assertFalse(store.seen('general', item))
                self.assertEqual(store.history('discovery')[0]['content'], '')
                self.assertNotIn('AI summary', json.dumps(store.history('discovery')))
            finally:
                store.close()

    def test_malformed_history_fails_closed(self):
        for raw in [None, {}, [1], [{'url': 'javascript:bad'}]]:
            with self.assertRaises(ValueError):
                legacy_records(raw, [])

    def test_default_schedules_sources_and_channels_are_preserved(self):
        channels = {'NEWS_CHANNEL_ID': 123, 'TEST_NEWS_CHANNEL_ID': 456}
        subs = [parse_subscription(raw, TOPICS, channels) for raw in DEFAULTS]
        self.assertEqual(subs[0].times, ('08:45', '15:30'))
        self.assertEqual(subs[1].times, ('08:00', '18:00'))
        self.assertEqual([s.channel_id for s in subs], [123, 456, 456, None])
        self.assertEqual((subs[3].topic, subs[3].source_groups), ('following', ('following',)))
        self.assertEqual(subs[2].params, {'countries': ['US', 'CA']})
        self.assertEqual(len(sources_for(('general',))), 8)
        self.assertEqual(len(SHARED_GROUPS['discovery']), 9)
        shared = {source.name for group in ('general', 'discovery') for source in SHARED_GROUPS[group]}
        self.assertFalse(shared & {'B站关注', '华尔街见闻'})

    def test_private_rsshub_feeds_exist_only_when_the_runtime_names_them(self):
        from core.news import personal
        env = {'RSSHUB_URL': 'https://rss.example/', 'RSSHUB_ACCESS_KEY': 'k', 'BILIBILI_UID': '42'}
        telegram_entry = {'name': '华尔街见闻', 'category': 'Investing', 'rsshub': '/telegram/channel/cnwallstreet'}
        bilibili_entry = {'name': 'B站关注', 'category': 'General', 'rsshub': '/bilibili/followings/video/{uid}'}
        with patch.object(personal, 'get_env', env.get):
            telegram = personal.feed_source(telegram_entry)
            bilibili = personal.feed_source(bilibili_entry)
        self.assertEqual(telegram.url, 'https://rss.example/telegram/channel/cnwallstreet?key=k')
        self.assertEqual(bilibili.url, 'https://rss.example/bilibili/followings/video/42?key=k')
        self.assertEqual((telegram.category, bilibili.name), ('Investing', 'B站关注'))
        for missing in ('RSSHUB_URL', 'RSSHUB_ACCESS_KEY', 'BILIBILI_UID'):
            partial = {key: value for key, value in env.items() if key != missing}
            with patch.object(personal, 'get_env', partial.get):
                self.assertIsNone(personal.feed_source(bilibili_entry))

    def test_bad_subscription_does_not_disable_valid_siblings(self):
        with patch('core.news.subscriptions.settings.load_settings', return_value={'NEWS_SUBSCRIPTIONS': [DEFAULTS[0], {'id': 'bad'}]}):
            subs, errors = load_subscriptions(TOPICS)
        self.assertEqual(len(subs), 1)
        self.assertEqual(len(errors), 1)
        with patch('core.news.subscriptions.settings.load_settings', return_value={'NEWS_SUBSCRIPTIONS': [DEFAULTS[0], DEFAULTS[0]]}):
            subs, errors = load_subscriptions(TOPICS)
        self.assertEqual(subs, [])
        self.assertTrue(errors)

    def test_manual_and_scheduled_keys_are_identical_and_dst_fold_is_not_a_new_period(self):
        sub = subscription()
        now = datetime.datetime(2026, 8, 1, 8, 0, tzinfo=datetime.UTC)
        self.assertEqual(period_for(sub, now), period_for(sub, now, scheduled=True))
        self.assertEqual(period_for(sub, now.replace(fold=1)), period_for(sub, now))
        self.assertIsNone(period_for(sub, now.replace(hour=9), scheduled=True))
        self.assertEqual(period_for(sub, now.replace(hour=7)), '2026-07-31/08:00')
