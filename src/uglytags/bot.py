import logging
import os

import aiohttp
import discord
from discord import app_commands

from uglytags.db import Database
from uglytags.i18n import t
from uglytags.media import BYTES_PER_MB, DEFAULT_MAX_MEDIA_MB, parse_media_limit_mb
from uglytags.ui import register_commands, report_error

log = logging.getLogger(__name__)


class CommandTree(app_commands.CommandTree):
    async def on_error(self, interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(t("error.permissions"), ephemeral=True)
        else:
            await report_error(interaction, error)


class UglyTags(discord.Client):
    def __init__(
        self,
        database_url: str,
        test_guild_id: int | None = None,
        max_media_mb: int = DEFAULT_MAX_MEDIA_MB,
    ):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.presences = True
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.db = Database(database_url)
        self.tree = CommandTree(self)
        self.test_guild_id = test_guild_id
        self.max_media_mb = max_media_mb
        self.max_media_bytes = max_media_mb * BYTES_PER_MB
        self.session: aiohttp.ClientSession | None = None
        register_commands(self)

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        await self.db.open()
        if self.test_guild_id:
            guild = discord.Object(id=self.test_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        log.info("UglyTags connected to %s servers", len(self.guilds))

    async def close(self):
        if self.session:
            await self.session.close()
        await self.db.close()
        await super().close()


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    token = os.environ.get("DISCORD_TOKEN")
    database_url = os.environ.get("DATABASE_URL")
    if not token or not database_url:
        raise SystemExit("DISCORD_TOKEN and DATABASE_URL must be set")
    test_guild_id = os.environ.get("DISCORD_TEST_GUILD_ID")
    try:
        max_media_mb = parse_media_limit_mb(os.environ.get("MAX_MEDIA_MB"))
    except ValueError as error:
        raise SystemExit(str(error)) from error
    bot = UglyTags(database_url, int(test_guild_id) if test_guild_id else None, max_media_mb)
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
