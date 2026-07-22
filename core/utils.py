import discord
import re
import asyncio
from typing import Callable, Any, Coroutine

_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)


def _split_markdown_table_row(line: str) -> list[str]:
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [cell.strip() for cell in row.split("|")]


def normalize_markdown_tables(text: str) -> str:
    """把 Discord 不支持的 Markdown 表格转换为普通项目符号列表。"""
    lines = text.splitlines()
    normalized: list[str] = []
    index = 0
    in_code_block = False

    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith("```"):
            in_code_block = not in_code_block
            normalized.append(line)
            index += 1
            continue

        is_table_start = (
            not in_code_block
            and index + 1 < len(lines)
            and "|" in line
            and _TABLE_SEPARATOR_RE.match(lines[index + 1]) is not None
        )
        if not is_table_start:
            normalized.append(line)
            index += 1
            continue

        headers = _split_markdown_table_row(line)
        index += 2  # 跳过表头和分隔行
        converted_rows = []
        while index < len(lines) and "|" in lines[index] and lines[index].strip():
            cells = _split_markdown_table_row(lines[index])
            if len(cells) < 2:
                break

            first = cells[0] or "未命名"
            first_part = first if first.startswith("**") and first.endswith("**") else f"**{first}**"
            details = []
            for cell_index, cell in enumerate(cells[1:], start=1):
                if not cell:
                    continue
                if len(headers) == 2:
                    details.append(cell)
                else:
                    label = headers[cell_index] if cell_index < len(headers) else "详情"
                    details.append(f"**{label}**：{cell}")

            suffix = f" — {'；'.join(details)}" if details else ""
            converted_rows.append(f"- {first_part}{suffix}")
            index += 1

        normalized.extend(converted_rows)

    return "\n".join(normalized)


def create_ai_embed(title: str, description: str, color: discord.Color = discord.Color.blue()) -> discord.Embed:
    """
    创建一个统一格式的 AI 回复 Embed 卡片，并自动处理 Discord 4096 字符限制截断。
    """
    footer_text = "✨ Powered by AI"
    
    fallback_indicator = "> 💡 *(由于主网络限流，本条回复已自动切换至 OpenRouter 免费节点生成)*"
    if fallback_indicator in description:
        description = description.replace(f"\n\n{fallback_indicator}", "")
        description = description.replace(fallback_indicator, "")
        
    model_match = re.search(r'<!--MODEL:(.*?)-->', description)
    if model_match:
        model_name = model_match.group(1)
        footer_text = f"✨ Powered by {model_name}"
        description = re.sub(r'\n*<!--MODEL:.*?-->\n*', '', description)

    description = normalize_markdown_tables(description)

    if len(description) > 4000:
        description = description[:4000] + "\n\n...(内容过长，已被自动截断)"
        
    embed = discord.Embed(
        title=title,
        description=description,
        color=color
    )
    embed.set_footer(text=footer_text)
    return embed

async def with_retry(task_name: str, coro_func: Callable[[], Coroutine[Any, Any, Any]], max_retries: int = 5, delay: int = 300) -> Any:
    """
    通用的重试包装函数，适用于需要定期执行且易受网络波动的任务。
    如果在最大重试次数内失败，则会引发最终异常并记录错误日志。
    """
    for attempt in range(max_retries):
        try:
            return await coro_func()
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"[{task_name}] 执行失败 (尝试 {attempt+1}/{max_retries}): {e}，将在 {delay} 秒后重试...")
                await asyncio.sleep(delay)
            else:
                print(f"🚨 紧急：[{task_name}] 在重试 {max_retries} 次后彻底告负: {e}")
                # 此处可以选择通知管理员 (如通过 webhook)
                raise
