import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from core.news import personal, sources
from core.news.models import Article, Subscription
from core.news.subscriptions import DEFAULTS, parse_subscription
from core.news.topics import TOPICS
from core.storage import JsonStore

ENV = {'RSSHUB_URL': 'https://rss.example', 'RSSHUB_ACCESS_KEY': 'secret-k', 'BILIBILI_UID': '42'}


class PersonalStoreCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'personal_sources.json'
        patches = [patch.object(personal, '_store', JsonStore(self.path, personal._default_document)),
                   patch.object(personal, 'get_env', ENV.get)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.directory.cleanup)


class PersonalSourceTests(PersonalStoreCase):
    def test_missing_file_uses_valid_defaults_in_five_sections(self):
        entries, errors = sources.personal_entries()
        self.assertEqual(errors, [])
        self.assertEqual(len(entries), len(personal.DEFAULT_SOURCES))
        self.assertEqual({e['category'] for e in entries}, set(personal.SECTIONS))
        self.assertFalse({e['name'] for e in entries} & sources.SHARED_NAMES)
        self.assertFalse(self.path.exists())

    def test_corrupt_file_falls_back_to_defaults_and_reports(self):
        self.path.write_text('{not json', encoding='utf-8')
        entries, errors = sources.personal_entries()
        self.assertEqual(len(entries), len(personal.DEFAULT_SOURCES))
        self.assertIn('默认清单', errors[0])

    def test_bad_entries_are_dropped_individually(self):
        good = {'name': 'Ok', 'category': 'General', 'url': 'https://example.com/feed'}
        bad = [
            {'name': 'A', 'category': 'Nope', 'url': 'https://a.example/feed'},
            {'name': 'B', 'category': 'General', 'url': 'http://b.example/feed'},
            {'name': 'C', 'category': 'General', 'url': 'https://localhost/feed'},
            {'name': 'D', 'category': 'General', 'url': 'https://10.0.0.5/feed'},
            {'name': 'E', 'category': 'General', 'url': 'https://e.example/f', 'rsshub': '/x'},
            {'name': 'F', 'category': 'General', 'rsshub': '/a/../b'},
            {'name': 'G', 'category': 'General', 'rsshub': '/a?key=1'},
            {'name': 'BBC World', 'category': 'General', 'url': 'https://g.example/feed'},
            {'name': 'Ok', 'category': 'General', 'url': 'https://h.example/feed'},
            {'name': 'H', 'category': 'General', 'url': 'https://rss.example/twitter/user/x'},
        ]
        self.path.write_text(json.dumps({'version': 1, 'sources': [good, *bad]}), encoding='utf-8')
        entries, errors = sources.personal_entries()
        self.assertEqual([e['name'] for e in entries], ['Ok'])
        self.assertEqual(len(errors), len(bad))

    def test_edits_apply_on_next_read_and_never_store_the_key(self):
        entry = personal.entry_from_input('X 关注', 'https://rss.example/twitter/home?key=secret-k', 'General')
        self.assertEqual(entry, {'name': 'X 关注', 'category': 'General', 'rsshub': '/twitter/home'})
        personal.add(entry, sources.SHARED_NAMES)
        self.assertNotIn('secret-k', self.path.read_text(encoding='utf-8'))
        urls = {s.name: s.url for s in sources.group_sources('following')}
        self.assertEqual(urls['X 关注'], 'https://rss.example/twitter/home?key=secret-k')
        with self.assertRaises(ValueError):
            personal.add(entry, sources.SHARED_NAMES)
        personal.remove('X 关注')
        self.assertNotIn('X 关注', {s.name for s in sources.group_sources('following')})
        with self.assertRaises(ValueError):
            personal.remove('X 关注')

    def test_input_validation(self):
        with self.assertRaises(ValueError):
            personal.entry_from_input('Y', 'https://rss.example/twitter/home?limit=5', 'General')
        with self.assertRaises(ValueError):
            personal.entry_from_input('Y', 'file:///etc/passwd', 'General')
        with self.assertRaises(ValueError):
            personal.entry_from_input('Y', 'https://192.168.1.1/rss', 'General')
        with self.assertRaises(ValueError):
            personal.entry_from_input('**Y**', 'https://example.com/rss', 'General')
        self.assertEqual(personal.entry_from_input('Y', '/telegram/channel/abc', 'Investing')['rsshub'],
                         '/telegram/channel/abc')

    def test_shared_groups_never_read_personal_sources(self):
        personal_names = {s.name for s in sources.group_sources('following')}
        self.assertTrue(personal_names)
        for group in sources.SHARED_GROUPS:
            self.assertFalse(personal_names & {s.name for s in sources.sources_for([group])})

    def test_personal_group_is_inbox_only(self):
        channels = {'NEWS_CHANNEL_ID': 1, 'INBOX_CHANNEL_ID': 2}
        following = next(raw for raw in DEFAULTS if raw['id'] == 'following')
        self.assertEqual(parse_subscription(following, TOPICS, channels).channel_id, 2)
        leaks = [
            {**DEFAULTS[0], 'source_groups': ['general', 'following']},
            {**following, 'channel_setting': 'NEWS_CHANNEL_ID'},
            {**following, 'channel_id': '1'},
            {**following, 'source_groups': ['following', 'general']},
            {**following, 'topic': 'discovery'},
        ]
        for raw in leaks:
            with self.assertRaises(ValueError):
                parse_subscription(raw, TOPICS, channels)


