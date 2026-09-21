#!/usr/bin/env python3
"""Offline news cutover gate; never run against a live Bot's SQLite file."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.news.migration import legacy_records
from core.news.store import NewsStore
from core.storage import JsonStore


def main():
    parser = argparse.ArgumentParser(description='停止旧新闻任务后导入快照；默认只校验，不写入')
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--general-history', type=Path)
    parser.add_argument('--discovery-history', type=Path)
    parser.add_argument('--general-channel', type=int)
    parser.add_argument('--discovery-channel', type=int)
    parser.add_argument('--fresh', action='store_true', help='明确确认这是没有旧投递历史的新安装')
    parser.add_argument('--apply', action='store_true', help='确认 Bot 已停止，写入 SQLite 并打开新入口投递门禁')
    args = parser.parse_args()
    if not args.state_dir.is_absolute():
        parser.error('--state-dir 必须为绝对路径')
    if args.fresh and (args.general_history or args.discovery_history):
        parser.error('--fresh 不能与旧历史路径同时使用')
    if not args.fresh and not (args.general_history and args.discovery_history):
        parser.error('必须同时指定两份旧历史快照，或显式 --fresh；缺失历史不能静默当空历史')

    def read_snapshot(path):
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('历史快照必须是至多2MB的普通文件')
        return JsonStore(path, list).read(strict=True)

    records = [] if args.fresh else legacy_records(read_snapshot(args.general_history),
        read_snapshot(args.discovery_history), general_channel=args.general_channel,
        discovery_channel=args.discovery_channel)
    if args.apply:
        store = NewsStore(args.state_dir / 'data' / 'news.sqlite3')
        try:
            if args.fresh and store.ready:
                raise ValueError('已初始化数据库不能重复按 fresh 初始化')
            store.import_history(records)
        finally:
            store.close()
        print('新闻历史初始化完成；未调用 Discord 或模型。')
    else:
        print('历史校验通过；未修改任何状态。停机并备份后方可使用 --apply。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
