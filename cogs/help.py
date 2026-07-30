from collections.abc import Iterable

import discord
from discord import app_commands
from discord.ext import commands

FIELD_VALUE_LIMIT = 1024
DESCRIPTION_LIMIT = 120

CATEGORY_RULES = (
    ("[Dev]", "开发工具"),
    ("[管理员]", "管理员命令"),
    ("[实验]", "实验功能"),
)
CATEGORY_ORDER = ("常用命令", "开发工具", "管理员命令", "实验功能")


def _leaf_commands(
    commands_to_list: Iterable[object],
) -> list[object]:
    leaves: list[object] = []
    for command in commands_to_list:
        children = getattr(command, "commands", None)
        if children:
            leaves.extend(_leaf_commands(children))
        else:
            leaves.append(command)
    return leaves


def _command_category(description: str) -> tuple[str, str]:
    for prefix, category in CATEGORY_RULES:
        if description.startswith(prefix):
            return category, description.removeprefix(prefix).strip()
    return "常用命令", description


def _chunks(lines: list[str], *, max_chars: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in lines:
        added_length = len(line) + (1 if current else 0)
        if current and current_length + added_length > max_chars:
            chunks.append("\n".join(current))
            current = []
            current_length = 0
            added_length = len(line)
        current.append(line)
        current_length += added_length
    if current:
        chunks.append("\n".join(current))
    return chunks


def _build_help_embed(commands_to_list: Iterable[object]) -> discord.Embed:
    grouped: dict[str, list[str]] = {category: [] for category in CATEGORY_ORDER}
    leaves = _leaf_commands(commands_to_list)

    for command in sorted(
        leaves,
        key=lambda item: str(getattr(item, "qualified_name", getattr(item, "name", ""))),
    ):
        name = str(getattr(command, "qualified_name", getattr(command, "name", ""))).strip()
        if not name:
            continue
        raw_description = str(getattr(command, "description", "") or "暂无说明").strip()
        category, description = _command_category(raw_description)
        if len(description) > DESCRIPTION_LIMIT:
            description = f"{description[: DESCRIPTION_LIMIT - 1].rstrip()}…"
        grouped[category].append(f"`/{name}` — {description}")

    embed = discord.Embed(
        title="📚 Jonathan 命令帮助",
        description="使用 `/命令` 调用功能；带“管理员”标记的命令仍会检查 Discord 权限。",
        color=discord.Color.blurple(),
    )
    for category in CATEGORY_ORDER:
        for index, value in enumerate(_chunks(grouped[category], max_chars=FIELD_VALUE_LIMIT)):
            field_name = category if index == 0 else f"{category}（续）"
            embed.add_field(name=field_name, value=value, inline=False)

    embed.set_footer(text=f"当前共 {len(leaves)} 个命令 · 此帮助列表会随已加载命令自动更新")
    return embed


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="列出机器人当前可用的所有命令")
    async def help(self, interaction: discord.Interaction):
        embed = _build_help_embed(self.bot.tree.get_commands())
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
