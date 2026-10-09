"""The owner's personal feed list: a validated JSON document under the state root.

Every personal source belongs to the `following` group, which only the personal
`following` subscription (delivered to the inbox channel) may read. RSSHub entries store a route path, never
the access key; the key is appended from the runtime env at fetch time.
"""
import asyncio
import ipaddress
import logging
import re
from urllib.parse import parse_qsl, urlsplit

import aiohttp

from config import STATE_ROOT, get_env
from core.feeds import DEFAULT_HEADERS, MAX_FEED_BYTES, FeedSource, _parse_feed
from core.storage import JsonStore
from core.web_fetcher import UnsafeUrlError, _validate_public_url

logger = logging.getLogger(__name__)

PERSONAL_GROUPS = ('following',)
# Sections of the personal feed; `category` must be one of these.
SECTIONS = ('Ottawa', 'Sudbury', 'Investing', 'AI-Tech', 'General')
MAX_SOURCES = 30
PROBE_ITEMS = 40
_NAME_RE = re.compile(r'[^\s<>@`*_|\[\]()#]{1}[^<>@`*_|\[\]()#\n]{0,29}')
_PATH_RE = re.compile(r'/[A-Za-z0-9_\-./:@%+~,=]{1,199}')

# What the bot reads when the owner has never edited the list. Twitter accounts and
# Telegram channels need names only the owner can supply; see docs/news.md.
DEFAULT_SOURCES = [
    {'name': 'Ottawa Citizen', 'category': 'Ottawa', 'url': 'https://ottawacitizen.com/feed'},
    {'name': 'CBC Ottawa', 'category': 'Ottawa', 'url': 'https://www.cbc.ca/webfeed/rss/rss-canada-ottawa'},
    {'name': 'CityNews Ottawa', 'category': 'Ottawa', 'url': 'https://ottawa.citynews.ca/feed'},
    {'name': 'Sudbury.com', 'category': 'Sudbury', 'url': 'https://www.sudbury.com/rss'},
    {'name': 'CBC Sudbury', 'category': 'Sudbury', 'url': 'https://www.cbc.ca/webfeed/rss/rss-canada-sudbury'},
    {'name': 'Northern Ontario Business', 'category': 'Sudbury', 'url': 'https://www.northernontariobusiness.com/rss'},
    {'name': '华尔街见闻', 'category': 'Investing', 'rsshub': '/telegram/channel/cnwallstreet'},
    {'name': 'Globe Investing', 'category': 'Investing',
     'url': 'https://www.theglobeandmail.com/arc/outboundfeeds/rss/category/investing/'},
    {'name': 'Globe Economy', 'category': 'Investing',
     'url': 'https://www.theglobeandmail.com/arc/outboundfeeds/rss/category/business/economy/'},
    {'name': 'Federal Reserve', 'category': 'Investing', 'url': 'https://www.federalreserve.gov/feeds/press_all.xml'},
    {'name': 'HF 每日论文', 'category': 'AI-Tech', 'rsshub': '/huggingface/daily-papers'},
    {'name': 'Simon Willison', 'category': 'AI-Tech', 'url': 'https://simonwillison.net/atom/everything/'},
    {'name': 'MIT Technology Review', 'category': 'AI-Tech', 'url': 'https://www.technologyreview.com/feed/'},
    {'name': 'The Conversation CA', 'category': 'General', 'url': 'https://theconversation.com/ca/articles.atom'},
    {'name': 'B站关注', 'category': 'General', 'rsshub': '/bilibili/followings/video/{uid}'},
]


def _default_document():
    return {'version': 1, 'sources': [dict(entry) for entry in DEFAULT_SOURCES]}


_store = None


def store():
    global _store
    if _store is None:
        _store = JsonStore(STATE_ROOT / 'data' / 'personal_sources.json', _default_document)
    return _store


class ProbeError(RuntimeError):
    pass


