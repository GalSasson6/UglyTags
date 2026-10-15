import asyncio
import contextlib
import io
import json
import logging
import time
import weakref
from dataclasses import dataclass, field

import discord

from uglytags import components as kinds
from uglytags import tagscript
from uglytags.i18n import t

log = logging.getLogger(__name__)

COMPONENT_TIMEOUT = 60
PAGE_TIMEOUT = 300
MAX_V2_COMPONENTS = 40
TRASH_EMOJI = "\U0001f5d1\ufe0f"


@dataclass
class Prepared:
    content: str | None = None
    embeds: list = field(default_factory=list)
    files: list = field(default_factory=list)
    view: discord.ui.View | discord.ui.LayoutView | None = None

    def edit_kwargs(self) -> dict:
        return {
            "content": self.content,
            "embeds": self.embeds,
            "attachments": [to_discord_file(file) for file in self.files],
            "view": self.view,
        }

    def send_kwargs(self) -> dict:
        return {
            "content": self.content,
            "embeds": self.embeds,
            "files": [to_discord_file(file) for file in self.files],
            "view": self.view,
        }


def to_discord_file(file: tagscript.TagFile) -> discord.File:
    data = file.data.encode() if isinstance(file.data, str) else file.data
    return discord.File(
        io.BytesIO(data),
        filename=file.filename,
        spoiler=file.spoiler,
        description=file.description,
    )


def emoji(data: dict | None):
    if not data:
        return None
    return discord.PartialEmoji(
        name=data.get("name"),
        id=int(data["id"]) if data.get("id") else None,
        animated=bool(data.get("animated")),
    )


class DeleteButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"uglytags:delete:(?P<user>\d+)"
):
    def __init__(self, user_id: int, row: int | None = None):
        self.user_id = user_id
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.danger,
                emoji=TRASH_EMOJI,
                custom_id=f"uglytags:delete:{user_id}",
                row=row,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["user"]))

    async def callback(self, interaction: discord.Interaction):
        permissions = getattr(interaction.user, "guild_permissions", discord.Permissions.none())
        if interaction.user.id != self.user_id and not permissions.administrator:
            await interaction.response.send_message(t("error.delete_owner"), ephemeral=True)
            return
        await interaction.response.defer()
        if self.view is not None:
            self.view.stop()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            await interaction.message.delete()
        await interaction.followup.send(t("result.deleted"), ephemeral=True)


