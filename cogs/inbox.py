import asyncio
import logging
import re
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

from config import STATE_ROOT
from core import settings
from core.inbox import DONE, DROPPED, PENDING, InboxStore, extract_article
from core.info_curator_client import is_supported_video_url
from core.web_fetcher import fetch_public_html

SAVE_EMOJI, DONE_EMOJI, DROP_EMOJI = "📥", "✅", "🗑️"
URL_RE = re.compile(r"https?://\S+")
EXCERPT_CHARS = 600
LIST_LIMIT = 15
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Payload:
    url: str | None
    title: str
    note: str
    summary: str


def _first_url(text: str) -> str | None:
    match = URL_RE.search(text or "")
    return match.group(0).rstrip(".,;:!?)]}>\"'*") if match else None


def payload_from_message(message, replied_to=None) -> Payload:
    """What to save from a marked message.

    A person's message carries the link and their own words. A bot card carries a
    title and summary; its link is the card's own, or the one in the message it answered.
    """
    content = message.content or ""
    if not message.author.bot:
        url = _first_url(content)
        note = URL_RE.sub("", content).strip() if url else ""
        return Payload(url=url, title="" if url else content[:80], note=note,
                       summary="" if url else content)

    embeds = list(message.embeds)
    title = next((embed.title for embed in embeds if embed.title), "") or ""
    parts = []
    for embed in embeds:
        if embed.description:
            parts.append(embed.description)
        parts += [f"**{field.name}**\n{field.value}" for field in embed.fields]
    url = next((embed.url for embed in embeds if embed.url), None) or _first_url(content)
    if not url and replied_to is not None:
        url = _first_url(replied_to.content or "")
    return Payload(url=url, title=title, note="", summary="\n\n".join(parts) or content)


class Inbox(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = InboxStore(STATE_ROOT / "inbox")
        self._fetch_slots = asyncio.Semaphore(2)

    async def _is_owner(self, user_id: int) -> bool:
        # The bot also serves shared servers; the inbox belongs to its owner alone.
        return await self.bot.is_owner(discord.Object(id=user_id))

    def _channel_id(self) -> int | None:
        value = settings.get_setting("INBOX_CHANNEL_ID")
        return int(value) if value else None

    async def _article(self, url: str | None):
        if not url or is_supported_video_url(url):
            return None
        try:
            async with self._fetch_slots:
                html = await fetch_public_html(url)
            return await asyncio.to_thread(extract_article, html)
        except Exception as error:
            logger.warning("Inbox 抓取正文失败 [%s]: %s", url, error)
            return None

    async def save_payload(self, payload: Payload, *, origin: str, fallback: discord.abc.Messageable,
                           source: str | None = None, via: str | None = None):
        """Save one payload and post its card; returns the inbox item (existing or new).

        Public for the feedback 📥 button: `source` and `via` attribute the item to the
        feed source it came from. The reaction/message flows call it without them.
        """
        article = await self._article(payload.url)
        title = (article.title if article else "") or payload.title or payload.url or "未命名"
        item, created = await asyncio.to_thread(
            lambda: self.store.save(
                url=payload.url,
                title=title,
                body=article.text if article else "",
                note=payload.note,
                summary=payload.summary,
                origin=origin,
                source=source,
                via=via,
            )
        )
        if not created:
            return item
        text = article.text if article else payload.summary
        excerpt = text[:EXCERPT_CHARS] + ("…" if len(text) > EXCERPT_CHARS else "")
        embed = discord.Embed(title=item["title"][:256], url=item["url"] or None,
                              description=excerpt, color=discord.Color.dark_teal())
        if payload.note:
            embed.add_field(name="备注", value=payload.note[:1024], inline=False)
        got = f"正文 {item['chars']} 字" if article else "未取得正文，只存了链接和摘要"
        embed.set_footer(text=f"{got} · {DONE_EMOJI} 读完 · {DROP_EMOJI} 丢弃")
        channel = self.bot.get_channel(self._channel_id() or 0) or fallback
        file = discord.File(self.store.path(item), filename=item["file"])
        card = await channel.send(embed=embed, file=file)
        await asyncio.to_thread(self.store.set_card, item["id"], card.channel.id, card.id)
        return item

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, event: discord.RawReactionActionEvent):
        emoji = str(event.emoji)
        if emoji not in (SAVE_EMOJI, DONE_EMOJI, DROP_EMOJI) or not await self._is_owner(event.user_id):
            return
        if emoji in (DONE_EMOJI, DROP_EMOJI):
            item = await asyncio.to_thread(self.store.by_card, event.message_id)
            if item:
                state = DONE if emoji == DONE_EMOJI else DROPPED
                await asyncio.to_thread(self.store.set_state, item["id"], state)
            return
        channel = self.bot.get_channel(event.channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(event.message_id)
            replied_to = None
            if message.reference and message.reference.message_id:
                replied_to = await channel.fetch_message(message.reference.message_id)
        except discord.HTTPException:
            return
        payload = payload_from_message(message, replied_to)
        if payload.url or payload.summary:
            await self.save_payload(payload, origin=message.jump_url, fallback=channel)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if (message.author.bot or message.channel.id != self._channel_id()
                or not await self._is_owner(message.author.id)):
            return
        payload = payload_from_message(message)
        if payload.url or payload.summary:
            await self.save_payload(payload, origin=message.jump_url, fallback=message.channel)

    @app_commands.command(name="inbox", description="[管理员] 列出收件箱里还没读的条目（仅所有者）")
    async def inbox(self, interaction: discord.Interaction):
        if not await self._is_owner(interaction.user.id):
            await interaction.response.send_message("收件箱只对机器人所有者开放。", ephemeral=True)
            return
        items = await asyncio.to_thread(self.store.pending)
        if not items:
            await interaction.response.send_message("收件箱是空的。", ephemeral=True)
            return
        lines = []
        for item in items[:LIST_LIMIT]:
            link = ""
            if item.get("card_message_id") and interaction.guild_id:
                link = (f" · [卡片](https://discord.com/channels/{interaction.guild_id}/"
                        f"{item['card_channel_id']}/{item['card_message_id']})")
            lines.append(f"`{item['saved_at'][:10]}` {item['title'][:60]}{link}")
        more = f"\n……另有 {len(items) - LIST_LIMIT} 条" if len(items) > LIST_LIMIT else ""
        await interaction.response.send_message(
            f"**未读 {len(items)} 条**\n" + "\n".join(lines) + more, ephemeral=True)

    @app_commands.command(name="set_inbox_channel", description="[管理员] 设置收件箱频道")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_inbox_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        settings.set_setting("INBOX_CHANNEL_ID", str(channel.id))
        await interaction.followup.send(
            f"✅ 收件箱频道设为 {channel.mention}。在任何消息上点 {SAVE_EMOJI} 即可存入，"
            f"也可以直接把链接或想法发到该频道。", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Inbox(bot))
