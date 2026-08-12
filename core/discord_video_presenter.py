from __future__ import annotations

import re

import discord


EMBED_DESCRIPTION_LIMIT = 3900
_CITATION_TOKEN = (
    r"\[[0-9]{2,}:[0-9]{2}:[0-9]{2}–[0-9]{2,}:[0-9]{2}:[0-9]{2}\]"
    r"\(https://www\.bilibili\.com/video/BV[A-Za-z0-9]{10,20}\?t=[0-9]+\)"
    r" `seg-[0-9]{6}`"
)
_AUDIT_CITATION_LINE = re.compile(
    rf"(?:  - )?引用：{_CITATION_TOKEN}(?:；{_CITATION_TOKEN})*[ \t]*"
)


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


def compact_curated_markdown(markdown: str) -> str:
    """Hide owner-rendered citation audit rows in the Discord presentation only."""
    retained = [
        line
        for line in markdown.splitlines(keepends=True)
        if _AUDIT_CITATION_LINE.fullmatch(line.rstrip("\r\n")) is None
    ]
    return re.sub(r"\n{3,}", "\n\n", "".join(retained))


def split_curated_markdown(markdown: str) -> list[str]:
    """Return Discord-sized slices after presentation-only compaction."""
    compacted = compact_curated_markdown(markdown)
    return _bounded_markdown_parts(compacted, limit=EMBED_DESCRIPTION_LIMIT)


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