def delete_view(user_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(DeleteButton(user_id))
    return view


def reply_embed(message: discord.Message) -> discord.Embed:
    text = " ".join(message.content.split())
    snippet = discord.utils.escape_markdown(text[:100])
    if len(text) > 100:
        snippet += "\u2026"
    description = t(
        "recent.reply",
        link=message.jump_url,
        text=snippet or t("recent.reply_attachment"),
    )
    if message.attachments or message.embeds:
        description += " \U0001f4ce"
    embed = discord.Embed(description=description)
    embed.set_author(
        name=t("recent.reply_author", name=message.author.global_name or message.author.name),
        icon_url=str(message.author.display_avatar.url),
    )
    return embed


def add_reply(prepared: Prepared, message: discord.Message) -> Prepared:
    embed = reply_embed(message)
    mention = t("recent.reply_mention", author=message.author.mention)
    if isinstance(prepared.view, discord.ui.LayoutView):
        card = discord.ui.Container(
            discord.ui.Section(
                f"**{discord.utils.escape_markdown(embed.author.name)}**\n{embed.description}",
                accessory=discord.ui.Thumbnail(embed.author.icon_url),
            )
        )
        items = prepared.view.to_components()
        if kinds.count_components(items) + 5 > MAX_V2_COMPONENTS:
            raise tagscript.TagError(t("error.reply_components"))
        trash_row = next(
            (
                item
                for item in prepared.view.children
                if isinstance(item, discord.ui.ActionRow)
                and any(isinstance(child, DeleteButton) for child in item.children)
            ),
            None,
        )
        if trash_row:
            prepared.view.remove_item(trash_row)
        prepared.view.add_item(discord.ui.TextDisplay(mention))
        prepared.view.add_item(card)
        if trash_row:
            prepared.view.add_item(trash_row)
    else:
        if len(prepared.embeds) >= 10:
            raise tagscript.TagError(t("error.reply_embeds"))
        content = prepared.content or ""
        prepared.content = "\n".join(
            part for part in (content[: 2000 - len(mention) - 1], mention) if part
        )
        prepared.embeds = [*prepared.embeds, embed]
    return prepared


class ComponentSession:
    def __init__(
        self, rt: tagscript.Runtime, result: tagscript.TagResult, editor, reply_message=None
    ):
        self.rt = rt
        self.result = result
        self.editor = editor
        self.executing = False
        self.view = None
        self.reply_message = reply_message

    def build(self, content: str) -> discord.ui.View | discord.ui.LayoutView:
        try:
            return self._build(content)
        except (TypeError, ValueError) as error:
            raise tagscript.TagError(f"Invalid components: {error}") from error

    def _build(self, content: str) -> discord.ui.View | discord.ui.LayoutView:
        items = self.result.components["components"]
        owner_id = self.rt.user.id
        if kinds.is_v2(items):
            total = kinds.count_components(items) + bool(content)
            if total > MAX_V2_COMPONENTS:
                raise tagscript.TagError("Messages can contain at most 40 components")
            view = SessionLayoutView(self)
            if content:
                view.add_item(discord.ui.TextDisplay(content[:4000]))
            for data in items:
                view.add_item(self.v2_item(data))
            if total + 2 <= MAX_V2_COMPONENTS:
                view.add_item(discord.ui.ActionRow(DeleteButton(owner_id)))
        else:
            view = SessionView(self)
            row = 0
            for data in items:
                if data["type"] == kinds.ACTION_ROW:
                    for child in data["components"]:
                        view.add_item(self.interactive(child, row))
                    row += 1
                else:
                    view.add_item(self.interactive(data, None))
            try:
                view.add_item(DeleteButton(owner_id, row if 0 < row < 5 else None))
            except ValueError:
                log.debug("No room for the delete button")
        self.view = view
        return view

    def interactive(self, data: dict, row: int | None):
        if data["type"] == kinds.BUTTON:
            button = discord.ui.Button(
                style=discord.ButtonStyle(data["style"]),
                label=data.get("label"),
                url=data.get("url"),
                emoji=emoji(data.get("emoji")),
                disabled=data.get("disabled", False),
                row=row,
            )
            if not data.get("url"):
                button.callback = self.callback(data.get("run"))
            return button
        options = [
            discord.SelectOption(
                label=option["label"],
                value=option["value"],
                description=option.get("description"),
                emoji=emoji(option.get("emoji")),
                default=option.get("default", False),
            )
            for option in data["options"]
        ]
        maximum = min(data.get("max_values", 1), len(options))
        select = discord.ui.Select(
            placeholder=data.get("placeholder"),
            min_values=min(data.get("min_values", 1), maximum),
            max_values=maximum,
            options=options,
            row=row,
        )
        select.callback = self.callback(data.get("run"), select)
        return select

    def v2_item(self, data: dict):
        kind = data["type"]
        if kind in (kinds.BUTTON, kinds.SELECT_MENU):
            return discord.ui.ActionRow(self.interactive(data, None))
        if kind == kinds.ACTION_ROW:
            return discord.ui.ActionRow(
                *(self.interactive(child, None) for child in data["components"])
            )
        if kind == kinds.CONTAINER:
            return discord.ui.Container(
                *(self.v2_item(child) for child in data["components"]),
                accent_colour=data.get("accent_color"),
                spoiler=data.get("spoiler", False),
            )
        if kind == kinds.SECTION:
            accessory = data["accessory"]
            return discord.ui.Section(
                *(child["content"] for child in data["components"]),
                accessory=(
                    self.interactive(accessory, None)
                    if accessory["type"] == kinds.BUTTON
                    else self.v2_item(accessory)
                ),
            )
        if kind == kinds.TEXT_DISPLAY:
            return discord.ui.TextDisplay(data["content"])
        if kind == kinds.THUMBNAIL:
            return discord.ui.Thumbnail(
                data["media"]["url"],
                description=data.get("description"),
                spoiler=data.get("spoiler", False),
            )
        if kind == kinds.MEDIA_GALLERY:
            return discord.ui.MediaGallery(
                *(
                    discord.MediaGalleryItem(
                        item["media"]["url"],
                        description=item.get("description"),
                        spoiler=item.get("spoiler", False),
                    )
                    for item in data["items"]
                )
            )
        if kind == kinds.FILE:
            return discord.ui.File(data["file"]["url"], spoiler=data.get("spoiler", False))
        if kind == kinds.SEPARATOR:
            return discord.ui.Separator(
                visible=data.get("divider", True),
                spacing=(
                    discord.SeparatorSpacing.large
                    if data.get("spacing") == 2
                    else discord.SeparatorSpacing.small
                ),
            )
        raise tagscript.TagError(f"Unknown Component Type: {kind}")

    def callback(self, script: str | None, select=None):
        async def run(interaction: discord.Interaction):
            await self.run(interaction, script, select.values if select else None)

        return run

    def disable(self):
        for item in self.view.walk_children():
            if isinstance(item, discord.ui.Button | discord.ui.Select):
                item.disabled = True

    async def run(self, interaction: discord.Interaction, script: str | None, values):
        settings = self.result.variables[tagscript.SETTINGS]
        allowed = settings.get(tagscript.SETTING_ALLOW_EVERYONE) or (
            interaction.user.id == self.rt.user.id
        )
        if not script or not allowed or self.executing:
            await interaction.response.defer()
            return
        self.executing = True
        self.view.stop()
        self.disable()
        await interaction.response.edit_message(view=self.view)
        self.editor = interaction.edit_original_response
        try:
            if values is not None:
                self.result.variables[tagscript.VALUES] = json.dumps(values)
            tagscript.reset_limits(self.result)
            tagscript.count_component_execution(self.result)
            result = await tagscript.parse(
                self.rt,
                script,
                "",
                self.result.variables,
                self.result.limits,
                self.result.files,
            )
            if result.pages:
                raise tagscript.TagError(
                    "TagScript does not support pages inside of components currently"
                )
            prepared = prepare(
                self.rt, result, editor=self.editor, reply_message=self.reply_message
            )
            await self.editor(**prepared.edit_kwargs())
        except tagscript.TagError as error:
            await self.fail(str(error))
        except discord.HTTPException as error:
            log.warning("Component update failed: %s", error)
            await self.fail(t("error.send", error=error.text or error.status))

    async def fail(self, message: str):
        try:
            await self.editor(
                content=t("show.component_error", error=message)[:2000],
                embeds=[],
                attachments=[],
                view=delete_view(self.rt.user.id),
            )
        except discord.HTTPException:
            log.warning("Could not report component failure")

    async def timeout(self):
        on_timeout = self.result.components.get("on_timeout")
        try:
            if on_timeout:
                tagscript.count_component_execution(self.result)
                result = await tagscript.parse(
                    self.rt,
                    on_timeout,
                    "",
                    self.result.variables,
                    self.result.limits,
                    self.result.files,
                )
                prepared = prepare(
                    self.rt, result, editor=self.editor, reply_message=self.reply_message
                )
                await self.editor(**prepared.edit_kwargs())
            elif isinstance(self.view, discord.ui.LayoutView):
                self.disable()
                await self.editor(view=self.view)
            else:
                await self.editor(view=delete_view(self.rt.user.id))
        except tagscript.TagError as error:
            await self.fail(str(error))
        except discord.HTTPException:
            log.debug("Could not update timed out tag components")


class SessionView(discord.ui.View):
    def __init__(self, session: ComponentSession):
        super().__init__(timeout=COMPONENT_TIMEOUT)
        self.session = session

    async def on_timeout(self):
        if not self.session.executing:
            await self.session.timeout()


class SessionLayoutView(discord.ui.LayoutView):
    def __init__(self, session: ComponentSession):
        super().__init__(timeout=COMPONENT_TIMEOUT)
        self.session = session

    async def on_timeout(self):
        if not self.session.executing:
            await self.session.timeout()


def page_files(result: tagscript.TagResult) -> list[list[tagscript.TagFile]]:
    pages = result.pages
    files = {file.filename: file for file in result.files[:10]}
    attached = [[] for _ in pages]
    used = set()
    for index, page in enumerate(pages):
        names = [name.removeprefix("attachment://") for name in page.get("filenames") or []]
        for embed in page.get("embeds") or []:
            for url in (
                embed.author.icon_url,
                embed.footer.icon_url,
                embed.thumbnail.url,
                embed.image.url,
            ):
                if url and url.startswith("attachment://"):
                    names.append(url.removeprefix("attachment://"))
        for name in names:
            if name in files and files[name] not in attached[index]:
                attached[index].append(files[name])
                used.add(name)
    for name, file in files.items():
        if name not in used:
            for page in attached:
                page.append(file)
    return attached


class Paginator(discord.ui.View):
    def __init__(
        self, result: tagscript.TagResult, owner_id: int, title: str = "", reply_message=None
    ):
        super().__init__(timeout=PAGE_TIMEOUT)
        self.pages = result.pages
        self.files = page_files(result)
        self.owner_id = owner_id
        self.title = title
        self.reply_message = reply_message
        self.index = 0
        self.editor = None
        self.add_item(DeleteButton(owner_id, 1))
        self.update_buttons()

    def update_buttons(self):
        last = len(self.pages) - 1
        self.first.disabled = self.previous.disabled = self.index == 0
        self.next.disabled = self.last.disabled = self.index == last
        self.counter.label = f"{self.index + 1}/{len(self.pages)}"

    def kwargs(self) -> dict:
        page = self.pages[self.index]
        content = "\n".join(part for part in (self.title, page.get("content")) if part)
        prepared = Prepared(
            content[:2000] or None,
            page.get("embeds") or [],
            self.files[self.index],
            self,
        )
        if self.reply_message:
            add_reply(prepared, self.reply_message)
        return prepared.edit_kwargs()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (interaction.data or {}).get("custom_id", "").startswith("uglytags:delete:"):
            return True
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(t("error.owner"), ephemeral=True)
            return False
        return True

    async def go(self, interaction: discord.Interaction, index: int):
        self.index = max(0, min(index, len(self.pages) - 1))
        self.update_buttons()
        await interaction.response.edit_message(**self.kwargs())

    @discord.ui.button(label="<<", style=discord.ButtonStyle.secondary)
    async def first(self, interaction, button):
        await self.go(interaction, 0)

    @discord.ui.button(label="<", style=discord.ButtonStyle.primary)
    async def previous(self, interaction, button):
        await self.go(interaction, self.index - 1)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter(self, interaction, button):
        await interaction.response.defer()

    @discord.ui.button(label=">", style=discord.ButtonStyle.primary)
    async def next(self, interaction, button):
        await self.go(interaction, self.index + 1)

    @discord.ui.button(label=">>", style=discord.ButtonStyle.secondary)
    async def last(self, interaction, button):
        await self.go(interaction, len(self.pages) - 1)

    async def on_timeout(self):
        if self.editor:
            try:
                await self.editor(view=delete_view(self.owner_id))
            except discord.HTTPException:
                log.debug("Could not remove paginator buttons")


def prepare(
    rt: tagscript.Runtime,
    result: tagscript.TagResult,
    title: str = "",
    editor=None,
    reply_message: discord.Message | None = None,
):
    if result.pages:
        paginator = Paginator(result, rt.user.id, title, reply_message)
        if reply_message and any(len(page.get("embeds") or []) >= 10 for page in result.pages):
            raise tagscript.TagError(t("error.reply_embeds"))
        paginator.editor = editor
        kwargs = paginator.kwargs()
        prepared = Prepared(kwargs["content"], kwargs["embeds"], paginator.files[0], paginator)
        return prepared
    content = result.text.strip()[:4000].strip()
    if title:
        content = f"{title}\n\n{content}"[:4000].strip()
    prepared = Prepared(files=result.files[:10])
    has_components = bool(result.components and result.components["components"])
    if has_components:
        session = ComponentSession(rt, result, editor, reply_message)
        prepared.view = session.build(content)
    else:
        prepared.view = delete_view(rt.user.id)
    if isinstance(prepared.view, discord.ui.LayoutView):
        return add_reply(prepared, reply_message) if reply_message else prepared
    prepared.content = content[:2000] or None
    prepared.embeds = result.embeds[:10]
    if not (content or has_components or result.embeds or result.files):
        prepared.content = t("show.empty")
    return add_reply(prepared, reply_message) if reply_message else prepared


async def send_result(
    interaction: discord.Interaction,
    rt,
    result,
    title: str = "",
    lead_embed=None,
    reply_message: discord.Message | None = None,
):
    lead = [lead_embed] if lead_embed else []
    try:
        prepared = prepare(rt, result, title, interaction.edit_original_response, reply_message)
    except tagscript.TagError as error:
        await send_error(
            interaction, "\n\n".join(part for part in (title, str(error)) if part), lead
        )
        return
    separate_lead = bool(lead) and isinstance(prepared.view, discord.ui.LayoutView | Paginator)
    if lead and not separate_lead:
        prepared.embeds = lead + prepared.embeds[:9]
    try:
        kwargs = prepared.edit_kwargs()
        if reply_message is not None:
            kwargs["allowed_mentions"] = discord.AllowedMentions(
                everyone=False,
                roles=False,
                users=[reply_message.author] if not reply_message.author.bot else False,
                replied_user=False,
            )
        await interaction.edit_original_response(**kwargs)
    except discord.HTTPException as error:
        log.warning("Tag response failed: %s", error)
        if prepared.view:
            prepared.view.stop()
        if error.status == 413:
            message = t("error.too_large")
        else:
            message = t("error.send", error=error.text or error.status)
        await send_error(interaction, message, lead)
        return
    if separate_lead:
        await interaction.followup.send(embed=lead_embed, ephemeral=True)


async def send_error(interaction: discord.Interaction, message: str, embeds=()):
    await interaction.edit_original_response(
        content=message[:2000],
        embeds=list(embeds),
        attachments=[],
        view=delete_view(interaction.user.id),
    )


REPLY_WINDOW = 15 * 60
MAX_TRACKED_REPLIES = 1000


@dataclass
class TrackedReply:
    reply: discord.Message
    view: weakref.ref | None
    content: str
    author_id: int
    channel_id: int
    expires: float

    def stop_view(self) -> None:
        view = self.view() if self.view is not None else None
        if view is not None:
            view.stop()


class ReplyTracker:
    """Remembers which bot reply answers which command message, so an edited
    command updates its reply and a deleted command removes it."""

    def __init__(self):
        self.entries: dict[int, TrackedReply] = {}
        self.locks: dict[int, list] = {}

    def get(self, message_id: int) -> TrackedReply | None:
        entry = self.entries.get(message_id)
        if entry is not None and entry.expires <= time.monotonic():
            del self.entries[message_id]
            return None
        return entry

    def take(self, message_id: int) -> TrackedReply | None:
        entry = self.get(message_id)
        self.entries.pop(message_id, None)
        return entry

    def put(self, message: discord.Message, reply: discord.Message, view) -> None:
        now = time.monotonic()
        self.entries.pop(message.id, None)
        self.entries[message.id] = TrackedReply(
            reply,
            weakref.ref(view) if view is not None else None,
            message.content,
            message.author.id,
            message.channel.id,
            now + REPLY_WINDOW,
        )
        for message_id in [key for key, entry in self.entries.items() if entry.expires <= now]:
            del self.entries[message_id]
        while len(self.entries) > MAX_TRACKED_REPLIES:
            del self.entries[next(iter(self.entries))]

    async def discard(self, message_id: int) -> bool:
        """Delete a tracked reply. A reply that cannot be deleted stays tracked."""
        entry = self.take(message_id)
        if entry is None:
            return False
        try:
            await entry.reply.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            self.entries[message_id] = entry
            return False
        entry.stop_view()
        return True

    def latest(self, author_id: int, channel_id: int, count: int) -> list[int]:
        found = []
        for message_id in sorted(self.entries, reverse=True):
            entry = self.get(message_id)
            if entry and entry.author_id == author_id and entry.channel_id == channel_id:
                found.append(message_id)
                if len(found) == count:
                    break
        return found

    @contextlib.asynccontextmanager
    async def guard(self, message_id: int):
        slot = self.locks.setdefault(message_id, [asyncio.Lock(), 0])
        slot[1] += 1
        try:
            async with slot[0]:
                yield
        finally:
            slot[1] -= 1
            if not slot[1]:
                self.locks.pop(message_id, None)


REPLIES = ReplyTracker()


async def respond(message: discord.Message, content: str | None = None, **kwargs):
    """Reply to a command message, or edit the earlier reply if it already has one."""
    sent = None
    view = kwargs.get("view")
    view = None if view is discord.utils.MISSING else view
    previous = REPLIES.take(message.id)
    if previous is not None:
        previous.stop_view()
        embeds = kwargs.get("embeds") or ([kwargs["embed"]] if kwargs.get("embed") else [])
        try:
            sent = await previous.reply.edit(
                content=content or None,
                embeds=embeds,
                attachments=kwargs.get("files") or [],
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as error:
            log.debug("Could not edit the earlier reply: %s", error)
            if error.status != 404:
                with contextlib.suppress(discord.HTTPException):
                    await previous.reply.delete()
    if sent is None:
        for file in kwargs.get("files") or []:
            file.reset()
        sent = await message.reply(
            content,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
            **kwargs,
        )
    REPLIES.put(message, sent, view)
    return sent


async def send_message_result(message: discord.Message, rt, result, title: str = ""):
    sent = None

    async def edit(**kwargs):
        await sent.edit(**kwargs)

    try:
        prepared = prepare(rt, result, title, edit)
    except tagscript.TagError as error:
        await send_message_error(message, "\n\n".join(part for part in (title, str(error)) if part))
        return
    try:
        sent = await respond(message, **prepared.send_kwargs())
    except discord.HTTPException as error:
        log.warning("Tag response failed: %s", error)
        if prepared.view:
            prepared.view.stop()
        error_message = (
            t("error.too_large")
            if error.status == 413
            else t("error.send", error=error.text or error.status)
        )
        await send_message_error(message, error_message)


async def send_message_error(message: discord.Message, content: str):
    await respond(message, content[:2000], view=delete_view(message.author.id))
