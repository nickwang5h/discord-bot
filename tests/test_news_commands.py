import asyncio
import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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

        async def publish(subs, now, lookup):
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
