import datetime
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.ai_providers import AIResult
from core.feeds import FeedItem
from core.jobs import RetryPolicy
from core.news.ingest import normalize
from core.news.models import Subscription
from core.news.pipeline import NewsPipeline
from core.news.store import NewsStore
from core.news.topics.power_projects import PowerProjectsTopic


def power_article(url='https://example.com/project', text=None):
    date = datetime.datetime.now(datetime.UTC).date().isoformat()
    text = text or f'Canada: North Grid approved the Maple Project substation in Ottawa on {date}. Equipment: transformers.'
    return normalize(FeedItem('World', 'BBC World', 'Canada power project update', url, text, time.time()), time.time())


def sub(countries=None):
    return Subscription('power', 'power_projects', ('general',), ('12:00',), 123,
                        {'countries': countries or ['CA']})


def payload(**updates):
    date = datetime.datetime.now(datetime.UTC).date().isoformat()
    item = {'id': 'P01', 'title': '加拿大电网项目获批', 'summary': '该变电站项目获批，原文提及变压器。',
            'project': 'Maple Project', 'actor': 'North Grid', 'location': 'Ottawa', 'equipment': 'transformers',
            'country': 'CA', 'country_quote': 'Canada', 'stage': 'planning', 'stage_quote': 'approved',
            'event_date': date, 'new_fact': f'North Grid approved the Maple Project substation in Ottawa on {date}.'}
    return json.dumps({'items': [{**item, **updates}]}, ensure_ascii=False)


class PowerTopicTests(unittest.TestCase):
    def setUp(self):
        self.topic = PowerProjectsTopic()
        self.prepared = self.topic.prepare([power_article()], sub(), [], '午后')

    def validate(self, **updates):
        return self.topic.validate(payload(**updates), self.prepared.candidates, sub(), [])

    def test_extracts_distinct_fields_and_renders_provenance(self):
        selected = self.validate()
        self.assertEqual(selected[0]['stage'], 'planning')
        embed = self.topic.render(selected, '午后', 'Test')[0]
        self.assertIn('不是采购', embed.description)
        self.assertIn('不代表采购', embed.description)
        self.assertIn('原文发布', embed.description)
        self.assertIn('新增事实（原文）', embed.description)
        self.assertIn(power_article().url, embed.description)
        self.assertNotIn('url', self.prepared.data['candidates'][0])

    def test_wrong_country_fabricated_entities_and_unquoted_claims_are_rejected(self):
        for update in [{'country': 'US'}, {'project': 'Invented project'}, {'actor': 'Imaginary buyer'},
                       {'equipment': 'GIS switchgear'}, {'new_fact': 'New open procurement'},
                       {'stage': 'procurement'}, {'event_date': '2020-01-01'}, {'country': []}]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.validate(**update)

    def test_awarded_cannot_be_promoted_to_open_procurement(self):
        text = 'Canada: North Grid awarded the tender for Maple Project in Ottawa. Equipment: transformers.'
        candidates = self.topic.prepare([power_article(text=text)], sub(), [], '午后').candidates
        with self.assertRaisesRegex(ValueError, '已授标'):
            self.topic.validate(payload(stage='procurement', stage_quote='tender', event_date=None,
                new_fact='North Grid awarded the tender for Maple Project in Ottawa.'), candidates, sub(), [])
        selected = self.topic.validate(payload(stage='awarded', stage_quote='awarded', event_date=None,
                new_fact='North Grid awarded the tender for Maple Project in Ottawa.'), candidates, sub(), [])
        self.assertEqual(selected[0]['stage'], 'awarded')

    def test_no_candidates_for_unrelated_scope_or_unknown_publication(self):
        prepared = self.topic.prepare([power_article()], sub(['DE']), [], '午后')
        self.assertEqual(prepared.candidates, [])
        from dataclasses import replace
        prepared = self.topic.prepare([replace(power_article(), published_at=None)], sub(), [], '午后')
        self.assertEqual(prepared.candidates, [])

    def test_link_changes_do_not_change_milestone_key_but_new_stage_does(self):
        selected = self.validate()[0]
        self.assertEqual(self.topic.identity(selected), self.topic.identity({**selected, 'url': 'https://another.example/new'}))
        self.assertNotEqual(self.topic.identity(selected), self.topic.identity({**selected, 'stage': 'awarded'}))


class PowerPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_media_link_is_not_a_second_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            store = NewsStore(Path(directory) / 'news.sqlite3')
            store.import_history([])
            pipeline = NewsPipeline(store)
            pipeline.ingester.collect = AsyncMock()
            channel = SimpleNamespace(id=123, send=AsyncMock(return_value=SimpleNamespace(id=789)))
            model = AsyncMock(return_value=AIResult(payload(), 'Test', 'model'))
            try:
                with patch('core.news.pipeline.ai_client.generate_ai', model):
                    store.upsert_articles([power_article()])
                    await pipeline.publish(sub(), '2026-08-01/12:00', channel, retry_policy=RetryPolicy(attempts=1))
                    store.upsert_articles([power_article(url='https://another.example/report')])
                    await pipeline.publish(sub(), '2026-08-02/12:00', channel, retry_policy=RetryPolicy(attempts=1))
                channel.send.assert_awaited_once()
            finally:
                store.close()

    async def test_processing_cache_is_separated_by_geographic_parameters(self):
        text = 'Canada and United States plan a new power grid connection.'
        with tempfile.TemporaryDirectory() as directory:
            store = NewsStore(Path(directory) / 'news.sqlite3')
            pipeline = NewsPipeline(store)
            pipeline.ingester.collect = AsyncMock()
            store.upsert_articles([power_article(text=text)])
            model = AsyncMock(return_value=AIResult('{"items":[]}', 'Test', 'model'))
            try:
                with patch('core.news.pipeline.ai_client.generate_ai', model):
                    await pipeline.preview(sub(['CA']), '2026-08-01/12:00')
                    await pipeline.preview(sub(['US']), '2026-08-01/12:00')
                    await pipeline.preview(sub(['CA']), '2026-08-01/12:00')
                self.assertEqual(model.await_count, 2)
                self.assertEqual(len(store.db.execute('SELECT * FROM processing').fetchall()), 2)
            finally:
                store.close()
