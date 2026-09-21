"""Validate explicitly supplied legacy snapshots without treating AI summaries as evidence."""
import math
import re
import time

from core.news.ingest import clean_text, source_url
from core.news.models import fingerprint


def legacy_records(general, discovery, *, general_channel=None, discovery_channel=None):
    records = []
    for topic, items, channel in [('general', general, general_channel), ('discovery', discovery, discovery_channel)]:
        if not isinstance(items, list) or len(items) > 10000:
            raise ValueError('旧新闻历史必须为有界列表')
        for entry in items:
            if not isinstance(entry, dict):
                raise ValueError('旧历史条目无效，拒绝不完整迁移')
            if topic == 'discovery' and not entry.get('pushed'):
                continue
            url = source_url(entry.get('url'))
            if not url:
                raise ValueError('旧投递历史链接无效，需人工确认后再迁移')
            # The old discovery timestamp was collection time, NOT delivery time.
            timestamp = entry.get('delivered_at', time.time())
            if (type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                    or not 0 <= timestamp <= time.time() + 3600):
                raise ValueError('旧历史时间无效')
            # Unknown evidence becomes a conservative URL tombstone, scoped to its subscription.
            item = {'url': url, 'title': clean_text(entry.get('title'), 300),
                    'publisher': clean_text(entry.get('publisher') or entry.get('source'), 60),
                    'category': clean_text(entry.get('category') or entry.get('source'), 30),
                    'content': clean_text(entry.get('content'), 4000),
                    'rss_summary': clean_text(entry.get('rss_summary'), 500),
                    '_version': '*', '_delivery_key': fingerprint(['legacy', url]),
                    'delivered_at': timestamp, 'legacy_first_seen': entry.get('timestamp')}
            if topic == 'general' and item['rss_summary']:
                item['_version'] = 'legacy-known'
                item['evidence_hash'] = str(entry.get('evidence_hash') or 'legacy-known')[:64]
                summary = re.sub(r'\s+', '', item['rss_summary']).casefold()
                item['_delivery_key'] = fingerprint([url, summary])
            elif topic == 'discovery' and item['content']:
                item['_version'] = fingerprint([item['title'].casefold(), item['content'].casefold()])
                item['_delivery_key'] = fingerprint([url, item['_version']])
            # Old model-only summary deliberately not copied into original evidence.
            records.append((topic, channel, item))
    return records