def _rsshub_base():
    base = get_env('RSSHUB_URL')
    return base.rstrip('/') if base and get_env('RSSHUB_ACCESS_KEY') else None


def validate_path(path):
    if (not isinstance(path, str) or not _PATH_RE.fullmatch(path.replace('{uid}', 'uid'))
            or '..' in path or '//' in path):
        raise ValueError('RSSHub 路由无效：以 / 开头，只含字母数字和 -_./:@%+~,=，不带查询参数')
    return path


def validate_public_url(url):
    """Synchronous shape check; `check_public_host` adds the DNS check before saving."""
    if not isinstance(url, str) or len(url) > 300 or any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError('地址无效')
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError as error:
        raise ValueError('地址无效') from error
    host = (parts.hostname or '').rstrip('.').lower()
    if parts.scheme != 'https' or not host or parts.username or parts.password or parts.fragment:
        raise ValueError('只接受不含登录信息的 https 公网 RSS 地址')
    if host == 'localhost' or host.endswith(('.local', '.internal', '.lan')) or '.' not in host:
        raise ValueError('不能使用本机或内网地址')
    try:
        literal = ipaddress.ip_address(host.strip('[]'))
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ValueError('不能使用本机、私网或保留地址')
    base = get_env('RSSHUB_URL')
    if base and urlsplit(base).hostname == host:
        raise ValueError('RSSHub 地址请填路由（例如 /twitter/user/名字）')
    return url


def validate_entry(raw):
    if not isinstance(raw, dict) or set(raw) - {'name', 'category', 'rsshub', 'url'}:
        raise ValueError('字段无效')
    name = raw.get('name')
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError('名称需为 1–30 个字符，不含换行和 Markdown 符号')
    category = raw.get('category')
    if category not in SECTIONS:
        raise ValueError(f'板块只能是 {"、".join(SECTIONS)}')
    if ('rsshub' in raw) == ('url' in raw):
        raise ValueError('rsshub 与 url 必须且只能填一个')
    entry = {'name': name, 'category': category}
    if 'rsshub' in raw:
        entry['rsshub'] = validate_path(raw['rsshub'])
    else:
        entry['url'] = validate_public_url(raw['url'])
    return entry


def parse_document(document, reserved_names=()):
    """Return valid entries and per-entry errors; one bad entry never disables the rest."""
    if not isinstance(document, dict) or not isinstance(document.get('sources'), list):
        raise ValueError('个人信源文件必须是含 sources 列表的对象')
    entries, errors, names, addresses = [], [], set(reserved_names), set()
    for index, raw in enumerate(document['sources'][:MAX_SOURCES]):
        try:
            entry = validate_entry(raw)
            address = entry.get('rsshub') or entry['url']
            if entry['name'] in names:
                raise ValueError('名称与共享信源或其他个人信源重复')
            if address in addresses:
                raise ValueError('地址重复')
            names.add(entry['name'])
            addresses.add(address)
            entries.append(entry)
        except ValueError as error:
            errors.append(f'个人信源第 {index + 1} 项: {error}')
    if len(document['sources']) > MAX_SOURCES:
        errors.append(f'个人信源最多 {MAX_SOURCES} 个，多出的已忽略')
    return entries, errors


def load(reserved_names=()):
    """Read on every call so edits take effect at the next collection without a restart."""
    try:
        return parse_document(store().read(strict=True), reserved_names)
    except (RuntimeError, ValueError) as error:
        # Fail toward the built-in list: these feeds only reach the owner's inbox.
        logger.error('个人信源文件无效，使用默认清单: %s', error)
        entries, errors = parse_document(_default_document(), reserved_names)
        return entries, [f'个人信源文件无效，已使用默认清单：{error}', *errors]


