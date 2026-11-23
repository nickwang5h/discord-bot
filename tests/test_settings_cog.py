import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.inbox import Inbox
from cogs.settings import SettingsCog

OWNER, STRANGER = 1, 2
CHANNEL = SimpleNamespace(id=555, mention="<#555>")


def interaction(user=OWNER):
    return SimpleNamespace(
        user=SimpleNamespace(id=user), guild_id=77,
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def owner_bot():
    bot = MagicMock()
    bot.is_owner = AsyncMock(side_effect=lambda user: user.id == OWNER)
    return bot


# Every command that writes a global setting or secret, with its arguments after the interaction.
SETTINGS_WRITES = {
    "set_gemini_key": ("key-value",),
    "set_news_channel": (CHANNEL,),
    "set_test_news_channel": (CHANNEL,),
    "set_model": ("gemini-x",),
    "set_reading_channel": (CHANNEL,),
    "set_models_channel": (CHANNEL,),
    "set_weather_channel": (CHANNEL,),
    "set_weather_cities": ("Ottawa",),
    "set_gaming_channel": (CHANNEL,),
}


class GlobalSettingsOwnerOnlyTests(unittest.IsolatedAsyncioTestCase):
    """Settings are global while the bot also serves shared servers: only the owner may write them."""

    def setUp(self):
        self.cog = SettingsCog(owner_bot())
        store = SettingsCog.set_news_channel.callback.__globals__["settings"]
        client = SettingsCog.set_news_channel.callback.__globals__["ai_client"]
        patches = [patch.object(store, "set_setting"), patch.object(store, "set_secret"),
                   patch.object(client, "reload_client", return_value=True)]
        self.set_setting, self.set_secret, _ = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

    async def test_other_server_admins_are_refused_without_writing(self):
        for name, args in SETTINGS_WRITES.items():
            command = getattr(self.cog, name)
            it = interaction(STRANGER)
            await command.callback(self.cog, it, *args)
            it.response.send_message.assert_awaited_once()
            self.assertTrue(it.response.send_message.call_args.kwargs["ephemeral"], name)
            self.assertIn("所有者", it.response.send_message.call_args.args[0], name)
            it.response.defer.assert_not_awaited()
        self.set_setting.assert_not_called()
        self.set_secret.assert_not_called()

    async def test_owner_still_writes_every_setting(self):
        for name, args in SETTINGS_WRITES.items():
            await getattr(self.cog, name).callback(self.cog, interaction(OWNER), *args)
        self.assertEqual(self.set_setting.call_count, len(SETTINGS_WRITES) - 1)
        self.set_secret.assert_called_once_with("GEMINI_API_KEY", "key-value")

    async def test_commands_stay_hidden_from_non_admins(self):
        for name in SETTINGS_WRITES:
            self.assertTrue(getattr(self.cog, name).default_permissions.administrator, name)


class InboxChannelOwnerOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_the_owner_sets_the_inbox_channel(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(Inbox.save_payload.__globals__, {"STATE_ROOT": Path(directory)}):
            cog = Inbox(owner_bot())
            store = Inbox.set_inbox_channel.callback.__globals__["settings"]
            with patch.object(store, "set_setting") as set_setting:
                stranger = interaction(STRANGER)
                await cog.set_inbox_channel.callback(cog, stranger, CHANNEL)
                set_setting.assert_not_called()
                stranger.response.send_message.assert_awaited_once()
                self.assertTrue(stranger.response.send_message.call_args.kwargs["ephemeral"])
                await cog.set_inbox_channel.callback(cog, interaction(OWNER), CHANNEL)
                set_setting.assert_called_once_with("INBOX_CHANNEL_ID", "555")
        self.assertTrue(cog.set_inbox_channel.default_permissions.administrator)


if __name__ == "__main__":
    unittest.main()
