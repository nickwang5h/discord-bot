import asyncio
import datetime
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from cogs import news as news_module
from cogs.news import News
from config import TZ
from core.news.models import Subscription


class NewsCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with patch.object(news_module, 'SCHEDULED_JOBS_ENABLED', False):
            self.cog = News(MagicMock())
        self.cog._pipeline = MagicMock(
            store=SimpleNamespace(ready=True, close=MagicMock()),
            ingester=SimpleNamespace(collect=AsyncMock()),
            publish_due=AsyncMock(),
        )

    async def asyncTearDown(self):
        await self.cog.cog_unload()

    async def test_initial_hourly_tick_is_skipped(self):
        await News.hourly_fetch.coro(self.cog)
        self.cog.pipeline.ingester.collect.assert_not_awaited()
        await News.hourly_fetch.coro(self.cog)
        self.cog.pipeline.ingester.collect.assert_awaited_once()

    async def test_slow_subscription_does_not_block_next_dispatch_tick(self):
        started, second_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def publish(subs, now, lookup, **kwargs):
            if subs[0].id == 'slow':
                started.set()
                await release.wait()
            else:
                second_started.set()

        self.cog.pipeline.publish_due.side_effect = publish
        now = datetime.datetime.now(TZ)
        subs = [Subscription(name, 'general', ('general',), (now.strftime('%H:%M'),), 123) for name in ('slow', 'fast')]
        with patch.object(news_module, 'load_subscriptions', return_value=([subs[0]], [])):
            await News.dispatch.coro(self.cog)
        await asyncio.wait_for(started.wait(), 1)
        with patch.object(news_module, 'load_subscriptions', return_value=(subs, [])):
            await News.dispatch.coro(self.cog)
        await asyncio.wait_for(second_started.wait(), 1)
        self.assertEqual(self.cog.pipeline.publish_due.await_count, 2)
        release.set()

    async def test_history_gate_prevents_background_publication(self):
        self.cog.pipeline.store.ready = False
        now = datetime.datetime.now(TZ)
        sub = Subscription('general', 'general', ('general',), (now.strftime('%H:%M'),), 123)
        with patch.object(news_module, 'load_subscriptions', return_value=([sub], [])):
            await News.dispatch.coro(self.cog)
        self.cog.pipeline.publish_due.assert_not_awaited()

    async def test_all_compatibility_commands_remain_admin_only(self):
        commands = {command.name: command for command in self.cog.get_app_commands()}
        for name in ('test_news', 'test_hourly_fetch', 'test_scheduled_digest',
                     'news_publish', 'news_preview', 'news_status', 'news_resolve'):
            self.assertIn(name, commands)
            self.assertTrue(getattr(commands[name], 'checks', []))
        self.assertFalse(self.cog.dispatch.is_running())
        self.assertFalse(self.cog.hourly_fetch.is_running())


class NewsFeedbackWiringTests(unittest.IsolatedAsyncioTestCase):
    """§2.8: only personal subscriptions get feedback buttons, scheduled and manual alike."""

    def setUp(self):
        self.bot = MagicMock()
        self.feedback = object()
        self.bot.get_cog.side_effect = lambda name: self.feedback if name == 'Feedback' else None
        with patch.object(news_module, 'SCHEDULED_JOBS_ENABLED', False):
            self.cog = News(self.bot)
        self.cog._pipeline = MagicMock(
            store=SimpleNamespace(ready=True, close=MagicMock()),
            publish=AsyncMock(return_value=None), preview=AsyncMock(return_value=None),
            publish_due=AsyncMock(),
        )
        self.following = Subscription('following', 'following', ('following',), ('08:20',), 2)
        self.general = Subscription('general', 'general', ('general',), ('08:45',), 1)

    async def asyncTearDown(self):
        await self.cog.cog_unload()

    def interaction(self):
        return SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),
                               followup=SimpleNamespace(send=AsyncMock()), channel=None, guild=None)

    async def test_factory_builds_run_buttons_for_personal_topics_only(self):
        self.assertIsNone(self.cog._view_factory(self.general))
        self.assertIsNone(self.cog._view_factory(Subscription('d', 'discovery', ('discovery',), ('08:00',), 1)))
        with patch('cogs.feedback.view_for', return_value='VIEW') as view_for:
            factory = self.cog._view_factory(self.following)
            self.assertEqual(factory(7, 3), 'VIEW')
        view_for.assert_called_once_with('run', 7, 3)
        self.feedback = None
        self.assertIsNone(self.cog._view_factory(self.following))

    async def test_manual_publish_passes_factory_and_preview_does_not(self):
        target = SimpleNamespace(id=2)
        for sub, personal in ((self.following, True), (self.general, False)):
            self.cog._pipeline.publish.reset_mock()
            with patch.object(self.cog, '_subscription', return_value=sub), \
                    patch.object(self.cog, '_channel', return_value=target):
                await self.cog._execute(self.interaction(), sub.id)
                await self.cog._execute(self.interaction(), sub.id, preview=True)
            factory = self.cog._pipeline.publish.await_args.kwargs['view_factory']
            self.assertEqual(factory is not None, personal)
            self.cog._pipeline.preview.assert_awaited_with(sub, ANY)
            self.assertEqual(self.cog._pipeline.preview.await_args.kwargs, {})

    async def test_scheduled_dispatch_passes_the_factory_lookup(self):
        now = datetime.datetime.now(TZ)
        sub = replace(self.following, times=(now.strftime('%H:%M'),))
        with patch.object(news_module, 'load_subscriptions', return_value=([sub], [])):
            await News.dispatch.coro(self.cog)
        await asyncio.gather(*self.cog._scheduled.values())
        kwargs = self.cog._pipeline.publish_due.await_args.kwargs
        self.assertEqual(kwargs['views'], self.cog._view_factory)