def feed_source(entry):
    """Resolve an entry to a fetchable feed, or None when its runtime switch is absent."""
    if 'url' in entry:
        return FeedSource(entry['category'], entry['url'], entry['name'])
    base, path = _rsshub_base(), entry['rsshub']
    if base is None:
        return None
    if '{uid}' in path:
        uid = get_env('BILIBILI_UID')
        if not uid or not uid.isdecimal():
            return None
        path = path.replace('{uid}', uid)
    return FeedSource(entry['category'], f"{base}{path}?key={get_env('RSSHUB_ACCESS_KEY')}", entry['name'])


def entry_from_input(name, address, category):
    """Turn owner input into an entry. A pasted RSSHub URL is reduced to its route."""
    address = (address or '').strip()
    raw = {'name': (name or '').strip(), 'category': category}
    base = get_env('RSSHUB_URL')
    if address.startswith('/'):
        raw['rsshub'] = address
    elif base and address.lower().startswith(('http://', 'https://')):
        try:
            parts = urlsplit(address)
        except ValueError as error:
            raise ValueError('地址无效') from error
        base_parts = urlsplit(base)
        if parts.hostname and parts.hostname == base_parts.hostname:
            # The access key is dropped here and re-added from the runtime env at fetch time.
            if any(key != 'key' for key, _ in parse_qsl(parts.query, keep_blank_values=True)):
                raise ValueError('RSSHub 路由暂不支持查询参数')
            prefix = base_parts.path.rstrip('/')
            raw['rsshub'] = parts.path[len(prefix):] if prefix and parts.path.startswith(prefix) else parts.path
        else:
            raw['url'] = address
    else:
        raw['url'] = address
    return validate_entry(raw)


async def check_public_host(entry):
    if 'url' in entry:
        try:
            await _validate_public_url(entry['url'])
        except UnsafeUrlError as error:
            raise ValueError(str(error)) from error


async def probe(entry):
    """Fetch once, without redirects, and return the parsed item count. Errors never carry the key."""
    source = feed_source(entry)
    if source is None:
        raise ProbeError('RSSHub 未在运行环境中配置（或缺少 BILIBILI_UID）')
    await check_public_host(entry)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60),
                                         headers=DEFAULT_HEADERS) as session:
            async with session.get(source.url, allow_redirects=False) as response:
                if response.status != 200:
                    raise ProbeError(f'HTTP {response.status}' + ('（请填写跳转后的最终地址）'
                                     if 300 <= response.status < 400 else ''))
                content = await response.content.read(MAX_FEED_BYTES + 1)
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise ProbeError(type(error).__name__) from None
    if len(content) > MAX_FEED_BYTES:
        raise ProbeError('RSS 内容超过 5 MB 限制')
    try:
        items = await asyncio.to_thread(_parse_feed, content, source, max_age_seconds=None,
                                        max_items=PROBE_ITEMS)
    except ValueError:
        raise ProbeError('不是可解析的 RSS/Atom') from None
    return len(items)


def add(entry, reserved_names=()):
    """Atomically append; refuses duplicates against the current file (or defaults)."""
    def mutate(document):
        entries, _ = parse_document(document, reserved_names)
        if len(document['sources']) >= MAX_SOURCES:
            raise ValueError(f'个人信源最多 {MAX_SOURCES} 个')
        address = entry.get('rsshub') or entry.get('url')
        if any(e['name'] == entry['name'] for e in entries) or entry['name'] in reserved_names:
            raise ValueError('名称已存在')
        if any((e.get('rsshub') or e.get('url')) == address for e in entries):
            raise ValueError('地址已存在')
        document['sources'].append(dict(entry))
        return document
    return _update(mutate)


def remove(name):
    def mutate(document):
        kept = [raw for raw in document['sources'] if not (isinstance(raw, dict) and raw.get('name') == name)]
        if len(kept) == len(document['sources']):
            raise ValueError('没有这个名称的个人信源')
        document['sources'] = kept
        return document
    return _update(mutate)


def _update(mutate):
    def guarded(document):
        if not isinstance(document, dict) or not isinstance(document.get('sources'), list):
            raise ValueError('个人信源文件结构损坏；请先修复或删除该文件')
        return mutate(document)
    return store().update(guarded)
