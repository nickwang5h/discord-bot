from __future__ import annotations

import discord

from core.utils import normalize_markdown_tables


EMBED_DESCRIPTION_LIMIT = 3900


def _bounded_markdown_parts(text: str, *, limit: int) -> list[str]:
    parts: list[str] = []
    remaining = text
    markers = ("\n\n", "\n", "；", "。", "，", " ")
    while len(remaining) > limit:
        boundaries = [
            index + len(marker)
            for marker in markers
            if (index := remaining.rfind(marker, 0, limit + 1)) >= 0
        ]
        end = max(boundaries, default=limit)
        parts.append(remaining[:end])
        remaining = remaining[end:]
    if remaining:
        parts.append(remaining)
    return parts


def split_curated_markdown(markdown: str) -> list[str]:
    """Return consecutive Discord-sized slices of complete Curator Markdown."""
    normalized = normalize_markdown_tables(markdown)
    return _bounded_markdown_parts(normalized, limit=EMBED_DESCRIPTION_LIMIT)


def create_curated_video_embeds(
    markdown: str, *, provider: str, model: str
) -> list[discord.Embed]:
    chunks = split_curated_markdown(markdown)
    embeds: list[discord.Embed] = []
    for index, chunk in enumerate(chunks, start=1):
        suffix = f" ({index}/{len(chunks)})" if len(chunks) > 1 else ""
        embed = discord.Embed(
            title=f"🔗 B站视频内容总结{suffix}",
            description=chunk,
            color=discord.Color.from_rgb(251, 114, 153),
        )
        embed.set_footer(text=f"✨ Powered by {provider} ({model})")
        embeds.append(embed)
    return embeds
