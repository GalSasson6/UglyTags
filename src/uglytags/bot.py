import logging
import os

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from uglytags.db import Database
from uglytags.i18n import t
from uglytags.media import (
    BYTES_PER_MB,
    DEFAULT_MAX_MEDIA_MB,
    parse_media_limit_mb,
    parse_positive_int,
)
from uglytags.render import REPLIES, DeleteButton, respond
from uglytags.transfer import ImportLimits
from uglytags.ui import (
    handle_prefix_delete,
    handle_prefix_edit,
    handle_prefix_message,
    is_current_edit,
    is_prefix_command,
    register_commands,
    report_error,
)

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
        import_limits: ImportLimits | None = None,
    ):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.members = True
        intents.presences = True
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.db = Database(database_url)
        self.tree = CommandTree(self)
        self.test_guild_id = test_guild_id
        self.max_media_mb = max_media_mb
        self.max_media_bytes = max_media_mb * BYTES_PER_MB
        self.import_limits = import_limits or ImportLimits()
        self.session: aiohttp.ClientSession | None = None
        register_commands(self)

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        await self.db.open()
        self.add_dynamic_items(DeleteButton)
        if self.test_guild_id:
            guild = discord.Object(id=self.test_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.cleanup.start()

    async def on_ready(self):
        log.info("UglyTags connected to %s servers", len(self.guilds))

    async def on_message(self, message: discord.Message):
        if is_prefix_command(message.content):
            await self.run_prefix(message, handle_prefix_message)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if is_current_edit(payload.message):
            before = payload.cached_message.content if payload.cached_message else None
            await self.run_prefix(payload.message, handle_prefix_edit, before)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        await handle_prefix_delete(payload.message_id)

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        for message_id in payload.message_ids:
            await handle_prefix_delete(message_id)

    async def run_prefix(self, message: discord.Message, handler, *args):
        async with REPLIES.guard(message.id):
            try:
                await handler(self, message, *args)
            except Exception:
                log.exception("Prefix command failed")
                await respond(message, t("error.generic"))

    @tasks.loop(hours=1)
    async def cleanup(self):
        try:
            removed = await self.db.cleanup_uses()
            if removed:
                log.info("Removed %s tag use records older than a year", removed)
        except Exception:
            log.exception("Use log cleanup failed; will try again next hour")

    async def close(self):
        self.cleanup.cancel()
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
        import_limits = parse_import_limits(os.environ)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    bot = UglyTags(
        database_url,
        int(test_guild_id) if test_guild_id else None,
        max_media_mb,
        import_limits,
    )
    bot.run(token, log_handler=None)


def parse_import_limits(environ) -> ImportLimits:
    defaults = ImportLimits()
    return ImportLimits(
        file_bytes=parse_positive_int(
            "MAX_IMPORT_MB", environ.get("MAX_IMPORT_MB"), defaults.file_bytes // BYTES_PER_MB
        )
        * BYTES_PER_MB,
        downloads=parse_positive_int(
            "MAX_IMPORT_DOWNLOADS", environ.get("MAX_IMPORT_DOWNLOADS"), defaults.downloads
        ),
        download_bytes=parse_positive_int(
            "MAX_IMPORT_DOWNLOAD_MB",
            environ.get("MAX_IMPORT_DOWNLOAD_MB"),
            defaults.download_bytes // BYTES_PER_MB,
        )
        * BYTES_PER_MB,
    )


if __name__ == "__main__":
    main()
