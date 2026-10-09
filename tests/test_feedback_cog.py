import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from cogs import feedback as feedback_module
from cogs.feedback import TEMPLATE, Feedback, FeedbackButton, view_for
from core.feedback.store import DAY, item_key
from core.inbox import InboxStore
from core.news.models import Subscription
from core.news.store import NewsStore

OWNER, STRANGER, BOT_ID = 1, 2, 99
SUBS = [Subscription('following', 'following', ('following',), ('08:20',), 2),
        Subscription('general', 'general', ('general',), ('08:45',), 1)]
NOW = 1_800_000_000.0


def payload_item(n, publisher='CBC Ottawa', category='Ottawa'):
    url = f'https://example.com/story-{n}'
    return {'id': f'R{n}', 'title': f'中文标题{n}', 'url': url, 'summary': '摘要', '_article_id': f'a{n}',
            '_version': f'v{n}', '_delivery_key': f'd{n}',
            '_evidence': {'url': url, 'title': f'Original story {n}', 'content': 'x' * 50,
                          'publisher': publisher, 'category': category}}


def interaction(user=OWNER, message_id=900):
    return SimpleNamespace(
        user=SimpleNamespace(id=user),
        message=SimpleNamespace(id=message_id, jump_url='https://discord.com/channels/1/2/900'),
        channel=SimpleNamespace(id=2),
        response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def styles(view):
    """{custom_id: selected?} for every button in a view."""
    return {item.custom_id: item.item.style == discord.ButtonStyle.success for item in view.children}


def reaction(emoji, *, user=OWNER, message_id=900, author=BOT_ID):
    return SimpleNamespace(emoji=emoji, user_id=user, message_id=message_id, channel_id=2,
                           message_author_id=author)


class CustomIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_template_accepts_only_wellformed_ids(self):
        pattern = re.compile(TEMPLATE)
        for good in ('fb:n:12:0:new', 'fb:w:3:4:save', 'fb:n:0:9:skip'):
            self.assertTrue(pattern.match(good), good)
        for bad in ('fb:x:12:0:new', 'fb:n:-1:0:new', 'fb:n:12:10:new', 'fb:n:12:0:like',
                    'fb:n:12:0:newx', 'xfb:n:1:0:new', 'fb:n::0:new'):
            self.assertIsNone(pattern.match(bad), bad)

    async def test_from_custom_id_rebuilds_the_button_after_restart(self):
        match = re.compile(TEMPLATE).match('fb:n:12:3:known')
        button = await FeedbackButton.from_custom_id(None, None, match)
        self.assertEqual((button.kind, button.ref, button.index, button.value), ('n', 12, 3, 'known'))
        with self.assertRaises(ValueError):
            await FeedbackButton.from_custom_id(None, None, re.compile(TEMPLATE).match('fb:n:12:7:new'))

    async def test_view_has_one_row_of_four_per_item_capped_at_five(self):
        view = view_for('run', 42, 7)
        self.assertEqual(len(view.children), 20)
        self.assertIsNone(view.timeout)
        first = view.children[:4]
        self.assertEqual([b.item.label for b in first], ['①🆕', '①👌', '①🚫', '①📥'])
        self.assertEqual([b.custom_id for b in first], [f'fb:n:42:0:{v}' for v in ('new', 'known', 'skip', 'save')])
        self.assertEqual({b.row for b in view.children}, {0, 1, 2, 3, 4})
        self.assertFalse(any(styles(view).values()))
        self.assertEqual(view_for('w', 5, 1).children[0].custom_id, 'fb:w:5:0:new')
        self.assertIsNone(view_for('run', 1, 0))

    async def test_state_marks_selected_verdict_and_saved_item_green(self):
        view = view_for('n', 7, 2, {0: {'verdict': 'known', 'saved': False}, 1: {'verdict': None, 'saved': True}})
        selected = sorted(cid for cid, on in styles(view).items() if on)
        self.assertEqual(selected, ['fb:n:7:0:known', 'fb:n:7:1:save'])


class CogFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patches = [patch.object(feedback_module, 'STATE_ROOT', self.root),
                   patch.object(feedback_module, 'SCHEDULED_JOBS_ENABLED', False),
                   patch.object(feedback_module, 'load_subscriptions', return_value=(SUBS, []))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.news = NewsStore(self.root / 'data' / 'news.sqlite3')
        self.news.import_history([])
        self.addCleanup(self.news.close)
        self.inbox_cog = SimpleNamespace(store=InboxStore(self.root / 'inbox'),
                                         save_payload=AsyncMock(return_value={'id': 'inbox42'}))
        self.bot = MagicMock()
        self.bot.is_owner = AsyncMock(side_effect=lambda user: user.id == OWNER)
        self.bot.user = SimpleNamespace(id=BOT_ID)
        self.bot.get_cog.side_effect = lambda name: self.inbox_cog if name == 'Inbox' else None
        self.cog = Feedback(self.bot)

    async def asyncTearDown(self):
        await self.cog.cog_unload()

    def publish(self, items, *, message_id=900, subscription='following'):
        run_id = self.news.claim(subscription, f'p{message_id}', 1)
        self.news.intent(run_id, items)
        with patch('core.news.store.time.time', return_value=NOW):
            self.news.complete(run_id, message_id)
        return run_id

    def verdict(self, n):
        row = self.cog.store.current(item_key(f'https://example.com/story-{n}'))
        return row and (row['verdict'], row['weight'], bool(row['coarse']), row['via'])


class ButtonTests(CogFixture):
    async def test_strangers_get_a_private_refusal_and_nothing_is_written(self):
        run = self.publish([payload_item(1)])
        event = interaction(user=STRANGER)
        await self.cog.handle_button(event, 'n', run, 0, 'new')
        event.response.send_message.assert_awaited_once_with('仅所有者可用。', ephemeral=True)
        event.response.edit_message.assert_not_awaited()
        self.assertIsNone(self.cog._store)
        self.assertFalse((self.root / 'data' / 'feedback.sqlite3').exists())

    async def test_click_records_and_redraws_then_same_click_undoes(self):
        run = self.publish([payload_item(1), payload_item(2)])
        event = interaction()
        await self.cog.handle_button(event, 'n', run, 1, 'new')
        self.assertEqual(self.verdict(2), ('new', 1.0, False, 'button'))
        self.assertIsNone(self.verdict(1))
        view = event.response.edit_message.await_args.kwargs['view']
        self.assertEqual(len(view.children), 8)
        self.assertEqual([cid for cid, on in styles(view).items() if on], [f'fb:n:{run}:1:new'])
        self.assertEqual(self.cog.store.current(item_key('https://example.com/story-2'))['message_id'], '900')

        event = interaction()
        await self.cog.handle_button(event, 'n', run, 1, 'known')
        self.assertEqual(self.verdict(2)[0], 'known')
        self.assertEqual([cid for cid, on in styles(event.response.edit_message.await_args.kwargs['view']).items()
                          if on], [f'fb:n:{run}:1:known'])

        event = interaction()
        await self.cog.handle_button(event, 'n', run, 1, 'known')
        self.assertIsNone(self.verdict(2))
        self.assertFalse(any(styles(event.response.edit_message.await_args.kwargs['view']).values()))

    async def test_unknown_or_out_of_range_item_cannot_take_feedback(self):
        run = self.publish([payload_item(1)])
        for ref, index in ((run, 3), (run + 100, 0)):
            event = interaction()
            await self.cog.handle_button(event, 'n', ref, index, 'new')
            event.response.send_message.assert_awaited_once_with('这条已无法反馈。', ephemeral=True)
        self.assertEqual(self.cog.store.db.execute('SELECT COUNT(*) FROM feedback_log').fetchone()[0], 0)
        event = interaction()
        await self.cog.handle_button(event, 'w', 5, 0, 'new')
        event.response.send_message.assert_awaited_once_with('这条已无法反馈。', ephemeral=True)

    async def test_failed_message_edit_keeps_the_verdict(self):
        run = self.publish([payload_item(1)])
        event = interaction()
        event.response.edit_message.side_effect = discord.HTTPException(MagicMock(status=403), 'missing access')
        with self.assertLogs('cogs.feedback', 'WARNING'):
            await self.cog.handle_button(event, 'n', run, 0, 'skip')
        self.assertEqual(self.verdict(1)[0], 'skip')

    async def test_store_failure_is_reported_privately(self):
        run = self.publish([payload_item(1)])
        event = interaction()
        with patch.object(type(self.cog.store), 'record', side_effect=RuntimeError('disk')), \
                self.assertLogs('cogs.feedback', 'ERROR'):
            await self.cog.handle_button(event, 'n', run, 0, 'new')
        event.response.send_message.assert_awaited_once_with('反馈保存失败，请稍后再试。', ephemeral=True)

    async def test_save_button_saves_to_inbox_with_source_and_records_it(self):
        run = self.publish([payload_item(1), payload_item(2, publisher='Reuters', category='Investing')])
        event = interaction()
        await self.cog.handle_button(event, 'n', run, 1, 'save')
        event.response.defer.assert_awaited_once()
        self.inbox_cog.save_payload.assert_awaited_once()
        payload = self.inbox_cog.save_payload.await_args.args[0]
        kwargs = self.inbox_cog.save_payload.await_args.kwargs
        self.assertEqual((payload.url, payload.title), ('https://example.com/story-2', '中文标题2'))
        self.assertEqual((kwargs['source'], kwargs['via'], kwargs['fallback']), ('Reuters', 'button', event.channel))
        self.assertEqual(self.cog.store.saved(item_key('https://example.com/story-2')), 'inbox42')
        self.assertIsNone(self.verdict(2))
        view = event.edit_original_response.await_args.kwargs['view']
        self.assertEqual([cid for cid, on in styles(view).items() if on], [f'fb:n:{run}:1:save'])

    async def test_save_without_inbox_cog_is_refused(self):
        run = self.publish([payload_item(1)])
        self.bot.get_cog.side_effect = lambda name: None
        event = interaction()
        await self.cog.handle_button(event, 'n', run, 0, 'save')
        event.response.send_message.assert_awaited_once_with('收件箱未加载，无法保存。', ephemeral=True)

    async def test_button_callback_routes_to_the_loaded_cog(self):
        event = interaction()
        event.client = SimpleNamespace(get_cog=lambda name: None)
        await FeedbackButton('n', 1, 0, 'new').callback(event)
        event.response.send_message.assert_awaited_once_with('反馈功能未加载。', ephemeral=True)
        handler = SimpleNamespace(handle_button=AsyncMock())
        event.client = SimpleNamespace(get_cog=lambda name: handler if name == 'Feedback' else None)
        await FeedbackButton('n', 1, 2, 'skip').callback(event)
        handler.handle_button.assert_awaited_once_with(event, 'n', 1, 2, 'skip')


class ReactionTests(CogFixture):
    async def test_reaction_on_multi_item_message_is_coarse_and_removal_undoes(self):
        self.publish([payload_item(1), payload_item(2), payload_item(3), payload_item(4)])
        await self.cog.on_raw_reaction_add(reaction('🆕'))
        for n in (1, 2, 3, 4):
            self.assertEqual(self.verdict(n), ('new', 0.25, True, 'reaction'))
        await self.cog.on_raw_reaction_remove(reaction('👌'))     # a different emoji undoes nothing
        self.assertEqual(self.verdict(1)[0], 'new')
        await self.cog.on_raw_reaction_remove(reaction('🆕', author=None))
        self.assertTrue(all(self.verdict(n) is None for n in (1, 2, 3, 4)))

    async def test_reaction_on_single_item_message_is_exact_and_buttons_win(self):
        self.publish([payload_item(1)])
        await self.cog.on_raw_reaction_add(reaction('🚫'))
        self.assertEqual(self.verdict(1), ('skip', 1.0, False, 'reaction'))
        self.publish([payload_item(1), payload_item(2)], message_id=901)
        await self.cog.on_raw_reaction_add(reaction('👌', message_id=901))
        self.assertEqual(self.verdict(1), ('skip', 1.0, False, 'reaction'))   # coarse never beats exact
        self.assertEqual(self.verdict(2), ('known', 0.5, True, 'reaction'))

    async def test_reaction_on_inbox_card_is_exact(self):
        item, _ = self.inbox_cog.store.save(url='https://example.com/story-9', title='卡片', body='')
        self.inbox_cog.store.set_card(item['id'], 2, 555)
        await self.cog.on_raw_reaction_add(reaction('🆕', message_id=555))
        self.assertEqual(self.verdict(9), ('new', 1.0, False, 'reaction'))

    async def test_strangers_other_emoji_and_foreign_messages_are_ignored(self):
        self.publish([payload_item(1)])
        await self.cog.on_raw_reaction_add(reaction('🆕', user=STRANGER))
        await self.cog.on_raw_reaction_add(reaction('📥'))
        await self.cog.on_raw_reaction_add(reaction('🗑️'))
        self.assertIsNone(self.cog._store)
        await self.cog.on_raw_reaction_add(reaction('🆕', author=12345))
        self.assertIsNone(self.cog._store)
        await self.cog.on_raw_reaction_add(reaction('🆕', message_id=31337))   # unknown bot message
        self.assertIsNone(self.verdict(1))

    async def test_reactions_never_reply_or_react(self):
        self.publish([payload_item(1)])
        await self.cog.on_raw_reaction_add(reaction('🆕'))
        self.bot.get_channel.assert_not_called()


class SyncAndStatsTests(CogFixture):
    async def test_sync_copies_exposures_and_cleans_up_once_a_day(self):
        self.publish([payload_item(1), payload_item(2)])
        with patch.object(type(self.cog.store), 'cleanup', wraps=self.cog.store.cleanup) as cleanup:
            self.assertEqual(self.cog._sync_once(now=NOW), 2)
            self.assertEqual(self.cog._sync_once(now=NOW + 600), 0)
            self.assertEqual(cleanup.call_count, 1)
            self.cog._sync_once(now=NOW + DAY)
            self.assertEqual(cleanup.call_count, 2)
        personal = self.cog.store.db.execute('SELECT DISTINCT personal FROM exposures').fetchall()
        self.assertEqual([row[0] for row in personal], [1])

    async def test_loop_iteration_swallows_errors(self):
        with patch.object(self.cog, '_sync_once', side_effect=RuntimeError('boom')), \
                self.assertLogs('cogs.feedback', 'ERROR'):
            await Feedback.sync_loop.coro(self.cog)

    async def test_loop_not_started_when_scheduled_jobs_are_disabled(self):
        self.assertFalse(self.cog.sync_loop.is_running())

    async def test_stats_are_owner_only_and_grouped(self):
        command = self.cog.feedback_stats
        self.assertTrue(command.default_permissions.administrator)
        event = interaction(user=STRANGER)
        await command.callback(self.cog, event, 30, None)
        event.response.send_message.assert_awaited_once_with('反馈统计只对机器人所有者开放。', ephemeral=True)
        self.assertIsNone(self.cog._store)

        run = self.publish([payload_item(1), payload_item(2, publisher='Reuters', category='Investing')])
        await self.cog.handle_button(interaction(), 'n', run, 0, 'new')
        await self.cog.handle_button(interaction(), 'n', run, 1, 'known')
        event = interaction()
        await command.callback(self.cog, event, 30, SimpleNamespace(value='board'))
        event.response.defer.assert_awaited_once_with(ephemeral=True)
        text = event.followup.send.await_args.args[0]
        self.assertIn('按板块', text)
        self.assertIn('`Ottawa`', text)
        self.assertIn('`投资`', text)
        self.assertIn('新知率 50%', text)
        self.assertTrue(event.followup.send.await_args.kwargs['ephemeral'])

    async def test_render_stats_handles_empty_and_long_tables(self):
        blank = {'name': None, 'exposures': 0, 'rated': 0, 'exact': 0, 'new': 0.0, 'known': 0.0, 'skip': 0.0,
                 'new_rate': None}
        text = Feedback.render_stats({'totals': blank, 'rows': []}, 30, 'source')
        self.assertIn('还没有曝光或反馈记录', text)
        rows = [{**blank, 'name': f's{n}', 'exposures': 1, 'new': 1 / 3, 'new_rate': 1.0} for n in range(20)]
        text = Feedback.render_stats({'totals': {**blank, 'exposures': 20}, 'rows': rows}, 7, 'source')
        self.assertIn('……另有 5 组', text)
        self.assertIn('🆕0.3', text)
        self.assertNotIn('`s15`', text)


class LifecycleTests(CogFixture):
    async def test_store_is_lazy_and_unload_closes_it_and_removes_buttons(self):
        self.assertIsNone(self.cog._store)
        self.assertFalse((self.root / 'data' / 'feedback.sqlite3').exists())
        await self.cog.cog_load()
        self.bot.add_dynamic_items.assert_called_once_with(FeedbackButton)
        store = self.cog.store
        self.assertTrue((self.root / 'data' / 'feedback.sqlite3').exists())
        await self.cog.cog_unload()
        self.bot.remove_dynamic_items.assert_called_once_with(FeedbackButton)
        self.assertIsNone(self.cog._store)
        with self.assertRaises(Exception):
            store.db.execute('SELECT 1')


if __name__ == '__main__':
    unittest.main()
