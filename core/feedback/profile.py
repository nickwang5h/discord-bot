"""Reader profile from owner feedback: per-feature weights and the prompt fragment.

Pure: the input is `FeedbackStore.feedback_since()` rows plus `now` (epoch seconds);
no file, model or Discord access. Design: docs/design-anti-info-gap.md §3.2 and §3.5.

- Window 90 days; each verdict contributes `weight × 0.5^(age_days / 30)` (exact
  weight 1, coarse 1/n as stored).
- Baselines p0 (new rate) and q0 (skip rate) with a +1/+2 Laplace prior.
- Per feature f (source, board, tag): r_f = (n + 4·p0)/(n + k + 4),
  z_f = (s + 4·q0)/(n + k + s + 4),
  W_f = clamp((r_f/p0)·((1 − z_f)/(1 − q0)), 0.25, 4.0); W_f = 1 while the decayed
  n + k + s is below 5, and for unseen features.
- Cold start: fewer than 15 exact verdicts in the window → `build()` returns None.
"""
import json
import re
import unicodedata
from dataclasses import dataclass
from types import MappingProxyType

DAY = 86400.0
WINDOW_DAYS = 90
HALF_LIFE_DAYS = 30
PRIOR = 4.0
W_MIN, W_MAX = 0.25, 4.0
MIN_EXACT = 15            # cold start: exact verdicts needed in the window
MIN_EFFECTIVE = 5.0       # per feature: decayed n + k + s needed before W leaves 1

KNOWN_MIN_K, KNOWN_MAX_R, KNOWN_LIMIT = 2.0, 0.3, 15
FRESH_MIN_N, FRESH_MIN_R, FRESH_LIMIT = 2.0, 0.6, 10
SKIP_MIN_S, SKIP_MIN_Z, SKIP_LIMIT = 2.0, 0.6, 10
SOURCE_HIGH_R, SOURCE_LOW_R, SOURCE_LIMIT = 0.6, 0.25, 20
VOCAB_MIN_COUNT, VOCAB_LIMIT = 2, 40
FRAGMENT_LIMIT = 2500
MAX_TAG_CHARS = 24
MAX_SOURCE_CHARS = 60

DIMENSIONS = ('source', 'board', 'tag')
VERDICTS = ('new', 'known', 'skip')
# When the fragment is too long, items are dropped from the tail of these lists, in order.
TRUNCATION_ORDER = ('tag_vocabulary', 'source_novelty', 'fresh_topics', 'not_interested', 'known_topics')
_URLISH = re.compile(r'(?i)(://|www\.|<|>|`|\]\()')


@dataclass(frozen=True)
class FeatureStat:
    """Decayed weighted counts and derived rates of one feature value."""
    name: str
    new: float
    known: float
    skip: float
    count: int          # raw number of verdicts (undecayed, any weight)
    rate_new: float     # r_f
    rate_skip: float    # z_f
    weight: float       # W_f (1.0 while effective < MIN_EFFECTIVE)

    @property
    def effective(self):
        return self.new + self.known + self.skip


def _clean(value, limit):
    """NFKC, single-line, bounded; '' for anything that looks like a URL or markup."""
    text = ' '.join(unicodedata.normalize('NFKC', str(value or '')).split())
    if not text or _URLISH.search(text):
        return ''
    return text[:limit]


def _decay(age_days):
    return 0.5 ** (max(age_days, 0.0) / HALF_LIFE_DAYS)


def _rates(n, k, s, p0, q0):
    r = (n + PRIOR * p0) / (n + k + PRIOR)
    z = (s + PRIOR * q0) / (n + k + s + PRIOR)
    if n + k + s < MIN_EFFECTIVE:
        return r, z, 1.0
    raw = (r / p0) * ((1 - z) / (1 - q0))
    return r, z, min(W_MAX, max(W_MIN, raw))


