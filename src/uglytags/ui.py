import io
import logging
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord
import psycopg
from discord import app_commands

from uglytags import render, tagscript
from uglytags.db import MAX_GROUP_MEMBERS, GroupFullError, is_group, new_id, target_id
from uglytags.i18n import t
from uglytags.media import (
    BYTES_PER_MB,
    MAX_FILES,
    MediaError,
    attach_references,
    read_uploads,
    referenced_files,
    unique_filename,
)
from uglytags.transfer import (
    MAX_CONTENT,
    MAX_NAME,
    TransferError,
    clean_name,
    export_payload,
    import_tags,
)

log = logging.getLogger(__name__)

CONFIRM_TIMEOUT = 300
REMOVE_ALL_TIMEOUT = 120
LIST_PAGE_SIZE = 25
DETAIL_PAGE_SIZE = 10
MAX_UNDO = 5
EDIT_EVENT_SECONDS = 60
UNDO_NOTICE_SECONDS = 1
UNDO_PATTERN = re.compile(r"^\.undo(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
TAG_PATTERN = re.compile(r"^\.(t|tag)(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
PERIODS = (0, 7, 30, 365)
SORTS = ("most", "least", "recent", "unused", "newest", "oldest")
PREFIX_COMMANDS = {
    "add": "create",
    "alias": "alias",
    "create": "create",
    "delete": "remove",
    "edit": "edit",
    "info": "info",
    "list": "list",
    "owner": "info",
    "random": "random",
    "raw": "info",
    "remove": "remove",
    "rename": "rename",
    "search": "search",
    "show": "show",
}


async def report_error(interaction: discord.Interaction, error: Exception):
    log.error("Interaction failed", exc_info=error)
    if interaction.response.is_done():
        await interaction.followup.send(t("error.generic"), ephemeral=True)
    else:
        await interaction.response.send_message(t("error.generic"), ephemeral=True)


async def reply(interaction: discord.Interaction, content: str, **kwargs):
    kwargs.setdefault("ephemeral", True)
    if interaction.response.is_done():
        await interaction.followup.send(content, **kwargs)
    else:
        await interaction.response.send_message(content, **kwargs)


def code(value: str) -> str:
    return "`" + value.replace("`", "\u02cb") + "`"


def permissions(interaction: discord.Interaction) -> discord.Permissions:
    return getattr(interaction.user, "guild_permissions", discord.Permissions.none())


def can_overwrite(interaction: discord.Interaction, tag: dict) -> bool:
    return tag["owner_id"] == interaction.user.id or permissions(interaction).administrator


def timestamp(value) -> str:
    seconds = int(value.timestamp())
    return f"<t:{seconds}:R> (<t:{seconds}:F>)"


def tag_content(tag: dict) -> str:
    return tag["reference_content"] or tag["content"]


def prefix_argument(value: str, *, consume: bool = False) -> tuple[str, str] | None:
    value = value.strip()
    if not value:
        return None
    if consume:
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace(r"\"", '"').replace(r"\\", "\\")
        return value, ""
    if value[0] != '"':
        argument, _, rest = value.partition(" ")
        return argument, rest.strip()
    escaped = False
    argument = []
    for index, character in enumerate(value[1:], 1):
        if escaped:
            argument.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == '"':
            return "".join(argument), value[index + 1 :].strip()
        else:
            argument.append(character)
    return None


def message_event(message: discord.Message):
    return SimpleNamespace(guild_id=message.guild.id, user=message.author)


class TagModal(discord.ui.Modal):
    def __init__(self, bot, guild_id: int, owner_id: int, name: str, tag: dict | None = None):
        super().__init__(title=t("modal.edit" if tag else "modal.create")[:45], timeout=900)
        self.bot, self.guild_id, self.owner_id, self.name, self.tag = (
            bot,
            guild_id,
            owner_id,
            name,
            tag,
        )
        self.content = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=MAX_CONTENT,
            default=tag["content"] if tag and not tag["reference_id"] else None,
            placeholder=t("modal.content_placeholder"),
        )
        self.upload = discord.ui.FileUpload(required=False, min_values=0, max_values=MAX_FILES)
        self.add_item(discord.ui.Label(text=t("modal.content"), component=self.content))
        self.add_item(
            discord.ui.Label(
                text=t("modal.files", max_media_mb=bot.max_media_mb),
                description=t("modal.files_hint"),
                component=self.upload,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await confirm_save(
            self.bot,
            interaction,
            self.name,
            self.content.value.strip(),
            self.upload.values,
            self.tag,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await report_error(interaction, error)


class RecentTagModal(discord.ui.Modal):
    def __init__(self, bot, owner_id: int, message: discord.Message, tags: list[dict]):
        super().__init__(title=t("recent.title"), timeout=CONFIRM_TIMEOUT)
        self.bot, self.owner_id, self.message = bot, owner_id, message
        self.tag_ids = {row["id"] for row in tags}
        self.tag_select = discord.ui.Select(
            options=[discord.SelectOption(label=row["name"], value=str(row["id"])) for row in tags]
        )
        self.arguments = discord.ui.TextInput(required=False, max_length=4000)
        self.upload = discord.ui.FileUpload(required=False, min_values=0, max_values=1)
        self.add_item(discord.ui.Label(text=t("recent.tag"), component=self.tag_select))
        self.add_item(discord.ui.Label(text=t("option.arguments"), component=self.arguments))
        self.add_item(discord.ui.Label(text=t("option.attachment"), component=self.upload))

    async def on_submit(self, interaction: discord.Interaction):
        if (
            interaction.user.id != self.owner_id
            or interaction.guild_id != self.message.guild.id
            or interaction.channel_id != self.message.channel.id
        ):
            await reply(interaction, t("error.owner"))
            return
        if not can_send_tag(interaction):
            await reply(interaction, t("error.recent_permissions"))
            return
        tag_id = int(self.tag_select.values[0])
        tag = await self.bot.db.get_tag_by_id(tag_id) if tag_id in self.tag_ids else None
        if tag is None or tag["guild_id"] != interaction.guild_id:
            await reply(interaction, t("error.unknown_tag"))
            return
        await execute(
            self.bot,
            interaction,
            tag,
            self.arguments.value,
            self.upload.values[0] if self.upload.values else None,
            reply_message=self.message,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await report_error(interaction, error)


def can_send_tag(interaction: discord.Interaction) -> bool:
    channel = interaction.channel
    if not isinstance(channel, discord.abc.GuildChannel | discord.Thread):
        return False
    for member in (interaction.user, interaction.guild.me):
        allowed = channel.permissions_for(member)
        send = (
            allowed.send_messages_in_threads
            if isinstance(channel, discord.Thread)
            else (allowed.send_messages)
        )
        if not (allowed.view_channel and send):
            return False
    return True


async def save_tag(
    bot,
    interaction: discord.Interaction,
    name: str,
    content: str,
    uploads: list[tuple[str, bytes]],
    expected: dict | None,
) -> str:
    guild_id, user_id = interaction.guild_id, interaction.user.id
    current = await bot.db.get_tag(guild_id, name)
    if current and not can_overwrite(interaction, current):
        return t("error.tag_exists")
    if current and is_group(current):
        return t("error.group_content")
    if (current and current["id"]) != (expected and expected["id"]):
        return t("error.tag_changed")
    existing = await bot.db.file_names(current["id"]) if current else []
    kept = referenced_files(content) & set(existing)
    taken, new_files = set(kept), {}
    for filename, data in uploads:
        filename = unique_filename(filename, taken)
        taken.add(filename)
        new_files[filename] = data
    if new_files:
        content = "\n".join(part for part in (content, attach_references(list(new_files))) if part)
    if not content:
        return t("error.empty_content")
    if len(content) > MAX_CONTENT:
        return t("error.content_length", limit=MAX_CONTENT)
    if len(taken) > MAX_FILES:
        return t("error.file_count", limit=MAX_FILES)
    missing = referenced_files(content) - taken
    if missing:
        return t("error.missing_files", files=", ".join(sorted(missing)))
    try:
        if current:
            await bot.db.save_content(current["id"], content, new_files, kept)
        else:
            await bot.db.create_tag(new_id(), guild_id, name, content, user_id, new_files)
    except psycopg.errors.UniqueViolation:
        return t("error.tag_exists")
    return t("result.edited" if current else "result.created", name=code(name))


async def execute(
    bot,
    interaction: discord.Interaction,
    tag: dict,
    arguments: str,
    attachment,
    title="",
    lead_embed=None,
    reply_message: discord.Message | None = None,
):
    preview = lead_embed is not None
    lead = [lead_embed] if preview else []
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=preview, thinking=True)
    if not preview:
        await bot.db.use(tag, interaction.guild_id, interaction.user.id)
    if is_group(tag):
        tag = await bot.db.random_member(target_id(tag))
        if tag is None:
            await render.send_error(
                interaction,
                "\n\n".join(part for part in (title, t("error.group_empty")) if part),
                lead,
            )
            return
        if not preview:
            await bot.db.use(tag, interaction.guild_id, interaction.user.id)
    rt = tagscript.Runtime(
        client=bot,
        guild=interaction.guild,
        channel=interaction.channel,
        user=interaction.user,
        tag=tag,
        attachment_url=attachment.url if attachment else None,
        max_file_size=interaction.filesize_limit,
        preview=preview,
    )
    try:
        result = await tagscript.parse(rt, tag_content(tag), arguments)
    except tagscript.TagError as error:
        await render.send_error(
            interaction, "\n\n".join(part for part in (title, str(error)) if part), lead
        )
        return
    await render.send_result(interaction, rt, result, title, lead_embed, reply_message)


async def execute_message(
    bot, message: discord.Message, tag: dict, arguments: str, attachment, title: str = ""
):
    await bot.db.use(tag, message.guild.id, message.author.id)
    if is_group(tag):
        tag = await bot.db.random_member(target_id(tag))
        if tag is None:
            await render.send_message_error(
                message, "\n\n".join(part for part in (title, t("error.group_empty")) if part)
            )
            return
        await bot.db.use(tag, message.guild.id, message.author.id)
    rt = tagscript.Runtime(
        client=bot,
        guild=message.guild,
        channel=message.channel,
        user=message.author,
        tag=tag,
        attachment_url=attachment.url if attachment else None,
        max_file_size=message.guild.filesize_limit,
    )
    try:
        result = await tagscript.parse(rt, tag_content(tag), arguments)
    except tagscript.TagError as error:
        await render.send_message_error(
            message, "\n\n".join(part for part in (title, str(error)) if part)
        )
        return
    await render.send_message_result(message, rt, result, title)


def info_embed(
    tag: dict, file_names: list[str], members: list[dict] | None = None
) -> discord.Embed:
    embed = discord.Embed(title=tag["name"][:256], colour=discord.Colour.blurple())
    if tag["reference_id"]:
        embed.description = t("info.alias", name=code(tag["reference_name"]))
    elif tag["is_group"]:
        names = ", ".join(code(member["name"]) for member in members or [])
        embed.description = t(
            "info.group", count=len(members or []), tags=names or t("info.group_none")
        )[:4096]
    else:
        embed.description = "```\n" + tag["content"].replace("```", "`\u200b``")[:4000] + "\n```"
    lines = [t("info.created", time=timestamp(tag["created_at"]))]
    if tag["edited_at"]:
        lines.append(t("info.edited", time=timestamp(tag["edited_at"])))
    lines += [
        t("info.id", id=code(str(tag["id"]))),
        t("info.owner", owner=f"<@{tag['owner_id']}>"),
        t("info.uses", uses=f"{tag['uses']:,}"),
    ]
    if file_names:
        lines.append(t("info.files", files=", ".join(code(name) for name in file_names)))
    embed.add_field(name=t("info.title"), value="\n".join(lines)[:1024])
    return embed


class OwnedView(discord.ui.View):
    def __init__(self, owner_id: int, timeout: float):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(t("error.owner"), ephemeral=True)
            return False
        return True

    async def on_error(self, interaction, error, item):
        await report_error(interaction, error)


async def finish_save(bot, interaction, name, content, attachments, expected):
    await interaction.response.defer()
    try:
        uploads = await read_uploads(attachments, bot.max_media_bytes)
    except MediaError as error:
        await interaction.edit_original_response(
            content=t(str(error), max_media_mb=bot.max_media_mb), view=None
        )
        return
    message = await save_tag(bot, interaction, name, content, uploads, expected)
    await interaction.edit_original_response(content=message, view=None)


class SaveConfirmation(OwnedView):
    def __init__(self, bot, owner_id, guild_id, name, content, attachments, expected):
        super().__init__(owner_id, CONFIRM_TIMEOUT)
        self.bot, self.guild_id, self.name = bot, guild_id, name
        self.content, self.attachments, self.expected = content, attachments, expected
        if expected is not None:
            self.remove_item(self.rename)

    @discord.ui.button(label=t("action.continue"), style=discord.ButtonStyle.primary)
    async def confirm(self, interaction, button):
        if self.is_finished() or interaction.guild_id != self.guild_id:
            await interaction.response.defer()
            return
        if self.content is None:
            await interaction.response.send_modal(
                TagModal(self.bot, self.guild_id, self.owner_id, self.name, self.expected)
            )
            return
        self.stop()
        attachments, self.attachments = self.attachments, []
        await finish_save(
            self.bot, interaction, self.name, self.content, attachments, self.expected
        )

    @discord.ui.button(label=t("action.rename"), style=discord.ButtonStyle.secondary)
    async def rename(self, interaction, button):
        await interaction.response.send_modal(RenameModal(self, self.name))

    @discord.ui.button(label=t("action.cancel"), style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self.stop()
        self.attachments = []
        await interaction.response.edit_message(content=t("save.cancelled"), view=None)

    async def on_timeout(self):
        self.attachments = []
        if self.message:
            try:
                await self.message.edit(content=t("create.expired"), view=None)
            except discord.HTTPException:
                pass


async def confirm_save(bot, interaction, name, content, uploads, expected):
    view = SaveConfirmation(
        bot, interaction.user.id, interaction.guild_id, name, content, uploads, expected
    )
    await reply(
        interaction,
        t("save.confirm_edit" if expected else "save.confirm_create", name=code(name)),
        view=view,
    )
    view.message = await interaction.original_response()


def name_candidates(name: str, count: int = 100) -> list[str]:
    match = re.fullmatch(r"(.*?)(\d+)", name, re.DOTALL)
    base, start = (match[1], int(match[2]) + 1) if match else (name, 1)
    candidates = []
    for number in range(start, start + count):
        suffix = str(number)
        candidates.append(base[: MAX_NAME - len(suffix)] + suffix)
    return candidates


async def available_names(bot, guild_id: int, name: str, count: int = 2) -> list[str]:
    candidates = name_candidates(name)
    taken = await bot.db.taken_names(guild_id, candidates)
    free = [candidate for candidate in candidates if candidate.lower() not in taken][:count]
    if not free:
        suffix = str(new_id())
        free.append(name[: MAX_NAME - len(suffix)] + suffix)
    return free


async def available_name(bot, guild_id: int, name: str) -> str:
    return (await available_names(bot, guild_id, name, 1))[0]


def valid_name(name: str) -> str | None:
    name = clean_name(name)
    return name if 0 < len(name) <= MAX_NAME else None


class RenameModal(discord.ui.Modal):
    def __init__(self, source, name: str):
        super().__init__(title=t("modal.rename")[:45], timeout=CONFIRM_TIMEOUT)
        self.source = source
        self.new_name = discord.ui.TextInput(max_length=MAX_NAME, default=name[:MAX_NAME])
        self.add_item(discord.ui.Label(text=t("modal.name"), component=self.new_name))

    async def on_submit(self, interaction: discord.Interaction):
        await choose_name(self.source, interaction, self.new_name.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await report_error(interaction, error)


async def choose_name(old, interaction: discord.Interaction, value: str):
    name = valid_name(value)
    if name is None:
        await reply(interaction, t("error.name", limit=MAX_NAME))
        return
    tag = await old.bot.db.get_tag(interaction.guild_id, name)
    if old.is_finished():
        await interaction.response.defer()
        return
    if tag:
        text, view = await create_conflict(
            old.bot, interaction, tag, name, old.content, old.attachments
        )
    else:
        view = SaveConfirmation(
            old.bot, old.owner_id, interaction.guild_id, name, old.content, old.attachments, None
        )
        text = t("save.confirm_create", name=code(name))
    if old.is_finished():
        view.stop()
        await interaction.response.defer()
        return
    old.stop()
    old.attachments = []
    view.message = old.message
    await interaction.response.edit_message(content=text, view=view)


class CreateConflict(OwnedView):
    def __init__(
        self,
        bot,
        owner_id: int,
        existing: dict | None,
        name: str,
        suggestions: list[str],
        content: str | None,
        attachments=(),
    ):
        super().__init__(owner_id, CONFIRM_TIMEOUT)
        self.bot, self.existing, self.name, self.content = bot, existing, name, content
        self.attachments = list(attachments)
        self.choose.options = [
            discord.SelectOption(label=suggestion, value=suggestion) for suggestion in suggestions
        ]
        if existing is None:
            self.remove_item(self.overwrite)

    async def save(self, interaction: discord.Interaction, name: str, expected: dict | None):
        if self.content is None:
            await interaction.response.send_modal(
                TagModal(self.bot, interaction.guild_id, self.owner_id, name, expected)
            )
            return
        if self.is_finished():
            await interaction.response.defer()
            return
        self.stop()
        attachments, self.attachments = self.attachments, []
        await finish_save(self.bot, interaction, name, self.content, attachments, expected)

    @discord.ui.select(placeholder=t("action.pick_name"), row=0)
    async def choose(self, interaction, select):
        await self.save(interaction, select.values[0], None)

    @discord.ui.button(label=t("action.overwrite"), style=discord.ButtonStyle.danger, row=1)
    async def overwrite(self, interaction, button):
        await self.save(interaction, self.existing["name"], self.existing)

    @discord.ui.button(label=t("action.custom_name"), style=discord.ButtonStyle.primary, row=1)
    async def custom_name(self, interaction, button):
        await interaction.response.send_modal(RenameModal(self, self.name))

    @discord.ui.button(label=t("action.cancel"), style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction, button):
        self.stop()
        self.attachments = []
        await interaction.response.edit_message(content=t("create.cancelled"), view=None)

    async def on_timeout(self):
        self.attachments = []
        if self.message:
            try:
                await self.message.edit(content=t("create.expired"), view=None)
            except discord.HTTPException:
                pass


async def create_conflict(bot, event, tag: dict, name: str, content: str | None, attachments=()):
    suggestions = await available_names(bot, event.guild_id, name)
    allowed = can_overwrite(event, tag) and not is_group(tag)
    view = CreateConflict(
        bot, event.user.id, tag if allowed else None, name, suggestions, content, attachments
    )
    if is_group(tag):
        key = "create.conflict_group"
    elif tag["owner_id"] == event.user.id:
        key = "create.conflict_own"
    else:
        key = "create.conflict_admin" if allowed else "create.conflict_other"
    text = t(
        key,
        name=code(tag["name"]),
        suggestion=code(suggestions[0]),
        owner=f"<@{tag['owner_id']}>",
    )
    return text, view


class ForceRemove(OwnedView):
    def __init__(self, bot, owner_id: int, tag: dict, embed: discord.Embed):
        super().__init__(owner_id, CONFIRM_TIMEOUT)
        self.bot, self.tag, self.embed = bot, tag, embed

    @discord.ui.button(label=t("action.continue"), style=discord.ButtonStyle.primary)
    async def confirm(self, interaction, button):
        if self.is_finished() or interaction.guild_id != self.tag["guild_id"]:
            return
        self.stop()
        await interaction.response.defer()
        current = await self.bot.db.get_tag(interaction.guild_id, self.tag["name"])
        if current is None or current["id"] != self.tag["id"]:
            await interaction.edit_original_response(
                content=t("error.tag_changed"), embed=None, view=None
            )
            return
        member_permissions = permissions(interaction)
        if current["owner_id"] != interaction.user.id and not (
            member_permissions.manage_guild or member_permissions.ban_members
        ):
            await interaction.edit_original_response(
                content=t("error.not_owner"), embed=None, view=None
            )
            return
        await self.bot.db.delete_matching(interaction.guild_id, [current["id"]])
        self.embed.title = t("result.removed", name=code(current["name"]))
        self.embed.colour = discord.Colour.green()
        await interaction.edit_original_response(embed=self.embed, view=None)

    @discord.ui.button(label=t("action.cancel"), style=discord.ButtonStyle.danger)
    async def cancel(self, interaction, button):
        self.stop()
        self.embed.title = t("remove.cancelled")
        self.embed.colour = discord.Colour.red()
        await interaction.response.edit_message(embed=self.embed, view=None)

    async def on_timeout(self):
        if self.message:
            self.embed.colour = discord.Colour.red()
            self.embed.set_footer(text=t("remove.expired"))
            try:
                await self.message.edit(embed=self.embed, view=None)
            except discord.HTTPException:
                pass


class RemoveAll(OwnedView):
    def __init__(self, bot, owner_id: int, guild_id: int, tags: list, options: dict, scope: str):
        super().__init__(owner_id, REMOVE_ALL_TIMEOUT)
        self.bot, self.guild_id, self.tags, self.options, self.scope = (
            bot,
            guild_id,
            tags,
            options,
            scope,
        )

    @discord.ui.button(label=t("action.continue"), style=discord.ButtonStyle.primary)
    async def confirm(self, interaction, button):
        if self.is_finished() or interaction.guild_id != self.guild_id:
            return
        self.stop()
        await interaction.response.defer()
        if not permissions(interaction).administrator and self.options["userId"] != str(
            interaction.user.id
        ):
            await interaction.edit_original_response(content=t("error.remove_others"), view=None)
            return
        tag_ids = [tag["id"] for tag in self.tags]
        if permissions(interaction).administrator:
            count = await self.bot.db.delete_matching(self.guild_id, tag_ids)
        else:
            count = await self.bot.db.delete_matching_owned(
                self.guild_id, tag_ids, interaction.user.id
            )
            if count is None:
                await interaction.edit_original_response(
                    content=t("error.remove_others"), view=None
                )
                return
        await interaction.edit_original_response(
            content=t("remove_all.done", count=f"{count:,}", scope=self.scope), view=None
        )

    @discord.ui.button(label=t("action.cancel"), style=discord.ButtonStyle.danger)
    async def cancel(self, interaction, button):
        self.stop()
        await interaction.response.edit_message(
            content=t("remove_all.cancelled", count=f"{len(self.tags):,}", scope=self.scope),
            view=None,
        )

    @discord.ui.button(label=t("action.export"), style=discord.ButtonStyle.secondary)
    async def export(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await send_export(self.bot, interaction, self.tags, self.options)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(
                    content=t("remove_all.timeout", count=f"{len(self.tags):,}", scope=self.scope),
                    view=None,
                )
            except discord.HTTPException:
                pass


class ListView(OwnedView):
    def __init__(
        self,
        owner_id: int,
        title: str,
        lines: list[str],
        footer: str = "",
        page_size: int = LIST_PAGE_SIZE,
    ):
        super().__init__(owner_id, CONFIRM_TIMEOUT)
        self.title, self.footer, self.index, self.page_size = title, footer, 0, page_size
        self.pages = [lines[index : index + page_size] for index in range(0, len(lines), page_size)]
        self.update()

    def update(self):
        self.previous.disabled = self.index == 0
        self.next.disabled = self.index >= len(self.pages) - 1

    def embed(self) -> discord.Embed:
        start = self.index * self.page_size
        lines = [f"{start + number}. {line}" for number, line in enumerate(self.page(), 1)]
        embed = discord.Embed(title=self.title[:256], description="\n".join(lines)[:4096])
        footer = t("list.footer", page=self.index + 1, pages=len(self.pages))
        embed.set_footer(text=" | ".join(part for part in (footer, self.footer) if part)[:2048])
        return embed

    def page(self) -> list[str]:
        return self.pages[self.index]

    @discord.ui.button(label="<", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.index -= 1
        self.update()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label=">", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        self.index += 1
        self.update()
        await interaction.response.edit_message(embed=self.embed(), view=self)


class PrunePreview(ListView):
    def __init__(self, bot, owner_id: int, guild_id: int, tags: list, title: str, scope: str):
        super().__init__(owner_id, title, [tag_line(tag) for tag in tags], scope, DETAIL_PAGE_SIZE)
        self.bot, self.guild_id, self.tags, self.scope = bot, guild_id, tags, scope

    @discord.ui.button(label=t("action.prune"), style=discord.ButtonStyle.danger, row=1)
    async def confirm(self, interaction, button):
        self.stop()
        await interaction.response.defer()
        count = await self.bot.db.delete_matching(self.guild_id, [tag["id"] for tag in self.tags])
        await interaction.edit_original_response(
            content=t("prune.done", count=f"{count:,}", scope=self.scope), embed=None, view=None
        )

    @discord.ui.button(label=t("action.cancel"), style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction, button):
        self.stop()
        await interaction.response.edit_message(
            content=t("prune.cancelled", count=f"{len(self.tags):,}"), embed=None, view=None
        )

    @discord.ui.button(label=t("action.export"), style=discord.ButtonStyle.secondary, row=1)
    async def export(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await send_export(self.bot, interaction, self.tags, {"prune": self.scope})

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(
                    content=t("prune.timeout", count=f"{len(self.tags):,}"), embed=None, view=None
                )
            except discord.HTTPException:
                pass


def scope_text(user) -> str:
    return t("scope.user", user=user.mention) if user else t("scope.server")


async def prefix_reply(message: discord.Message, content: str, **kwargs):
    return await render.respond(message, content, **kwargs)


def is_prefix_command(content: str) -> bool:
    return bool(UNDO_PATTERN.match(content) or TAG_PATTERN.match(content))


def is_current_edit(message: discord.Message) -> bool:
    """True for a fresh edit, inside the edit window, of a command or a command's reply."""
    if message.guild is None or message.author.bot or message.edited_at is None:
        return False
    if (message.edited_at - message.created_at).total_seconds() > render.REPLY_WINDOW:
        return False
    if (discord.utils.utcnow() - message.edited_at).total_seconds() > EDIT_EVENT_SECONDS:
        return False
    return render.REPLIES.get(message.id) is not None or is_prefix_command(message.content)


async def handle_prefix_edit(bot, message: discord.Message, before: str | None):
    """Rerun an edited command, updating its earlier reply instead of sending a new one.

    The caller holds REPLIES.guard(message.id)."""
    entry = render.REPLIES.get(message.id)
    if entry is not None:
        before = entry.content
    if before is None or before == message.content:
        return
    await handle_prefix_message(bot, message)
    if entry is not None and render.REPLIES.get(message.id) is entry:
        await render.REPLIES.discard(message.id)


async def handle_prefix_delete(message_id: int):
    """A deleted command removes its reply; a deleted reply is no longer tracked."""
    if render.REPLIES.get(message_id) is None and message_id not in render.REPLIES.locks:
        render.REPLIES.forget_reply(message_id)
        return
    async with render.REPLIES.guard(message_id):
        await render.REPLIES.discard(message_id)


async def prefix_uploads(bot, message: discord.Message, attachments=None):
    attachments = message.attachments if attachments is None else attachments
    if any(not 0 < attachment.size <= bot.max_media_bytes for attachment in attachments):
        await prefix_reply(message, t("error.media_size", max_media_mb=bot.max_media_mb))
        return None
    return attachments


async def undo_commands(channel, me, user_id: int, amount: int, exclude_id: int | None = None):
    """Delete the replies to a user's last commands in a channel, newest first.

    Returns the notice text and whether the bot can delete the user's messages."""
    can_delete = channel.permissions_for(me).manage_messages
    prefixed = interactions = failed = 0
    for command_id in render.REPLIES.latest(user_id, channel.id):
        if prefixed + interactions + failed >= amount:
            break
        entry = render.REPLIES.get(command_id)
        if entry is None or command_id == exclude_id or command_id in render.REPLIES.locks:
            continue
        if not await render.REPLIES.discard(command_id):
            failed += 1
            continue
        if not entry.prefixed:
            interactions += 1
            continue
        prefixed += 1
        if can_delete:
            try:
                await channel.get_partial_message(command_id).delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                failed += 1
    if not (prefixed or interactions or failed):
        return t("undo.none"), can_delete
    if prefixed and interactions:
        text = t("undo.done_both", interactions=interactions, prefixed=prefixed)
    elif interactions:
        text = t("undo.done_interactions", interactions=interactions)
    else:
        text = t("undo.done", count=prefixed)
    if failed:
        text = t("undo.failed", text=text, failed=failed)
    return text, can_delete


async def prefix_undo(message: discord.Message, value: str):
    amount = value.strip() or "1"
    if not amount.isdecimal():
        await prefix_reply(message, t("prefix.usage", usage=".undo [amount]"))
        return
    amount = max(1, min(int(amount.lstrip("0")[:9] or "0"), MAX_UNDO))
    text, can_delete = await undo_commands(
        message.channel, message.guild.me, message.author.id, amount, message.id
    )
    options = {
        "allowed_mentions": discord.AllowedMentions.none(),
        "delete_after": UNDO_NOTICE_SECONDS,
    }
    if can_delete:
        await message.channel.send(text, **options)
        try:
            await message.delete()
        except discord.HTTPException:
            log.debug("Could not delete the undo command message")
    else:
        await message.reply(text, mention_author=False, **options)


async def handle_prefix_message(bot, message: discord.Message) -> bool:
    if message.guild is None or message.author.bot:
        return False
    undo = UNDO_PATTERN.match(message.content)
    if undo:
        await prefix_undo(message, undo[1] or "")
        return True
    match = TAG_PATTERN.match(message.content)
    if match is None:
        return False
    value = (match[2] or "").strip()
    first = prefix_argument(value)
    if first is None:
        await prefix_reply(message, t("prefix.usage", usage=".t <tag> [arguments]"))
        return True
    command = PREFIX_COMMANDS.get(first[0].lower())
    if command is None:
        await prefix_show(bot, message, first[0], first[1])
        return True
    if command == "show":
        tag_argument = prefix_argument(first[1])
        if tag_argument is None:
            await prefix_reply(message, t("prefix.usage", usage=".t show <tag> [arguments]"))
        else:
            await prefix_show(bot, message, tag_argument[0], tag_argument[1])
        return True
    handlers = {
        "alias": prefix_alias,
        "create": prefix_create,
        "edit": prefix_edit,
        "info": prefix_info,
        "list": prefix_list,
        "random": prefix_random,
        "remove": prefix_remove,
        "rename": prefix_rename,
        "search": prefix_search,
    }
    await handlers[command](bot, message, first[1])
    return True


async def prefix_find(bot, message: discord.Message, name: str):
    tag = await bot.db.get_tag(message.guild.id, clean_name(name))
    if tag is None:
        await prefix_reply(message, t("error.unknown_tag"))
    return tag


async def prefix_show(bot, message: discord.Message, name: str, arguments: str):
    tag = await prefix_find(bot, message, name)
    if tag is not None:
        await execute_message(
            bot,
            message,
            tag,
            arguments,
            message.attachments[0] if message.attachments else None,
        )


async def prefix_create(bot, message: discord.Message, value: str):
    parsed = prefix_argument(value)
    if parsed is None:
        await prefix_reply(message, t("prefix.usage", usage=".t create <tag> [content]"))
        return
    name, content = clean_name(parsed[0]), parsed[1].strip()
    if not 0 < len(name) <= MAX_NAME:
        await prefix_reply(message, t("error.name", limit=MAX_NAME))
        return
    source = message
    if not content and not message.attachments and message.reference:
        reference = message.reference
        if reference.type == discord.MessageReferenceType.default:
            source = reference.resolved
            if source is None:
                try:
                    source = await message.channel.fetch_message(reference.message_id)
                except discord.HTTPException:
                    await prefix_reply(message, t("error.create_reply"))
                    return
            if isinstance(source, discord.DeletedReferencedMessage):
                await prefix_reply(message, t("error.create_reply"))
                return
            text = "" if source.attachments else source.content.strip()
            urls = [
                media.url
                for embed in source.embeds
                for media in (embed.image, embed.thumbnail, embed.video)
                if media.url and media.url not in text
            ]
            parts = [text, *dict.fromkeys(urls)]
            content = "\n".join(part for part in parts if part)
    if not content and not source.attachments:
        await prefix_reply(message, t("prefix.usage", usage=".t create <tag> [content]"))
        return
    uploads = await prefix_uploads(bot, message, source.attachments)
    if uploads is None:
        return
    tag = await bot.db.get_tag(message.guild.id, name)
    if tag:
        text, view = await create_conflict(bot, message_event(message), tag, name, content, uploads)
    else:
        view = SaveConfirmation(
            bot, message.author.id, message.guild.id, name, content, uploads, None
        )
        text = t("save.confirm_create", name=code(name))
    view.message = await prefix_reply(message, text, view=view)


async def prefix_edit(bot, message: discord.Message, value: str):
    parsed = prefix_argument(value)
    if parsed is None or not parsed[1].strip():
        await prefix_reply(message, t("prefix.usage", usage=".t edit <tag> <content>"))
        return
    tag = await prefix_find(bot, message, parsed[0])
    if tag is None:
        return
    event = message_event(message)
    if not can_overwrite(event, tag):
        await prefix_reply(message, t("error.not_owner"))
        return
    if is_group(tag):
        await prefix_reply(message, t("error.group_content"))
        return
    uploads = await prefix_uploads(bot, message)
    if uploads is not None:
        view = SaveConfirmation(
            bot, message.author.id, message.guild.id, tag["name"], parsed[1].strip(), uploads, tag
        )
        view.message = await prefix_reply(
            message, t("save.confirm_edit", name=code(tag["name"])), view=view
        )


async def prefix_alias(bot, message: discord.Message, value: str):
    parsed = prefix_argument(value)
    target_argument = prefix_argument(parsed[1], consume=True) if parsed else None
    if parsed is None or target_argument is None:
        await prefix_reply(message, t("prefix.usage", usage=".t alias <new name> <tag>"))
        return
    name = clean_name(parsed[0])
    target = await prefix_find(bot, message, target_argument[0])
    if target is None:
        return
    if not 0 < len(name) <= MAX_NAME:
        await prefix_reply(message, t("error.name", limit=MAX_NAME))
        return
    reference_id = target["reference_id"] or target["id"]
    reference_name = target["reference_name"] or target["name"]
    if reference_name.lower() == name.lower():
        await prefix_reply(message, t("result.ok"))
        return
    existing = await bot.db.get_tag(message.guild.id, name)
    if existing and existing["owner_id"] != message.author.id:
        await prefix_reply(message, t("error.alias_not_owner"))
        return
    if existing and not existing["reference_id"]:
        await prefix_reply(message, t("error.alias_regular"))
        return
    try:
        await bot.db.save_alias(
            existing["id"] if existing else new_id(),
            message.guild.id,
            name,
            message.author.id,
            reference_id,
        )
    except psycopg.errors.UniqueViolation:
        await prefix_reply(message, t("error.tag_exists"))
        return
    await prefix_reply(message, t("result.alias", name=code(name), target=code(reference_name)))


async def prefix_rename(bot, message: discord.Message, value: str):
    parsed = prefix_argument(value)
    name_argument = prefix_argument(parsed[1], consume=True) if parsed else None
    if parsed is None or name_argument is None:
        await prefix_reply(message, t("prefix.usage", usage=".t rename <tag> <new name>"))
        return
    tag = await prefix_find(bot, message, parsed[0])
    if tag is None:
        return
    if not can_overwrite(message_event(message), tag):
        await prefix_reply(message, t("error.not_owner"))
        return
    name = clean_name(name_argument[0])
    if not 0 < len(name) <= MAX_NAME:
        await prefix_reply(message, t("error.name", limit=MAX_NAME))
        return
    if tag["name"] == name:
        await prefix_reply(message, t("result.ok"))
        return
    try:
        await bot.db.rename(tag["id"], name)
    except psycopg.errors.UniqueViolation:
        await prefix_reply(message, t("error.tag_exists"))
        return
    await prefix_reply(message, t("result.renamed", old=code(tag["name"]), new=code(name)))


async def prefix_info(bot, message: discord.Message, value: str):
    parsed = prefix_argument(value, consume=True)
    if parsed is None:
        await prefix_reply(message, t("prefix.usage", usage=".t info <tag>"))
        return
    tag = await prefix_find(bot, message, parsed[0])
    if tag is None:
        return
    names = await bot.db.file_names(tag["reference_id"] or tag["id"])
    members = await bot.db.group_members(tag["id"]) if tag["is_group"] else None
    await prefix_reply(message, "", embed=info_embed(tag, names, members))


async def prefix_list(bot, message: discord.Message, value: str):
    user = message.author
    if value:
        match = re.fullmatch(r"<@!?(\d+)>|(\d+)", value.strip())
        user_id = int(next(part for part in match.groups() if part)) if match else None
        user = (
            message.guild.get_member(user_id)
            if user_id
            else message.guild.get_member_named(value.strip())
        )
        if user is None or user.bot:
            await prefix_reply(message, t("prefix.user_not_found"))
            return
    tags = await bot.db.search(message.guild.id, owner_id=user.id)
    if not tags:
        await prefix_reply(message, t("error.no_tags"))
        return
    title = t("list.user", user=user.display_name, count=f"{len(tags):,}")
    await prefix_send_list(message, title, tags)


async def prefix_search(bot, message: discord.Message, value: str):
    parsed = prefix_argument(value, consume=True)
    if parsed is None or not parsed[0]:
        await prefix_list(bot, message, "")
        return
    tags = await bot.db.search(message.guild.id, name=parsed[0])
    if not tags:
        await prefix_reply(message, t("error.no_matching_tags"))
        return
    title = t("list.server", guild=message.guild.name, count=f"{len(tags):,}")
    await prefix_send_list(message, title, tags)


async def prefix_send_list(message: discord.Message, title: str, tags: list):
    view = ListView(message.author.id, title, [code(tag["name"]) for tag in tags])
    sent = await prefix_reply(
        message,
        "",
        embed=view.embed(),
        view=view if len(view.pages) > 1 else discord.utils.MISSING,
    )
    view.message = sent


async def prefix_random(bot, message: discord.Message, arguments: str):
    tag = await bot.db.random(message.guild.id)
    if tag is None:
        await render.send_message_error(message, t("error.no_tags"))
        return
    await execute_message(
        bot,
        message,
        tag,
        arguments,
        message.attachments[0] if message.attachments else None,
        t("show.random_title", name=code(tag["name"])),
    )


async def prefix_remove(bot, message: discord.Message, value: str):
    if value.lower().startswith("tag "):
        value = value[4:]
    parsed = prefix_argument(value, consume=True)
    if parsed is None:
        await prefix_reply(message, t("prefix.usage", usage=".t remove <tag>"))
        return
    tag = await prefix_find(bot, message, parsed[0])
    if tag is None:
        return
    member_permissions = message.author.guild_permissions
    if tag["owner_id"] != message.author.id and not (
        member_permissions.manage_guild or member_permissions.ban_members
    ):
        await prefix_reply(message, t("error.not_owner"))
        return
    embed = discord.Embed(
        title=t("remove.confirm_title"),
        description=t("remove.force_body", name=code(tag["name"])),
        colour=discord.Colour.blurple(),
    )
    view = ForceRemove(bot, message.author.id, tag, embed)
    view.message = await prefix_reply(message, "", embed=embed, view=view)


def format_size(size: int) -> str:
    for unit, scale in (("GB", 10**9), ("MB", 10**6), ("KB", 10**3)):
        if size >= scale:
            return f"{size / scale:,.2f} {unit}"
    return f"{size:,} B"


def since(days: int) -> datetime | None:
    return datetime.now(UTC) - timedelta(days=days) if days else None


def period_label(days: int) -> str:
    return t(f"period.{days}")


def last_used(value) -> str:
    return f"<t:{int(value.timestamp())}:R>" if value else t("stats.never")


def tag_line(row: dict) -> str:
    return t(
        "stats.row",
        name=code(row["name"]),
        owner=f"<@{row['owner_id']}>",
        uses=f"{row['ranked_uses']:,}",
        last_used=last_used(row["last_used_at"]),
        created=f"<t:{int(row['created_at'].timestamp())}:d>",
        size=format_size(row["size"]),
    )


def filter_text(owner=None, unused_days=None, max_uses=None, min_age_days=None, limit=None):
    parts = []
    if owner is not None:
        parts.append(t("prune.owner", owner=owner.display_name))
    if unused_days is not None:
        parts.append(t("prune.unused_days", days=unused_days))
    if max_uses is not None:
        parts.append(t("prune.max_uses", uses=max_uses))
    if min_age_days:
        parts.append(t("prune.min_age", days=min_age_days))
    if limit is not None:
        parts.append(t("prune.least_used", count=limit))
    return t("prune.scope", criteria=", ".join(parts)) if parts else ""


def ranked(rows: list[dict], line) -> str:
    return "\n".join(f"{index}. {line(row)}" for index, row in enumerate(rows, 1)) or t(
        "stats.none"
    )


def stats_embed(title: str, days: int) -> discord.Embed:
    embed = discord.Embed(
        title=title[:256], description=period_label(days), colour=discord.Colour.blurple()
    )
    embed.set_footer(text=t("stats.footer"))
    return embed


async def send_export(bot, interaction: discord.Interaction, tags: list, options: dict):
    tags = list(tags)
    included = {tag["id"] for tag in tags}
    groups = {}
    index = 0
    while index < len(tags):
        tag = tags[index]
        index += 1
        dependencies = [tag["reference_id"]] if tag["reference_id"] else []
        if tag["is_group"]:
            groups[tag["id"]] = [member["id"] for member in await bot.db.group_members(tag["id"])]
            dependencies += groups[tag["id"]]
        for dependency in dependencies:
            if dependency not in included:
                target = await bot.db.get_tag_by_id(dependency)
                if target:
                    tags.append(target)
                    included.add(target["id"])
    files = {}
    for tag in tags:
        names = await bot.db.file_names(tag["id"])
        files[tag["id"]] = {name: await bot.db.file(tag["id"], name) for name in names}
    filename, data = export_payload(interaction.guild_id, tags, files, options, groups)
    if len(data) > interaction.filesize_limit:
        await reply(interaction, t("error.export_size"))
        return
    await interaction.followup.send(
        t("export.done", count=f"{len(tags):,}"),
        file=discord.File(io.BytesIO(data), filename=filename),
        ephemeral=True,
    )


def register_commands(bot):
    group = app_commands.Group(name="tag", description=t("command.tag"), guild_only=True)
    list_group = app_commands.Group(name="list", description=t("command.list"), parent=group)
    remove_group = app_commands.Group(name="remove", description=t("command.remove"), parent=group)

    @app_commands.guild_only()
    async def recent_tags(interaction: discord.Interaction, message: discord.Message):
        if not can_send_tag(interaction):
            await reply(interaction, t("error.recent_permissions"))
            return
        tags = await bot.db.recent_tags(interaction.guild_id, interaction.user.id)
        if not tags:
            await reply(interaction, t("recent.empty"))
            return
        await interaction.response.send_modal(
            RecentTagModal(bot, interaction.user.id, message, tags)
        )

    bot.tree.add_command(
        app_commands.ContextMenu(name=t("command.recent_tags"), callback=recent_tags)
    )

    async def all_tags(interaction: discord.Interaction, current: str):
        names = await bot.db.autocomplete(interaction.guild_id, current)
        return [app_commands.Choice(name=name, value=name) for name in names]

    async def own_tags(interaction: discord.Interaction, current: str):
        owner_id = None if permissions(interaction).administrator else interaction.user.id
        names = await bot.db.autocomplete(interaction.guild_id, current, owner_id)
        return [app_commands.Choice(name=name, value=name) for name in names]

    async def find(interaction: discord.Interaction, name: str):
        tag = await bot.db.get_tag(interaction.guild_id, clean_name(name))
        if tag is None:
            await reply(interaction, t("error.unknown_tag"))
        return tag

    @bot.tree.command(name="t", description=t("command.show"))
    @app_commands.guild_only()
    @app_commands.describe(
        name=t("option.name"), arguments=t("option.arguments"), attachment=t("option.attachment")
    )
    @app_commands.autocomplete(name=all_tags)
    async def show(
        interaction: discord.Interaction,
        name: str,
        arguments: str | None = None,
        attachment: discord.Attachment | None = None,
    ):
        tag = await find(interaction, name)
        if tag:
            await execute(bot, interaction, tag, arguments or "", attachment)

    async def create_tag(interaction: discord.Interaction, name: str, content: str | None = None):
        name = valid_name(name)
        if name is None:
            await reply(interaction, t("error.name", limit=MAX_NAME))
            return
        tag = await bot.db.get_tag(interaction.guild_id, name)
        if tag:
            text, view = await create_conflict(
                bot, interaction, tag, name, None if content is None else content.strip()
            )
            await interaction.response.send_message(text, view=view, ephemeral=True)
            view.message = await interaction.original_response()
            return
        if content is None:
            await interaction.response.send_modal(
                TagModal(bot, interaction.guild_id, interaction.user.id, name)
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await confirm_save(bot, interaction, name, content.strip(), [], None)

    @group.command(name="create", description=t("command.create"))
    @app_commands.describe(name=t("option.new_name"), content=t("option.content"))
    async def create(interaction: discord.Interaction, name: str, content: str | None = None):
        await create_tag(interaction, name, content)

    @group.command(name="add", description=t("command.create"))
    @app_commands.describe(name=t("option.new_name"), content=t("option.content"))
    async def add(interaction: discord.Interaction, name: str, content: str | None = None):
        await create_tag(interaction, name, content)

    @group.command(name="edit", description=t("command.edit"))
    @app_commands.describe(name=t("option.name"), content=t("option.content"))
    @app_commands.autocomplete(name=own_tags)
    async def edit(interaction: discord.Interaction, name: str, content: str | None = None):
        tag = await find(interaction, name)
        if tag is None:
            return
        if not can_overwrite(interaction, tag):
            await reply(interaction, t("error.not_owner"))
            return
        if is_group(tag):
            await reply(interaction, t("error.group_content"))
            return
        if content is None:
            await interaction.response.send_modal(
                TagModal(bot, interaction.guild_id, interaction.user.id, tag["name"], tag)
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await confirm_save(bot, interaction, tag["name"], content.strip(), [], tag)

    @group.command(name="alias", description=t("command.alias"))
    @app_commands.describe(tag=t("option.alias_target"), name=t("option.new_name"))
    @app_commands.autocomplete(tag=all_tags)
    async def alias(interaction: discord.Interaction, tag: str, name: str):
        target = await find(interaction, tag)
        name = valid_name(name)
        if target is None:
            return
        if name is None:
            await reply(interaction, t("error.name", limit=MAX_NAME))
            return
        reference_id = target["reference_id"] or target["id"]
        reference_name = target["reference_name"] or target["name"]
        if reference_name.lower() == name.lower():
            await reply(interaction, t("result.ok"))
            return
        existing = await bot.db.get_tag(interaction.guild_id, name)
        if existing and existing["owner_id"] != interaction.user.id:
            await reply(interaction, t("error.alias_not_owner"))
            return
        if existing and not existing["reference_id"]:
            await reply(interaction, t("error.alias_regular"))
            return
        try:
            await bot.db.save_alias(
                existing["id"] if existing else new_id(),
                interaction.guild_id,
                name,
                interaction.user.id,
                reference_id,
            )
        except psycopg.errors.UniqueViolation:
            await reply(interaction, t("error.tag_exists"))
            return
        await reply(interaction, t("result.alias", name=code(name), target=code(reference_name)))

    @group.command(name="rename", description=t("command.rename"))
    @app_commands.describe(tag=t("option.name"), name=t("option.new_name"))
    @app_commands.autocomplete(tag=own_tags)
    async def rename(interaction: discord.Interaction, tag: str, name: str):
        current = await find(interaction, tag)
        name = valid_name(name)
        if current is None:
            return
        if not can_overwrite(interaction, current):
            await reply(interaction, t("error.not_owner"))
            return
        if name is None:
            await reply(interaction, t("error.name", limit=MAX_NAME))
            return
        if current["name"] == name:
            await reply(interaction, t("result.ok"))
            return
        try:
            await bot.db.rename(current["id"], name)
        except psycopg.errors.UniqueViolation:
            await reply(interaction, t("error.tag_exists"))
            return
        await reply(interaction, t("result.renamed", old=code(current["name"]), new=code(name)))

    @group.command(name="info", description=t("command.info"))
    @app_commands.describe(
        name=t("option.name"), arguments=t("option.arguments"), attachment=t("option.attachment")
    )
    @app_commands.autocomplete(name=all_tags)
    async def info(
        interaction: discord.Interaction,
        name: str,
        arguments: str | None = None,
        attachment: discord.Attachment | None = None,
    ):
        tag = await find(interaction, name)
        if tag is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        names = await bot.db.file_names(tag["reference_id"] or tag["id"])
        members = await bot.db.group_members(tag["id"]) if tag["is_group"] else None
        embed = info_embed(tag, names, members)
        await execute(bot, interaction, tag, arguments or "", attachment, lead_embed=embed)

    group_group = app_commands.Group(name="group", description=t("command.group"), parent=group)

    async def group_tags(interaction: discord.Interaction, current: str):
        names = await bot.db.autocomplete(interaction.guild_id, current, groups=True)
        return [app_commands.Choice(name=name, value=name) for name in names]

    async def group_member_tags(interaction: discord.Interaction, current: str):
        group_name = clean_name(getattr(interaction.namespace, "group", None) or "")
        target = await bot.db.get_tag(interaction.guild_id, group_name) if group_name else None
        if not target or not target["is_group"]:
            return []
        members = await bot.db.group_members(target["id"])
        lowered = current.lower()
        return [
            app_commands.Choice(name=member["name"], value=member["name"])
            for member in members
            if lowered in member["name"].lower()
        ][:25]

    async def managed_group(interaction: discord.Interaction, name: str):
        target = await find(interaction, name)
        if target is None:
            return None
        if not target["is_group"]:
            await reply(interaction, t("error.not_group", name=code(target["name"])))
            return None
        if not can_overwrite(interaction, target):
            await reply(interaction, t("error.not_owner"))
            return None
        return target

    async def group_member(interaction: discord.Interaction, name: str):
        member = await find(interaction, name)
        if member is not None and is_group(member):
            await reply(interaction, t("error.group_in_group"))
            return None
        return member

    @group_group.command(name="create", description=t("command.group_create"))
    @app_commands.describe(name=t("option.new_name"), tag=t("option.group_first_tag"))
    @app_commands.autocomplete(tag=all_tags)
    async def group_create(interaction: discord.Interaction, name: str, tag: str):
        name = valid_name(name)
        if name is None:
            await reply(interaction, t("error.name", limit=MAX_NAME))
            return
        member = await group_member(interaction, tag)
        if member is None:
            return
        if await bot.db.get_tag(interaction.guild_id, name):
            suggestion = await available_name(bot, interaction.guild_id, name)
            await reply(
                interaction,
                t("error.group_name_taken", name=code(name), suggestion=code(suggestion)),
            )
            return
        try:
            await bot.db.create_group(
                new_id(), interaction.guild_id, name, interaction.user.id, member["id"]
            )
        except psycopg.errors.UniqueViolation:
            await reply(interaction, t("error.tag_exists"))
            return
        await reply(
            interaction, t("result.group_created", name=code(name), tag=code(member["name"]))
        )

    @group_group.command(name="add", description=t("command.group_add"))
    @app_commands.describe(group=t("option.group"), tag=t("option.group_add_tag"))
    @app_commands.autocomplete(group=group_tags, tag=all_tags)
    async def group_add(interaction: discord.Interaction, group: str, tag: str):
        target = await managed_group(interaction, group)
        if target is None:
            return
        member = await group_member(interaction, tag)
        if member is None:
            return
        try:
            added = await bot.db.add_member(target["id"], member["id"])
        except GroupFullError:
            await reply(interaction, t("error.group_full", limit=MAX_GROUP_MEMBERS))
            return
        key = "result.group_added" if added else "error.group_has_tag"
        await reply(interaction, t(key, name=code(target["name"]), tag=code(member["name"])))

    @group_group.command(name="remove", description=t("command.group_remove"))
    @app_commands.describe(group=t("option.group"), tag=t("option.group_remove_tag"))
    @app_commands.autocomplete(group=group_tags, tag=group_member_tags)
    async def group_remove(interaction: discord.Interaction, group: str, tag: str):
        target = await managed_group(interaction, group)
        if target is None:
            return
        member = await find(interaction, tag)
        if member is None:
            return
        removed = await bot.db.remove_member(target["id"], member["id"])
        key = "result.group_removed" if removed else "error.group_missing_tag"
        await reply(interaction, t(key, name=code(target["name"]), tag=code(member["name"])))

    @group.command(name="random", description=t("command.random"))
    @app_commands.describe(
        arguments=t("option.arguments"),
        content=t("option.filter_content"),
        name=t("option.filter_name"),
        user=t("option.filter_user"),
        attachment=t("option.attachment"),
    )
    async def random_tag(
        interaction: discord.Interaction,
        arguments: str | None = None,
        content: app_commands.Range[str, 1, 1000] | None = None,
        name: app_commands.Range[str, 1, 64] | None = None,
        user: discord.User | None = None,
        attachment: discord.Attachment | None = None,
    ):
        await interaction.response.defer(thinking=True)
        tag = await bot.db.random(
            interaction.guild_id, name=name, content=content, owner_id=user and user.id
        )
        if tag is None:
            await render.send_error(
                interaction,
                t("error.no_matching_tags" if content or name or user else "error.no_tags"),
            )
            return
        title = t("show.random_title", name=code(tag["name"]))
        await execute(bot, interaction, tag, arguments or "", attachment, title)

    async def send_list(interaction, title: str, tags: list, filtered: bool):
        if not tags:
            await reply(interaction, t("error.no_matching_tags" if filtered else "error.no_tags"))
            return
        view = ListView(interaction.user.id, title, [code(tag["name"]) for tag in tags])
        await interaction.followup.send(
            embed=view.embed(),
            view=view if len(view.pages) > 1 else discord.utils.MISSING,
            ephemeral=True,
        )

    @list_group.command(name="server", description=t("command.list_server"))
    @app_commands.describe(
        content=t("option.filter_content"),
        name=t("option.filter_name"),
        user=t("option.filter_user"),
    )
    async def list_server(
        interaction: discord.Interaction,
        content: app_commands.Range[str, 1, 1000] | None = None,
        name: app_commands.Range[str, 1, 64] | None = None,
        user: discord.User | None = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        tags = await bot.db.search(
            interaction.guild_id, name=name, content=content, owner_id=user and user.id
        )
        title = t("list.server", guild=interaction.guild.name, count=f"{len(tags):,}")
        await send_list(interaction, title, tags, bool(content or name or user))

    @list_group.command(name="user", description=t("command.list_user"))
    @app_commands.describe(
        user=t("option.list_user"), content=t("option.filter_content"), name=t("option.filter_name")
    )
    async def list_user(
        interaction: discord.Interaction,
        user: discord.User | None = None,
        content: app_commands.Range[str, 1, 1000] | None = None,
        name: app_commands.Range[str, 1, 64] | None = None,
    ):
        user = user or interaction.user
        if user.bot:
            await reply(interaction, t("error.bot_tags"))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        tags = await bot.db.search(
            interaction.guild_id, name=name, content=content, owner_id=user.id
        )
        title = t("list.user", user=user.display_name, count=f"{len(tags):,}")
        await send_list(interaction, title, tags, bool(content or name))

    @remove_group.command(name="tag", description=t("command.remove_tag"))
    @app_commands.describe(name=t("option.name"))
    @app_commands.autocomplete(name=all_tags)
    async def remove_tag(interaction: discord.Interaction, name: str):
        tag = await find(interaction, name)
        if tag is None:
            return
        member_permissions = permissions(interaction)
        if tag["owner_id"] != interaction.user.id and not (
            member_permissions.manage_guild or member_permissions.ban_members
        ):
            await reply(interaction, t("error.not_owner"))
            return
        embed = discord.Embed(
            title=t("remove.confirm_title"),
            description=t("remove.force_body", name=code(tag["name"])),
            colour=discord.Colour.blurple(),
        )
        lines = [t("info.created", time=timestamp(tag["created_at"]))]
        if tag["edited_at"]:
            lines.append(t("info.edited", time=timestamp(tag["edited_at"])))
        lines += [
            t("info.owner", owner=f"<@{tag['owner_id']}>"),
            t("info.uses", uses=f"{tag['uses']:,}"),
        ]
        embed.add_field(name=t("info.title"), value="\n".join(lines))
        view = ForceRemove(bot, interaction.user.id, tag, embed)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    async def scoped_user(interaction, user, permission: str, error_key: str):
        if user and user.bot:
            await reply(interaction, t("error.bot_tags"))
            return False, None
        if user is None or user.id != interaction.user.id:
            if not getattr(permissions(interaction), permission):
                if user is not None:
                    await reply(interaction, t(error_key))
                    return False, None
                user = interaction.user
        return True, user

    @remove_group.command(name="all", description=t("command.remove_all"))
    @app_commands.describe(
        content=t("option.filter_content"),
        name=t("option.filter_name"),
        user=t("option.filter_user"),
    )
    async def remove_all(
        interaction: discord.Interaction,
        content: app_commands.Range[str, 1, 1000] | None = None,
        name: app_commands.Range[str, 1, 64] | None = None,
        user: discord.User | None = None,
    ):
        allowed, user = await scoped_user(interaction, user, "administrator", "error.remove_others")
        if not allowed:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        tags = await bot.db.search(
            interaction.guild_id, name=name, content=content, owner_id=user and user.id
        )
        scope = scope_text(user)
        if not tags:
            await reply(
                interaction,
                t("error.no_matching_tags" if content or name or user else "error.no_tags"),
            )
            return
        options = {"content": content, "name": name, "userId": str(user.id) if user else None}
        view = RemoveAll(bot, interaction.user.id, interaction.guild_id, tags, options, scope)
        view.message = await interaction.followup.send(
            t("remove_all.found", count=f"{len(tags):,}", scope=scope),
            view=view,
            ephemeral=True,
            wait=True,
        )

    @group.command(name="undo", description=t("command.undo"))
    @app_commands.describe(amount=t("option.undo_amount", max=MAX_UNDO))
    async def undo(
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, MAX_UNDO] = 1,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        text, _ = await undo_commands(
            interaction.channel, interaction.guild.me, interaction.user.id, amount
        )
        await reply(interaction, text)

    @group.command(name="export", description=t("command.export"))
    @app_commands.describe(
        content=t("option.filter_content"),
        name=t("option.filter_name"),
        user=t("option.filter_user"),
    )
    async def export(
        interaction: discord.Interaction,
        content: app_commands.Range[str, 1, 1000] | None = None,
        name: app_commands.Range[str, 1, 64] | None = None,
        user: discord.User | None = None,
    ):
        allowed, user = await scoped_user(interaction, user, "manage_guild", "error.export_others")
        if not allowed:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        tags = await bot.db.search(
            interaction.guild_id, name=name, content=content, owner_id=user and user.id
        )
        if not tags:
            await reply(
                interaction,
                t("error.no_matching_tags" if content or name or user else "error.no_tags"),
            )
            return
        options = {"content": content, "name": name, "userId": str(user.id) if user else None}
        await send_export(bot, interaction, tags, options)

    @group.command(name="import", description=t("command.import"))
    @app_commands.describe(file=t("option.import_file"))
    async def import_command(interaction: discord.Interaction, file: discord.Attachment):
        if file.size > bot.import_limits.file_bytes:
            await reply(
                interaction,
                t("error.import_size", limit=bot.import_limits.file_bytes // BYTES_PER_MB),
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        owner_id = None if permissions(interaction).manage_guild else interaction.user.id
        try:
            report = await import_tags(bot, interaction.guild_id, await file.read(), owner_id)
        except TransferError as error:
            await reply(interaction, t(str(error)))
            return
        message = t(
            "import.done",
            imported=f"{report.imported:,}",
            skipped=f"{report.skipped:,}",
            invalid=f"{report.invalid:,}",
            failed=f"{report.failed_files:,}",
        )
        if report.not_owned:
            message += " " + t("import.not_owned", count=f"{report.not_owned:,}")
        log.info("Import into guild %s finished: %s", interaction.guild_id, report)
        try:
            await reply(interaction, message)
        except discord.HTTPException:
            log.warning("Could not report the import result; the interaction expired")

    stats_group = app_commands.Group(name="stats", description=t("command.stats"), parent=group)
    periods = [app_commands.Choice(name=period_label(days), value=days) for days in PERIODS]
    sorts = [app_commands.Choice(name=t(f"sort.{key}"), value=key) for key in SORTS]

    def user_line(row) -> str:
        return t("stats.user_line", user=f"<@{row['user_id']}>", uses=f"{row['uses']:,}")

    def use_line(row) -> str:
        return t("stats.tag_line", name=code(row["name"]), uses=f"{row['uses']:,}")

    @stats_group.command(name="overview", description=t("command.stats_overview"))
    @app_commands.describe(period=t("option.period"))
    @app_commands.choices(period=periods)
    async def stats_overview(interaction: discord.Interaction, period: int = 0):
        await interaction.response.defer(ephemeral=True, thinking=True)
        stats = await bot.db.server_stats(interaction.guild_id, since(period))
        embed = stats_embed(t("stats.server_title", guild=interaction.guild.name), period)
        embed.add_field(
            name=t("stats.overview"),
            value=t(
                "stats.server_overview",
                tags=f"{stats['tags']:,}",
                groups=f"{stats['groups']:,}",
                aliases=f"{stats['aliases']:,}",
                uses=f"{stats['uses']:,}",
                period_uses=f"{stats['period_uses']:,}",
                storage=format_size(stats["storage"]),
            ),
            inline=False,
        )
        embed.add_field(
            name=t("stats.unused"),
            value=t(
                "stats.unused_overview",
                never=f"{stats['never_used']:,}",
                once=f"{stats['once']:,}",
                stale=f"{stats['stale']:,}",
                stale_90=f"{stats['stale_90']:,}",
                stale_365=f"{stats['stale_365']:,}",
            ),
            inline=False,
        )
        embed.add_field(name=t("stats.most_used"), value=ranked(stats["top_tags"], use_line)[:1024])
        embed.add_field(
            name=t("stats.top_users"), value=ranked(stats["top_users"], user_line)[:1024]
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @stats_group.command(name="tag", description=t("command.stats_tag"))
    @app_commands.describe(name=t("option.name"), period=t("option.period"))
    @app_commands.autocomplete(name=all_tags)
    @app_commands.choices(period=periods)
    async def stats_tag(interaction: discord.Interaction, name: str, period: int = 0):
        tag = await find(interaction, name)
        if tag is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        stats = await bot.db.tag_stats(tag["id"], since(period))
        size = stats["size"]
        embed = stats_embed(tag["name"], period)
        embed.add_field(
            name=t("stats.overview"),
            value=t(
                "stats.tag_overview",
                uses=f"{tag['uses']:,}",
                period_uses=f"{stats['period_uses']:,}",
                last_used=last_used(tag["last_used_at"]),
                created=f"<t:{int(tag['created_at'].timestamp())}:R>",
                owner=f"<@{tag['owner_id']}>",
                storage=format_size(size["text"] + size["files"] + size["variables"]),
                files=f"{size['file_count']:,}",
            ),
            inline=False,
        )
        embed.add_field(
            name=t("stats.top_users"), value=ranked(stats["top_users"], user_line)[:1024]
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @stats_group.command(name="member", description=t("command.stats_member"))
    @app_commands.describe(member=t("option.stats_member"), period=t("option.period"))
    @app_commands.choices(period=periods)
    async def stats_member(
        interaction: discord.Interaction, member: discord.User | None = None, period: int = 0
    ):
        member = member or interaction.user
        await interaction.response.defer(ephemeral=True, thinking=True)
        stats = await bot.db.user_stats(interaction.guild_id, member.id, since(period))
        embed = stats_embed(t("stats.user_title", user=member.display_name), period)
        embed.add_field(
            name=t("stats.overview"),
            value=t(
                "stats.user_overview",
                tags=f"{stats['tags']:,}",
                uses=f"{stats['uses']:,}",
                period_uses=f"{stats['period_owned_uses']:,}",
                runs=f"{stats['runs']:,}",
                storage=format_size(stats["storage"]),
            ),
            inline=False,
        )
        embed.add_field(name=t("stats.most_run"), value=ranked(stats["top_tags"], use_line)[:1024])
        await interaction.followup.send(embed=embed, ephemeral=True)

    @stats_group.command(name="ranking", description=t("command.stats_ranking"))
    @app_commands.describe(
        sort=t("option.sort"),
        period=t("option.period"),
        owner=t("option.owner"),
        unused_days=t("option.unused_days"),
        max_uses=t("option.max_uses"),
        min_age_days=t("option.ranking_min_age_days"),
    )
    @app_commands.choices(sort=sorts, period=periods)
    async def stats_ranking(
        interaction: discord.Interaction,
        sort: str = "most",
        period: int = 0,
        owner: discord.User | None = None,
        unused_days: app_commands.Range[int, 1, 36500] | None = None,
        max_uses: app_commands.Range[int, 1, 1000000000] | None = None,
        min_age_days: app_commands.Range[int, 1, 36500] | None = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await bot.db.select_tags(
            interaction.guild_id,
            sort=sort,
            since=since(period),
            owner_id=owner and owner.id,
            unused_days=unused_days,
            max_uses=max_uses,
            min_age_days=min_age_days,
        )
        scope = filter_text(owner, unused_days, max_uses, min_age_days)
        if not rows:
            await reply(interaction, t("prune.none", scope=scope) if scope else t("error.no_tags"))
            return
        title = t(
            "stats.ranking_title",
            sort=t(f"sort.{sort}"),
            count=f"{len(rows):,}",
            guild=interaction.guild.name,
        )
        footer = " | ".join(part for part in (period_label(period), scope) if part)
        view = ListView(
            interaction.user.id, title, [tag_line(row) for row in rows], footer, DETAIL_PAGE_SIZE
        )
        await interaction.followup.send(
            embed=view.embed(),
            view=view if len(view.pages) > 1 else discord.utils.MISSING,
            ephemeral=True,
        )

    @stats_group.command(name="storage", description=t("command.stats_storage"))
    async def stats_storage(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        storage = await bot.db.storage(interaction.guild_id)
        embed = discord.Embed(
            title=t("stats.storage_title", guild=interaction.guild.name)[:256],
            description=t(
                "stats.storage_overview",
                total=format_size(storage["total"]),
                files=format_size(storage["files"]),
                file_count=f"{storage['file_count']:,}",
                text=format_size(storage["text"]),
                variables=format_size(storage["variables"]),
            ),
            colour=discord.Colour.blurple(),
        )
        embed.add_field(
            name=t("stats.largest_tags"),
            value=ranked(
                [row for row in storage["top_tags"] if row["size"]],
                lambda row: t(
                    "stats.size_line", name=code(row["name"]), size=format_size(row["size"])
                ),
            )[:1024],
        )
        embed.add_field(
            name=t("stats.largest_owners"),
            value=ranked(
                storage["top_owners"],
                lambda row: t(
                    "stats.size_line", name=f"<@{row['owner_id']}>", size=format_size(row["size"])
                ),
            )[:1024],
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @group.command(name="prune", description=t("command.prune"))
    @app_commands.describe(
        unused_days=t("option.unused_days"),
        max_uses=t("option.max_uses"),
        least_used=t("option.least_used"),
        owner=t("option.owner"),
        min_age_days=t("option.min_age_days"),
    )
    async def prune(
        interaction: discord.Interaction,
        unused_days: app_commands.Range[int, 1, 36500] | None = None,
        max_uses: app_commands.Range[int, 1, 1000000000] | None = None,
        least_used: app_commands.Range[int, 1, 100000] | None = None,
        owner: discord.User | None = None,
        min_age_days: app_commands.Range[int, 0, 36500] = 7,
    ):
        if not permissions(interaction).manage_guild:
            await reply(interaction, t("error.permissions"))
            return
        if unused_days is None and least_used is None and max_uses is None:
            await reply(interaction, t("error.prune_criteria"))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        tags = await bot.db.select_tags(
            interaction.guild_id,
            sort="least",
            owner_id=owner and owner.id,
            unused_days=unused_days,
            max_uses=max_uses,
            min_age_days=min_age_days,
            limit=least_used,
        )
        scope = filter_text(owner, unused_days, max_uses, min_age_days, least_used)
        if not tags:
            await reply(interaction, t("prune.none", scope=scope))
            return
        title = t("prune.title", count=f"{len(tags):,}")
        view = PrunePreview(bot, interaction.user.id, interaction.guild_id, tags, title, scope)
        view.message = await interaction.followup.send(
            t("prune.found", count=f"{len(tags):,}", scope=scope)[:2000],
            embed=view.embed(),
            view=view,
            ephemeral=True,
            wait=True,
        )

    bot.tree.add_command(group)
