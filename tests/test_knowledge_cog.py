import asyncio
import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from cogs import knowledge as knowledge_module
from cogs.knowledge import Knowledge
from core.inbox import InboxStore
from core.knowledge.recall import RecallError, RecallResult, RecallSource
from core.knowledge.store import KnowledgeStore

OWNER, STRANGER, CHANNEL = 1, 2, 555
TZ = knowledge_module.TZ
MONDAY = datetime.datetime(2026, 10, 12, 9, 0, tzinfo=TZ)


def interaction(user=OWNER):
    return SimpleNamespace(
        user=SimpleNamespace(id=user), guild_id=77,
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


class Fixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = {'INBOX_CHANNEL_ID': str(CHANNEL)}
        patches = [patch.object(knowledge_module, 'STATE_ROOT', self.root),
                   patch.object(knowledge_module, 'SCHEDULED_JOBS_ENABLED', False),
                   patch.object(knowledge_module, 'source_catalog', return_value=[]),
                   patch.object(knowledge_module.settings, 'get_setting',
                                side_effect=lambda key, default=None: self.settings.get(key, default))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.inbox = InboxStore(self.root / 'inbox')
        self.cogs = {'Inbox': SimpleNamespace(store=self.inbox)}
        self.channel = SimpleNamespace(id=CHANNEL, send=AsyncMock(return_value=SimpleNamespace(id=4242)))
        self.bot = MagicMock()
        self.bot.is_owner = AsyncMock(side_effect=lambda user: user.id == OWNER)
        self.bot.get_cog.side_effect = lambda name: self.cogs.get(name)
        self.bot.get_channel.side_effect = lambda cid: self.channel if cid == CHANNEL else None
        self.cog = Knowledge(self.bot)

    async def asyncTearDown(self):
        await self.cog.cog_unload()

    @property
    def db_path(self):
        return self.root / 'data' / 'knowledge.sqlite3'


class LifecycleTests(Fixture):
    async def test_store_is_lazy_and_closed_on_unload(self):
        self.assertFalse(self.db_path.exists())
        for loop in (self.cog.sync_loop, self.cog.maintenance, self.cog.weekly_report):
            self.assertFalse(loop.is_running())
        self.assertIn('尚未打开', self.cog.stats_line())
        self.assertFalse(self.db_path.exists())
        store = self.cog.store
        self.assertTrue(self.db_path.exists())
        self.assertIn('知识库: 0 篇', self.cog.stats_line())
        await self.cog.cog_unload()
        self.assertIsNone(self.cog._store)
        with self.assertRaises(Exception):
            store.stats()

    async def test_commands_are_owner_only_and_hidden_from_non_admins(self):
        for command in (self.cog.recall_command, self.cog.review):
            self.assertTrue(command.default_permissions.administrator, command.name)
        self.assertEqual({c.name for c in self.cog.get_app_commands()}, {'recall', 'review'})

    async def test_sync_uses_the_loaded_cogs_and_tolerates_missing_ones(self):
        feedback = SimpleNamespace(store=object())
        self.cogs['Feedback'] = feedback
        with patch.object(knowledge_module, 'sync_all', return_value={'errors': {}}) as sync_all:
            self.cog._sync_once(now=123.0)
        kwargs = sync_all.call_args.kwargs
        self.assertIs(kwargs['inbox_store'], self.inbox)
        self.assertIs(kwargs['feedback_store'], feedback.store)
        self.assertIs(kwargs['pool_reader'], self.cog.pool)
        self.cogs.clear()
        with patch.object(knowledge_module, 'sync_all', return_value={'errors': {'news': 'x'}}) as sync_all:
            self.cog._sync_once(now=124.0)
        self.assertIsNone(sync_all.call_args.kwargs['inbox_store'])
        self.assertIsNone(sync_all.call_args.kwargs['feedback_store'])
        self.assertEqual(self.cog.last_sync, {'at': 124.0, 'errors': {'news': 'x'}})

    async def test_real_sync_round_on_empty_sources(self):
        result = await asyncio.to_thread(self.cog._sync_once)
        self.assertEqual(result['errors'], {})
        self.assertEqual(result['news']['read'], 0)
        self.assertFalse((self.root / 'data' / 'news.sqlite3').exists())


def ok_result(*sources):
    return RecallResult('ok', '问题', '答案 [S1]', sources=tuple(sources), hits=3, provider='Claude',
                        model='claude-opus-5-5')


class RecallCommandTests(Fixture):
    async def test_non_owner_is_refused_without_touching_anything(self):
        it = interaction(STRANGER)
        with patch.object(knowledge_module, 'recall', AsyncMock()) as recall:
            await self.cog.recall_command.callback(self.cog, it, '问题')
        it.response.send_message.assert_awaited_once()
        self.assertTrue(it.response.send_message.call_args.kwargs['ephemeral'])
        it.response.defer.assert_not_awaited()
        recall.assert_not_awaited()
        self.assertFalse(self.db_path.exists())

    async def test_answer_is_private_counts_usage_and_links_inbox_notes(self):
        item, _ = self.inbox.save(url=None, title='一条想法', body='储能', origin='https://discord.com/channels/1/2/3')
        sources = (RecallSource(1, '一条想法', '', '收藏', '2026-10-01', 'inbox', f'inbox:{item["id"]}'),
                   RecallSource(2, 'News', 'https://example.com/a', 'BBC', None, 'news', 'news:a'))
        it = interaction()
        self.settings['RECALL_LIMITS'] = {'daily_calls': 5}
        with patch.object(knowledge_module, 'recall', AsyncMock(return_value=ok_result(*sources))) as recall:
            await self.cog.recall_command.callback(self.cog, it, '储能怎么样', None, 30)
        it.response.defer.assert_awaited_once_with(ephemeral=True)
        args, kwargs = recall.call_args
        self.assertEqual(args[0], '储能怎么样')
        self.assertIs(args[1], self.cog.store)
        self.assertEqual((kwargs['scope'], kwargs['days'], kwargs['limits']), (None, 30, {'daily_calls': 5}))
        sent = it.followup.send.call_args
        self.assertTrue(sent.kwargs['ephemeral'])
        embed = sent.kwargs['embed']
        self.assertIn('[一条想法](https://discord.com/channels/1/2/3)', embed.description)
        self.assertIn('[News](https://example.com/a)', embed.description)
        self.assertIn('Claude', embed.footer.text)
        self.assertLessEqual(len(embed.description), 4096)
        today = datetime.datetime.now(TZ).date().isoformat()
        self.assertEqual(self.cog.store.usage_count('recall', today, '9999-12-31'), 1)

    async def test_card_link_when_the_note_has_no_origin(self):
        item, _ = self.inbox.save(url=None, title='想法', body='x', origin='')
        self.inbox.set_card(item['id'], 10, 20)
        source = RecallSource(1, '想法', '', '收藏', None, 'inbox', f'inbox:{item["id"]}')
        linked = self.cog._with_inbox_links(ok_result(source), guild_id=77)
        self.assertEqual(linked.sources[0].url, 'https://discord.com/channels/77/10/20')

    async def test_scope_choice_is_passed_through(self):
        it = interaction()
        scope = SimpleNamespace(value='inbox')
        with patch.object(knowledge_module, 'recall', AsyncMock(return_value=ok_result())) as recall:
            await self.cog.recall_command.callback(self.cog, it, '问题', scope, 90)
        self.assertEqual(recall.call_args.kwargs['scope'], 'inbox')

    async def test_model_failure_sends_the_zero_model_fallback(self):
        fallback = RecallResult('answer_failed', '问题', '⚠️ 作答模型暂时不可用，以下是本地命中的前几条材料：',
                                sources=(RecallSource(1, 'Hit', 'https://example.com/h', 'BBC', None, 'news', 'news:h'),),
                                hits=1, degraded=True)
        it = interaction()
        with patch.object(knowledge_module, 'recall',
                          AsyncMock(side_effect=RecallError('作答超时，请稍后再试。', fallback=fallback))):
            await self.cog.recall_command.callback(self.cog, it, '问题')
        embed = it.followup.send.call_args.kwargs['embed']
        self.assertIn('作答超时', embed.description)
        self.assertIn('[Hit](https://example.com/h)', embed.description)
        self.assertIn('零模型兜底', embed.footer.text)
        self.assertTrue(it.followup.send.call_args.kwargs['ephemeral'])

    async def test_only_one_recall_runs_at_a_time(self):
        active = peak = 0

        async def slow(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return ok_result()

        with patch.object(knowledge_module, 'recall', side_effect=slow):
            await asyncio.gather(*(self.cog.recall_command.callback(self.cog, interaction(), f'q{i}')
                                   for i in range(3)))
        self.assertEqual(peak, 1)


class ReviewCommandTests(Fixture):
    async def test_review_is_private_and_never_claims_the_week(self):
        it = interaction()
        await self.cog.review.callback(self.cog, it, '2026-W41')
        it.response.defer.assert_awaited_once_with(ephemeral=True)
        embed = it.followup.send.call_args.kwargs['embed']
        self.assertIn('2026-W41', embed.title)
        self.assertIn('未投递', embed.footer.text)
        self.assertTrue(it.followup.send.call_args.kwargs['ephemeral'])
        self.assertIsNone(self.cog.store.get_report('2026-W41'))
        self.channel.send.assert_not_awaited()

    async def test_bad_week_and_strangers(self):
        it = interaction()
        await self.cog.review.callback(self.cog, it, 'last')
        self.assertIn('YYYY-Www', it.response.send_message.call_args.args[0])
        it = interaction(STRANGER)
        await self.cog.review.callback(self.cog, it, None)
        it.response.defer.assert_not_awaited()
        self.assertFalse(self.db_path.exists())


class WeeklyDeliveryTests(Fixture):
    async def test_delivered_once_to_the_inbox_channel(self):
        embed = await self.cog.deliver_weekly(MONDAY)
        self.assertIn('2026-W41', embed.title)
        self.channel.send.assert_awaited_once()
        report = self.cog.store.get_report('2026-W41')
        self.assertEqual((report['status'], report['message_id'], report['channel_id']),
                         ('delivered', '4242', str(CHANNEL)))
        self.assertIn('2026-W41', report['payload'])
        self.assertIsNone(await self.cog.deliver_weekly(MONDAY + datetime.timedelta(hours=1)))
        self.channel.send.assert_awaited_once()

    async def test_failed_send_is_uncertain_and_never_resent(self):
        self.channel.send.side_effect = RuntimeError('discord timeout')
        with self.assertRaises(RuntimeError):
            await self.cog.deliver_weekly(MONDAY)
        self.assertEqual(self.cog.store.get_report('2026-W41')['status'], 'uncertain')
        self.channel.send.side_effect = None
        self.assertIsNone(await self.cog.deliver_weekly(MONDAY))
        self.assertEqual(self.channel.send.await_count, 1)

    async def test_build_failure_is_final_without_sending(self):
        with (patch.object(knowledge_module, 'REPORT_RETRY', knowledge_module.RetryPolicy(attempts=1)),
              patch.object(Knowledge, 'build_review', side_effect=ValueError('boom'))):
            with self.assertRaises(ValueError):
                await self.cog.deliver_weekly(MONDAY)
        self.assertEqual(self.cog.store.get_report('2026-W41')['status'], 'failed')
        self.channel.send.assert_not_awaited()
        self.assertIsNone(await self.cog.deliver_weekly(MONDAY))
        self.channel.send.assert_not_awaited()

    async def test_restart_during_send_is_not_resent(self):
        store = self.cog.store
        self.assertTrue(store.claim_report('2026-W41', CHANNEL))
        store.report_intent('2026-W41', {'title': 't'})
        await self.cog.cog_unload()
        self.cog = Knowledge(self.bot)  # reopening marks the interrupted send uncertain
        self.assertEqual(self.cog.store.get_report('2026-W41')['status'], 'uncertain')
        self.assertIsNone(await self.cog.deliver_weekly(MONDAY))
        self.channel.send.assert_not_awaited()

    async def test_missing_channel_skips_without_claiming(self):
        self.settings.pop('INBOX_CHANNEL_ID')
        self.assertIsNone(await self.cog.deliver_weekly(MONDAY))
        self.settings['INBOX_CHANNEL_ID'] = '999'
        self.assertIsNone(await self.cog.deliver_weekly(MONDAY))
        self.channel.send.assert_not_awaited()
        self.assertIsNone(self.cog.store.get_report('2026-W41'))

    async def test_loop_runs_only_on_monday(self):
        with patch.object(Knowledge, 'deliver_weekly', AsyncMock()) as deliver:
            with patch.object(Knowledge, '_now', return_value=MONDAY - datetime.timedelta(days=1)):
                await self.cog.weekly_report.coro(self.cog)
            deliver.assert_not_awaited()
            with patch.object(Knowledge, '_now', return_value=MONDAY):
                await self.cog.weekly_report.coro(self.cog)
            deliver.assert_awaited_once_with(MONDAY)

    async def test_loop_swallows_errors(self):
        with (patch.object(Knowledge, '_now', return_value=MONDAY),
              patch.object(Knowledge, 'deliver_weekly', AsyncMock(side_effect=RuntimeError('x')))):
            await self.cog.weekly_report.coro(self.cog)


if __name__ == '__main__':
    unittest.main()
