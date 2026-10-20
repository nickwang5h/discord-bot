#!/usr/bin/env python3
"""Measure the deterministic digest fact-check on delivered items (read-only, no model calls).

Sources (pick one):
  --db PATH       news.sqlite3; reads the `deliveries` table read-only (default last 14 days)
  --items FILE    JSON list of delivered payload items (the `deliveries.payload` objects),
                  each optionally with "subscription" and "label": "error" | "ok"

Prints per subscription and per provider/model: items, flagged, and with labels the
catches (flagged errors), misses and false alarms (flagged "ok" items). `--show N` lists
the first N flagged items with their reasons.
"""
import argparse
import collections
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.news import factcheck  # noqa: E402

GENERAL_FIELDS = ('digest_title', 'digest_summary')
RECOMMENDATION_FIELDS = ('title', 'summary', 'why_read')


def fields_for(item):
    return GENERAL_FIELDS if 'digest_title' in item else RECOMMENDATION_FIELDS


def load_db(path, days, subscriptions):
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    rows = db.execute('SELECT subscription, payload FROM deliveries WHERE delivered_at >= ? ORDER BY delivered_at',
                      (time.time() - days * 86400,))
    items = []
    for subscription, payload in rows:
        if subscriptions and subscription not in subscriptions:
            continue
        item = json.loads(payload)
        if isinstance(item, dict) and isinstance(item.get('_evidence'), dict):
            items.append({**item, 'subscription': subscription})
    db.close()
    return items


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--db', type=Path)
    group.add_argument('--items', type=Path)
    parser.add_argument('--days', type=float, default=14)
    parser.add_argument('--subscription', action='append', default=[], help='limit to these subscriptions')
    parser.add_argument('--show', type=int, default=0)
    args = parser.parse_args()
    items = (load_db(args.db, args.days, set(args.subscription)) if args.db
             else json.loads(args.items.read_text(encoding='utf-8')))

    totals = collections.defaultdict(collections.Counter)
    flagged_examples = []
    for item in items:
        reasons = factcheck.check_item(item, fields_for(item))
        label = item.get('label')
        model = ' '.join(filter(None, (item.get('_provider'), item.get('_model')))) or '(unrecorded)'
        for key in (f"subscription {item.get('subscription', '?')}", f'model {model}', 'ALL'):
            counter = totals[key]
            counter['items'] += 1
            counter['flagged'] += bool(reasons)
            if label == 'error':
                counter['errors'] += 1
                counter['caught' if reasons else 'missed'] += 1
            elif label == 'ok':
                counter['ok'] += 1
                counter['false_alarm'] += bool(reasons)
        if reasons:
            flagged_examples.append((item, reasons))

    for key in sorted(totals, key=lambda k: (k != 'ALL', k)):
        c = totals[key]
        line = f"{key}: items={c['items']} flagged={c['flagged']} ({c['flagged'] / c['items']:.1%})"
        if c['errors'] or c['ok']:
            line += (f" | labelled errors={c['errors']} caught={c['caught']} missed={c['missed']}"
                     f" | labelled ok={c['ok']} false_alarms={c['false_alarm']}")
        print(line)
    for item, reasons in flagged_examples[:args.show]:
        fields = fields_for(item)
        print('\n-', item.get('subscription', '?'), item.get('label', ''), reasons)
        print('  out:', ' / '.join(str(item.get(f) or '') for f in fields[:2]))
        print('  src:', factcheck.original_title(item), '/', factcheck.original_summary(item)[:200])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
