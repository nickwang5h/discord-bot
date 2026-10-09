"""Public settings describe subscriptions; SQLite holds no mutable configuration."""
import datetime
import re

from core import settings
from core.news.models import Subscription
from core.news.personal import PERSONAL_GROUPS
from core.news.sources import GROUP_NAMES

DEFAULTS = [
    {'id': 'general', 'topic': 'general', 'source_groups': ['general'],
     'times': ['08:45', '15:30'], 'channel_setting': 'NEWS_CHANNEL_ID', 'max_candidates': 36},
    {'id': 'discovery', 'topic': 'discovery', 'source_groups': ['discovery'],
     'times': ['08:00', '18:00'], 'channel_setting': 'TEST_NEWS_CHANNEL_ID'},
    {'id': 'power-us-ca', 'topic': 'power_projects', 'source_groups': ['general', 'discovery'],
     'times': ['12:00'], 'channel_setting': 'TEST_NEWS_CHANNEL_ID', 'params': {'countries': ['US', 'CA']}},
    # Private: the owner's own feeds, delivered to the owner's inbox channel only.
    {'id': 'following', 'topic': 'following', 'source_groups': ['following'],
     'times': ['08:20', '18:20'], 'channel_setting': 'INBOX_CHANNEL_ID'},
]
PERSONAL_TOPICS = frozenset({'following'})


def bounded_int(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('整数配置超出范围')
    return value


def parse_subscription(raw, topics, channels):
    allowed = {'id', 'topic', 'source_groups', 'times', 'channel_id', 'channel_setting',
               'params', 'max_candidates', 'max_output_tokens', 'enabled'}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ValueError('订阅字段无效')
    identity = raw.get('id')
    if not isinstance(identity, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', identity):
        raise ValueError('订阅 ID 无效')
    topic = raw.get('topic')
    if not isinstance(topic, str) or topic not in topics:
        raise ValueError('专题未注册')
    groups, times = raw.get('source_groups'), raw.get('times')
    if (not isinstance(groups, list) or not 1 <= len(groups) <= len(GROUP_NAMES)
            or any(not isinstance(g, str) or g not in GROUP_NAMES for g in groups)):
        raise ValueError('信源组无效')
    # Personal feeds stay personal: only personal topics read them, only the inbox receives them.
    personal = topic in PERSONAL_TOPICS or any(g in PERSONAL_GROUPS for g in groups)
    if personal and (topic not in PERSONAL_TOPICS or any(g not in PERSONAL_GROUPS for g in groups)
                     or raw.get('channel_setting') != 'INBOX_CHANNEL_ID' or 'channel_id' in raw):
        raise ValueError('个人信源组只能由个人专题读取，并只投递到 INBOX_CHANNEL_ID')
    if (not isinstance(times, list) or not 1 <= len(times) <= 4
            or any(not isinstance(t, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', t) for t in times)
            or len(set(times)) != len(times)):
        raise ValueError('每天出刊时间无效')
    channel_key = raw.get('channel_setting')
    if channel_key is not None and channel_key not in {'NEWS_CHANNEL_ID', 'TEST_NEWS_CHANNEL_ID', 'INBOX_CHANNEL_ID'}:
        raise ValueError('兼容频道设置名无效')
    channel = raw.get('channel_id', channels.get(channel_key))
    if channel is not None:
        if isinstance(channel, bool) or not re.fullmatch(r'[1-9]\d{0,19}', str(channel)):
            raise ValueError('频道 ID 无效')
        channel = int(channel)
    params = raw.get('params', {})
    if not isinstance(params, dict):
        raise ValueError('专题参数无效')
    topics[topic].validate_params(params)
    enabled = raw.get('enabled', True)
    if type(enabled) is not bool:
        raise ValueError('enabled 必须为布尔值')
    return Subscription(identity, topic, tuple(groups), tuple(sorted(times)), channel, params,
                        bounded_int(raw.get('max_candidates', 40), 1, 40),
                        bounded_int(raw.get('max_output_tokens', 3000), 256, 3000), enabled)


def load_subscriptions(topics):
    public = settings.load_settings()
    raw = public.get('NEWS_SUBSCRIPTIONS', DEFAULTS)
    if not isinstance(raw, list) or len(raw) > 16:
        return [], ['NEWS_SUBSCRIPTIONS 必须为至多16份订阅的列表']
    subscriptions, errors, seen, duplicates = [], [], set(), set()
    for index, entry in enumerate(raw):
        try:
            subscription = parse_subscription(entry, topics, public)
            if subscription.id in seen:
                duplicates.add(subscription.id)
                raise ValueError('订阅 ID 重复，所有同名配置停用')
            subscriptions.append(subscription)
            seen.add(subscription.id)
        except (ValueError, TypeError) as error:
            errors.append(f'配置项 {index + 1}: {error}')
    return [sub for sub in subscriptions if sub.id not in duplicates], errors


def period_for(subscription, now, *, scheduled=False):
    clock = now.strftime('%H:%M')
    if scheduled:
        return f'{now.date().isoformat()}/{clock}' if clock in subscription.times else None
    # Manual publication uses the same most recent edition key, never a random retry key.
    elapsed = [slot for slot in subscription.times if slot <= clock]
    date = now.date() if elapsed else now.date() - datetime.timedelta(days=1)
    return f'{date.isoformat()}/{elapsed[-1] if elapsed else subscription.times[-1]}'


def edition_name(period):
    hour = int(period.split('/')[-1][:2])
    return '早间' if hour < 12 else ('午后' if hour < 18 else '晚间')