class ProbeTests(PersonalStoreCase):
    async def _probe(self, entry):
        return await personal.probe(entry)

    def test_probe_errors_do_not_echo_the_key(self):
        import asyncio
        entry = {'name': 'T', 'category': 'General', 'rsshub': '/telegram/channel/abc'}
        response = MagicMock(status=503)
        session = MagicMock()
        session.get.return_value.__aenter__ = AsyncMock(return_value=response)
        session.get.return_value.__aexit__ = AsyncMock(return_value=False)
        session_cm = MagicMock(__aenter__=AsyncMock(return_value=session), __aexit__=AsyncMock(return_value=False))
        with patch.object(personal.aiohttp, 'ClientSession', return_value=session_cm):
            with self.assertRaises(personal.ProbeError) as caught:
                asyncio.run(self._probe(entry))
        self.assertNotIn('secret-k', str(caught.exception))
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])


def _article(source, url, title, age=60):
    now = time.time()
    return Article(f'{source}{url}', source, url, title, f'{title} body', 'General',
                   now - age, now - age, f'v{url}')


class RepostDedupeTests(unittest.TestCase):
    def test_following_drops_reposted_and_already_delivered_titles(self):
        articles = [
            _article('A', 'https://a.example/1', 'Fed holds rates steady'),
            _article('B', 'https://b.example/9', 'Fed  holds rates, steady!'),
            _article('C', 'https://c.example/2', 'Ottawa transit fares rise'),
            _article('D', 'https://d.example/3', 'Already sent story'),
            _article('E', 'https://e.example/old', 'Different headline'),
        ]
        history = [{'url': 'https://x.example/0', 'title': 'already sent story'},
                   {'url': 'https://e.example/old', 'title': 'something else'}]
        sub = Subscription('following', 'following', ('following',), ('08:20',), 2)
        prepared = TOPICS['following'].prepare(articles, sub, history, '早间')
        self.assertEqual([c['url'] for c in prepared.candidates],
                         ['https://a.example/1', 'https://c.example/2'])

    def test_shared_discovery_selection_is_unchanged(self):
        articles = [_article('A', 'https://a.example/1', 'Same'), _article('B', 'https://b.example/1', 'Same')]
        sub = Subscription('discovery', 'discovery', ('discovery',), ('08:00',), 2)
        prepared = TOPICS['discovery'].prepare(articles, sub, [], '早间')
        self.assertEqual(len(prepared.candidates), 2)


class SourceCommandTests(PersonalStoreCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        from cogs import news as news_module
        with patch.object(news_module, 'SCHEDULED_JOBS_ENABLED', False):
            self.bot = MagicMock()
            self.cog = news_module.News(self.bot)

    def _interaction(self):
        return SimpleNamespace(user=SimpleNamespace(id=7),
                               response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                               followup=SimpleNamespace(send=AsyncMock()))

    async def test_non_owner_cannot_add(self):
        self.bot.is_owner = AsyncMock(return_value=False)
        interaction = self._interaction()
        section = SimpleNamespace(value='General')
        await self.cog.source_add.callback(self.cog, interaction, 'Z', 'https://z.example/rss', section)
        self.assertIn('所有者', interaction.response.send_message.await_args.args[0])
        self.assertFalse(self.path.exists())

    async def test_owner_add_probes_then_writes(self):
        self.bot.is_owner = AsyncMock(return_value=True)
        interaction = self._interaction()
        section = SimpleNamespace(value='Ottawa')
        with patch.object(personal, 'probe', AsyncMock(return_value=3)) as probe:
            await self.cog.source_add.callback(self.cog, interaction, 'Z', 'https://z.example/rss', section)
        probe.assert_awaited_once()
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertIn({'name': 'Z', 'category': 'Ottawa', 'url': 'https://z.example/rss'}, saved['sources'])
        self.assertIn('3', interaction.followup.send.await_args.args[0])

    async def test_failed_probe_writes_nothing(self):
        self.bot.is_owner = AsyncMock(return_value=True)
        interaction = self._interaction()
        with patch.object(personal, 'probe', AsyncMock(side_effect=personal.ProbeError('HTTP 404'))):
            await self.cog.source_add.callback(self.cog, interaction, 'Z', '/nope', SimpleNamespace(value='General'))
        self.assertFalse(self.path.exists())
        self.assertIn('未添加', interaction.followup.send.await_args.args[0])


if __name__ == '__main__':
    unittest.main()