@dataclass(frozen=True)
class Profile:
    """Immutable reader profile; build it with `build()`."""
    now: float
    exact: int                      # exact verdicts in the window (raw count)
    rated: int                      # all verdicts in the window (raw count)
    totals: tuple                   # decayed (new, known, skip)
    p0: float
    q0: float
    features: MappingProxyType      # dimension -> {name -> FeatureStat}
    vocabulary: tuple               # tags rated ≥ VOCAB_MIN_COUNT times, most frequent first

    # ---- weights -----------------------------------------------------------------
    def feature(self, dimension, name):
        if dimension not in DIMENSIONS:
            raise ValueError(f'unknown dimension: {dimension}')
        return self.features[dimension].get(name)

    def _weight(self, dimension, name):
        stat = self.feature(dimension, name)
        return stat.weight if stat else 1.0

    def weight_for_source(self, name):
        return self._weight('source', _clean(name, MAX_SOURCE_CHARS))

    def weight_for_board(self, board_id):
        return self._weight('board', str(board_id or ''))

    def weight_for_tag(self, tag):
        return self._weight('tag', _clean(tag, MAX_TAG_CHARS))

    # ---- §3.5 sets ---------------------------------------------------------------
    def _tags(self):
        return self.features['tag'].values()

    @property
    def known_topics(self):
        picked = [t for t in self._tags() if t.known >= KNOWN_MIN_K and t.rate_new <= KNOWN_MAX_R]
        return tuple(t.name for t in sorted(picked, key=lambda t: (-t.known, t.name))[:KNOWN_LIMIT])

    @property
    def fresh_topics(self):
        picked = [t for t in self._tags() if t.new >= FRESH_MIN_N and t.rate_new >= FRESH_MIN_R]
        return tuple(t.name for t in sorted(picked, key=lambda t: (-t.new, t.name))[:FRESH_LIMIT])

    @property
    def not_interested(self):
        picked = [t for t in self._tags() if t.skip >= SKIP_MIN_S and t.rate_skip >= SKIP_MIN_Z]
        return tuple(t.name for t in sorted(picked, key=lambda t: (-t.skip, t.name))[:SKIP_LIMIT])

    @property
    def source_novelty(self):
        picked = [s for s in self.features['source'].values() if s.effective >= MIN_EFFECTIVE]
        result = []
        for stat in sorted(picked, key=lambda s: (-s.effective, s.name))[:SOURCE_LIMIT]:
            level = '高' if stat.rate_new >= SOURCE_HIGH_R else '低' if stat.rate_new <= SOURCE_LOW_R else '中'
            result.append({'publisher': stat.name, 'level': level})
        return tuple(result)

    # ---- prompt ------------------------------------------------------------------
    def prompt_data(self, limit=FRAGMENT_LIMIT):
        """`{"reader_profile": {...}, "tag_vocabulary": [...]}` whose compact JSON fits `limit`.

        Only tag and source names; over-long payloads lose list tails in TRUNCATION_ORDER.
        """
        parts = {'known_topics': list(self.known_topics), 'fresh_topics': list(self.fresh_topics),
                 'not_interested': list(self.not_interested), 'source_novelty': list(self.source_novelty),
                 'tag_vocabulary': list(self.vocabulary)}

        def assemble():
            return {'reader_profile': {key: parts[key] for key in
                                       ('known_topics', 'fresh_topics', 'not_interested', 'source_novelty')},
                    'tag_vocabulary': parts['tag_vocabulary']}

        data = assemble()
        while len(_dump(data)) > limit:
            victim = next((key for key in TRUNCATION_ORDER if parts[key]), None)
            if victim is None:
                break
            parts[victim].pop()
            data = assemble()
        return data

    def prompt_fragment(self, limit=FRAGMENT_LIMIT):
        """Compact JSON of `prompt_data()`; at most `limit` characters."""
        return _dump(self.prompt_data(limit))

    def explain(self):
        """Owner-facing text for /feedback_stats: basis, baselines and the fragment sent to the model."""
        n, k, s = self.totals
        return '\n'.join([
            f'读者画像：近 {WINDOW_DAYS} 天 {self.rated} 条反馈（精确 {self.exact} 条），半衰期 {HALF_LIFE_DAYS} 天',
            f'加权：🆕 {n:.1f} · 👌 {k:.1f} · 🚫 {s:.1f}；基线新知率 p0={self.p0:.2f}，不关心率 q0={self.q0:.2f}',
            '当前发给模型的画像片段：',
            self.prompt_fragment(),
        ])


def _dump(data):
    return json.dumps(data, ensure_ascii=False, separators=(',', ':'))


def explain(profile):
    """Like `Profile.explain()`, also covering the cold-start case (None)."""
    if profile is None:
        return f'读者画像：冷启动（近 {WINDOW_DAYS} 天精确反馈不足 {MIN_EXACT} 条），选编不使用画像。'
    return profile.explain()


def build(rows, now, *, personal=None):
    """Profile from `FeedbackStore.feedback_since()` rows, or None on cold start.

    `personal` keeps only rows whose item was shown personally (True) / only shared (False).
    Rows with an unknown verdict or outside the 90-day window are ignored.
    """
    now = float(now)
    horizon = now - WINDOW_DAYS * DAY
    sums = {dim: {} for dim in DIMENSIONS}
    counts = {}
    totals = dict.fromkeys(VERDICTS, 0.0)
    exact = rated = 0
    for row in rows:
        verdict = row.get('verdict')
        at = float(row.get('updated_at') or 0)
        if verdict not in VERDICTS or at < horizon:
            continue
        if personal is not None and bool(row.get('personal')) != personal:
            continue
        contribution = float(row.get('weight') or 0) * _decay((now - at) / DAY)
        if contribution <= 0:
            continue
        rated += 1
        exact += 0 if row.get('coarse') else 1
        totals[verdict] += contribution
        tags = {_clean(tag, MAX_TAG_CHARS) for tag in row.get('tags') or ()} - {''}
        names = {'source': {_clean(row.get('source'), MAX_SOURCE_CHARS)} - {''},
                 'board': {str(row.get('board') or '')} - {''},
                 'tag': tags}
        for dim, values in names.items():
            for value in values:
                bucket = sums[dim].setdefault(value, {'new': 0.0, 'known': 0.0, 'skip': 0.0, 'count': 0})
                bucket[verdict] += contribution
                bucket['count'] += 1
        for tag in tags:
            counts[tag] = counts.get(tag, 0) + 1
    if exact < MIN_EXACT:
        return None

    n, k, s = totals['new'], totals['known'], totals['skip']
    p0 = (n + 1) / (n + k + 2)
    q0 = (s + 1) / (n + k + s + 2)
    features = {}
    for dim, table in sums.items():
        stats = {}
        for name, b in table.items():
            r, z, w = _rates(b['new'], b['known'], b['skip'], p0, q0)
            stats[name] = FeatureStat(name, b['new'], b['known'], b['skip'], b['count'], r, z, w)
        features[dim] = MappingProxyType(stats)
    vocabulary = tuple(tag for tag, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
                       if c >= VOCAB_MIN_COUNT)[:VOCAB_LIMIT]
    return Profile(now=now, exact=exact, rated=rated, totals=(n, k, s), p0=p0, q0=q0,
                   features=MappingProxyType(features), vocabulary=vocabulary)
