#!/usr/bin/env python3
import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import aiohttp

from config import DISCORD_TOKEN
from core import ai_client, settings, web_search
from core.feeds import FeedSource, fetch_feed


class Report:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.successes: list[str] = []

    def ok(self, message: str):
        self.successes.append(message)

    def warn(self, message: str):
        self.warnings.append(message)

    def error(self, message: str):
        self.errors.append(message)

    def print(self):
        for message in self.successes:
            print(f"✅ {message}")
        for message in self.warnings:
            print(f"⚠️  {message}")
        for message in self.errors:
            print(f"❌ {message}")
        print(
            f"\nSummary: {len(self.successes)} passed, "
            f"{len(self.warnings)} warnings, {len(self.errors)} errors"
        )


def run_offline_checks(report: Report, *, strict: bool) -> None:
    if sys.version_info >= (3, 10):
        report.ok(f"Python {sys.version.split()[0]}")
    else:
        report.error("需要 Python 3.10+")

    if DISCORD_TOKEN and DISCORD_TOKEN != "your_discord_bot_token_here":
        report.ok("DISCORD_TOKEN 已配置")
    elif strict:
        report.error("DISCORD_TOKEN 未配置")
    else:
        report.warn("DISCORD_TOKEN 未配置")

    provider_status = ai_client.get_provider_status()
    configured = [name for name in ("gemini", "groq", "zhipu", "openrouter") if provider_status[name]]
    if configured:
        report.ok(f"已配置 AI provider: {', '.join(configured)}")
    elif strict:
        report.error("没有配置任何 AI provider")
    else:
        report.warn("没有配置任何 AI provider")

    for key in ("NEWS_CHANNEL_ID", "TEST_NEWS_CHANNEL_ID", "READING_CHANNEL_ID"):
        value = settings.get_setting(key)
        if value is None:
            report.warn(f"{key} 未配置")
        elif str(value).isdigit():
            report.ok(f"{key} 格式正确")
        else:
            report.error(f"{key} 必须是 Discord channel ID")

    try:
        json.loads(settings.SETTINGS_FILE.read_text(encoding="utf-8"))
        report.ok("settings.json 是有效 JSON")
    except Exception as error:
        report.error(f"settings.json 无效: {error}")

    model_lists = {
        "Groq": ai_client.GROQ_MODELS,
        "Zhipu": ai_client.ZHIPU_MODELS,
        "OpenRouter": ai_client.OPENROUTER_MODELS,
    }
    for provider, specs in model_lists.items():
        model_ids = [spec.model_id for spec in specs]
        if len(model_ids) == len(set(model_ids)):
            report.ok(f"{provider} fallback 列表无重复项")
        else:
            report.error(f"{provider} fallback 列表存在重复项")


async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> dict:
    async with session.get(url, headers=headers) as response:
        response.raise_for_status()
        return await response.json()


async def run_live_checks(report: Report) -> None:
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        gemini_key = settings.get_secret("GEMINI_API_KEY")
        if gemini_key:
            try:
                model = str(ai_client.get_provider_status()["gemini_model"])
                await _get_json(
                    session,
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}",
                    headers={"x-goog-api-key": gemini_key},
                )
                report.ok(f"Gemini key/model 有效（{model}）")
            except Exception as error:
                report.error(f"Gemini key/model 验证失败: {error}")

        try:
            data = await _get_json(session, "https://openrouter.ai/api/v1/models")
            live_ids = {item.get("id") for item in data.get("data", [])}
            missing = [spec.model_id for spec in ai_client.OPENROUTER_MODELS if spec.model_id not in live_ids]
            if missing:
                report.error(f"OpenRouter 模型已失效: {', '.join(missing)}")
            else:
                report.ok("OpenRouter fallback 模型均在线")
        except Exception as error:
            report.error(f"无法验证 OpenRouter 模型: {error}")

        groq_key = settings.get_secret("GROQ_API_KEY")
        if groq_key:
            try:
                data = await _get_json(
                    session,
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {groq_key}"},
                )
                live_ids = {item.get("id") for item in data.get("data", [])}
                missing = [spec.model_id for spec in ai_client.GROQ_MODELS if spec.model_id not in live_ids]
                if missing:
                    report.error(f"Groq 模型不可用: {', '.join(missing)}")
                else:
                    report.ok("Groq fallback 模型均在线")
            except Exception as error:
                report.error(f"无法验证 Groq 模型: {error}")
        else:
            report.warn("未配置 GROQ_API_KEY，跳过 Groq 在线模型检查")

        if DISCORD_TOKEN and DISCORD_TOKEN != "your_discord_bot_token_here":
            try:
                data = await _get_json(
                    session,
                    "https://discord.com/api/v10/users/@me",
                    headers={"Authorization": f"Bot {DISCORD_TOKEN}"},
                )
                report.ok(f"Discord token 有效（bot: {data.get('username', 'unknown')}）")
            except Exception as error:
                report.error(f"Discord token 验证失败: {error}")

    feed_sources = [
        FeedSource("World", "https://feeds.bbci.co.uk/news/world/rss.xml", "BBC"),
        FeedSource("Reading", "https://feeds.npr.org/1004/rss.xml", "NPR"),
    ]
    results = await asyncio.gather(
        *(fetch_feed(source, max_age_seconds=None, max_items=1) for source in feed_sources),
        return_exceptions=True,
    )
    healthy = [
        source.name
        for source, result in zip(feed_sources, results)
        if not isinstance(result, BaseException) and result
    ]
    if len(healthy) == len(feed_sources):
        report.ok(f"RSS 抓取器可用（{', '.join(healthy)}）")
    elif healthy:
        report.warn(f"部分 RSS 可用（{', '.join(healthy)}）")
    else:
        report.error("RSS 抓取器无法读取测试源")

    try:
        sources = await web_search.search_web("人工智能")
        kinds = {source.kind for source in sources}
        expected = {"Google News", "Wikipedia"}
        if kinds == expected:
            report.ok("联网问答抓取可用（Google News, Wikipedia）")
        elif kinds:
            report.warn(f"联网问答部分抓取可用（{', '.join(sorted(kinds))}）")
        else:
            report.error("联网问答未从 Google News 或 Wikipedia 取得结果")
    except Exception as error:
        report.error(f"联网问答抓取检查失败: {error}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Discord bot zero-token health check")
    parser.add_argument("--live", action="store_true", help="验证 Discord 和模型目录 API，不调用模型生成")
    parser.add_argument("--strict", action="store_true", help="把缺少必要配置视为错误")
    args = parser.parse_args()

    report = Report()
    run_offline_checks(report, strict=args.strict)
    if args.live:
        await run_live_checks(report)
    report.print()
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
