import asyncio
import gc
import itertools
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from uglytags import ui
from uglytags.bot import UglyTags, parse_import_limits
from uglytags.transfer import ImportLimits
from uglytags.ui import TagModal, format_size, name_candidates, save_tag, send_export

MESSAGE_IDS = itertools.count(1000)


@pytest.fixture(autouse=True)
def clear_replies():
    ui.render.REPLIES.entries.clear()


def tag(**overrides):
    row = {
        "id": 10,
        "guild_id": 1,
        "name": "hello",
        "content": "hi",
        "owner_id": 2,
        "reference_id": None,
        "reference_name": None,
        "reference_content": None,
        "uses": 0,
        "created_at": datetime.now(UTC),
        "edited_at": None,
        "is_group": False,
        "reference_is_group": None,
    }
    row.update(overrides)
    return row


def interaction(user_id=2, **permissions):
    state = {"done": False}

    async def defer(**kwargs):
        state["done"] = True

    response = SimpleNamespace(
        send_message=AsyncMock(),
        send_modal=AsyncMock(),
        edit_message=AsyncMock(),
        defer=AsyncMock(side_effect=defer),
        is_done=lambda: state["done"],
    )
    user = SimpleNamespace(
        id=user_id,
        bot=False,
        mention=f"<@{user_id}>",
        guild_permissions=discord.Permissions(**permissions),
    )
    return SimpleNamespace(
        guild_id=1,
        user=user,
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
        original_response=AsyncMock(),
    )


def prefix_message(content, user_id=2, attachments=None, **permissions):
    author = SimpleNamespace(
        id=user_id,
        bot=False,
        display_name=f"User {user_id}",
        guild_permissions=discord.Permissions(**permissions),
    )
    guild = SimpleNamespace(
        id=1,
        name="Guild",
        filesize_limit=20_000_000,
        get_member=lambda member_id: author if member_id == user_id else None,
        get_member_named=lambda name: author if name == author.display_name else None,
    )
    return SimpleNamespace(
        id=next(MESSAGE_IDS),
        content=content,
        author=author,
        guild=guild,
        channel=SimpleNamespace(id=5, send=AsyncMock(return_value=AsyncMock())),
        attachments=attachments or [],
        embeds=[],
        reference=None,
        reply=AsyncMock(return_value=AsyncMock()),
    )


def command(bot, *path):
    group = bot.tree.get_command("tag")
    for name in path:
        group = group.get_command(name)
    return group.callback


async def test_commands_and_intents():
    bot = UglyTags("postgresql://localhost/test")
    assert {command.name for command in bot.tree.get_commands()} == {
        "t",
        "tag",
        "\U0001f4ac Tag reply",
    }
    shortcut = bot.tree.get_command("\U0001f4ac Tag reply", type=discord.AppCommandType.message)
    assert shortcut.guild_only
    assert shortcut.type == discord.AppCommandType.message
    show = bot.tree.get_command("t")
    assert show.guild_only
    assert [parameter.name for parameter in show.parameters] == ["name", "arguments", "attachment"]
    assert show.parameters[0].required and show.parameters[0].autocomplete
    group = bot.tree.get_command("tag")
    assert group.guild_only
    assert {command.qualified_name for command in group.walk_commands()} >= {
        "tag create",
        "tag add",
        "tag edit",
        "tag alias",
        "tag rename",
        "tag info",
        "tag random",
        "tag list server",
        "tag list user",
        "tag remove tag",
        "tag remove all",
        "tag export",
        "tag import",
    }
    assert (
        bot.intents.guild_messages
        and bot.intents.members
        and bot.intents.presences
        and bot.intents.message_content
    )
    payload = group.to_dict(bot.tree)
    for option in payload["options"]:
        assert len(option["description"]) <= 100
    await bot.close()


@pytest.mark.parametrize(
    ("value", "consume", "expected"),
    [
        ('"two words" remaining text', False, ("two words", "remaining text")),
        ("one remaining text", False, ("one", "remaining text")),
        ('"two words"', True, ("two words", "")),
        ("two words", True, ("two words", "")),
        ('"unterminated', False, None),
    ],
)
def test_prefix_argument_parsing(value, consume, expected):
    assert ui.prefix_argument(value, consume=consume) == expected


async def test_prefix_show_and_original_aliases(monkeypatch):
    row = tag(name="two words")
    bot = stats_bot(get_tag=AsyncMock(return_value=row))
    execute = AsyncMock()
    monkeypatch.setattr(ui, "execute_message", execute)

    message = prefix_message('.t "two words" first second')
    assert await ui.handle_prefix_message(bot, message)
    execute.assert_awaited_once_with(bot, message, row, "first second", None)

    execute.reset_mock()
    message = prefix_message(".tag show hello arguments")
    await ui.handle_prefix_message(bot, message)
    execute.assert_awaited_once_with(bot, message, row, "arguments", None)

    assert not await ui.handle_prefix_message(bot, prefix_message("not a command"))
    await bot.close()


async def test_prefix_create_edit_alias_and_rename(monkeypatch):
    row = tag()
    bot = stats_bot(
        get_tag=AsyncMock(side_effect=[None, row, row, None, row]),
        save_alias=AsyncMock(),
        rename=AsyncMock(),
    )
    save = AsyncMock(side_effect=["created", "edited"])
    monkeypatch.setattr(ui, "save_tag", save)
    monkeypatch.setattr(ui, "prefix_uploads", AsyncMock(return_value=[]))

    create = prefix_message(".t create hello created content")
    await ui.handle_prefix_message(bot, create)
    save.assert_not_awaited()
    create_view = create.reply.await_args.kwargs["view"]
    click = button_event()
    await create_view.confirm.callback(click)
    assert save.await_args_list[0].args[2:5] == ("hello", "created content", [])
    assert click.edit_original_response.await_args.kwargs["content"] == "created"
    assert not create.reply.await_args.kwargs["mention_author"]

    edit = prefix_message(".t edit hello edited content")
    await ui.handle_prefix_message(bot, edit)
    assert save.await_count == 1
    edit_view = edit.reply.await_args.kwargs["view"]
    await edit_view.confirm.callback(button_event())
    assert save.await_args_list[1].args[2:5] == ("hello", "edited content", [])
    assert save.await_args_list[1].args[5] is row

    alias = prefix_message('.t alias greeting "hello"')
    await ui.handle_prefix_message(bot, alias)
    assert bot.db.save_alias.await_args.args[1:] == (1, "greeting", 2, 10)

    rename = prefix_message('.t rename hello "new name"')
    await ui.handle_prefix_message(bot, rename)
    bot.db.rename.assert_awaited_once_with(10, "new name")
    await bot.close()


@pytest.mark.parametrize("command_name", ["add", "create"])
@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("content", ["Original message", ""])
async def test_prefix_create_from_reply(command_name, cached, content):
    bot = stats_bot(get_tag=AsyncMock(return_value=None), create_tag=AsyncMock())
    attachment = SimpleNamespace(
        size=4, filename="../photo.png", read=AsyncMock(return_value=b"data")
    )
    target = prefix_message(content, user_id=3, attachments=[attachment])
    message = prefix_message(f'.t {command_name} "two words"')
    message.reference = SimpleNamespace(
        resolved=target if cached else None,
        message_id=100,
        type=discord.MessageReferenceType.default,
    )
    message.channel.id = 5
    message.channel.fetch_message = AsyncMock(return_value=target)

    await ui.handle_prefix_message(bot, message)

    bot.db.create_tag.assert_not_awaited()
    attachment.read.assert_not_awaited()
    if cached:
        message.channel.fetch_message.assert_not_awaited()
    else:
        message.channel.fetch_message.assert_awaited_once_with(100)
    view = message.reply.await_args.kwargs["view"]
    assert view.owner_id == 2
    click = button_event()
    await view.confirm.callback(click)
    args = bot.db.create_tag.await_args.args
    assert args[1:] == (
        1,
        "two words",
        "{attach:photo.png}",
        2,
        {"photo.png": b"data"},
    )
    await bot.close()


async def test_prefix_create_does_not_copy_plain_reply_text():
    bot = stats_bot(get_tag=AsyncMock(return_value=None), create_tag=AsyncMock())
    message = prefix_message(".tag add hello")
    message.reference = SimpleNamespace(
        resolved=prefix_message("original text"),
        message_id=100,
        type=discord.MessageReferenceType.default,
    )
    await ui.handle_prefix_message(bot, message)
    assert "Usage:" in message.reply.await_args.args[0]
    bot.db.create_tag.assert_not_awaited()
    await bot.close()


async def test_prefix_create_from_reply_links_and_embed_media():
    bot = stats_bot(get_tag=AsyncMock(return_value=None), create_tag=AsyncMock())
    url = "https://example.com/photo.png"
    target = prefix_message(f"look {url} https://example.com/ignored")
    target.embeds = [
        discord.Embed().set_image(url=url).set_thumbnail(url="https://example.com/thumb.png")
    ]
    message = prefix_message(".t add hello")
    message.reference = SimpleNamespace(resolved=target, type=discord.MessageReferenceType.default)
    await ui.handle_prefix_message(bot, message)
    view = message.reply.await_args.kwargs["view"]
    await view.confirm.callback(button_event())
    assert bot.db.create_tag.await_args.args[3] == f"{url}\nhttps://example.com/thumb.png"
    await bot.close()


@pytest.mark.parametrize("explicit_content", ["", "explicit body"])
async def test_prefix_create_command_content_and_uploads_take_priority(explicit_content):
    bot = stats_bot(get_tag=AsyncMock(return_value=None), create_tag=AsyncMock())
    attachment = SimpleNamespace(size=4, filename="file.txt", read=AsyncMock(return_value=b"data"))
    message = prefix_message(f".t add hello {explicit_content}", attachments=[attachment])
    message.reference = SimpleNamespace(resolved=None, type=discord.MessageReferenceType.default)
    message.channel.fetch_message = AsyncMock()
    await ui.handle_prefix_message(bot, message)
    message.channel.fetch_message.assert_not_awaited()
    view = message.reply.await_args.kwargs["view"]
    await view.confirm.callback(button_event())
    assert bot.db.create_tag.await_args.args[3] == "\n".join(
        part for part in (explicit_content, "{attach:file.txt}") if part
    )
    await bot.close()


async def test_prefix_create_explicit_text_skips_reply():
    bot = stats_bot(get_tag=AsyncMock(return_value=None), create_tag=AsyncMock())
    message = prefix_message(".t add hello explicit body")
    message.reference = SimpleNamespace(resolved=None, type=discord.MessageReferenceType.default)
    message.channel.fetch_message = AsyncMock()
    await ui.handle_prefix_message(bot, message)
    message.channel.fetch_message.assert_not_awaited()
    view = message.reply.await_args.kwargs["view"]
    await view.confirm.callback(button_event())
    assert bot.db.create_tag.await_args.args[3] == "explicit body"
    await bot.close()


@pytest.mark.parametrize("failure", ["deleted", "not_found", "forbidden"])
async def test_prefix_create_unavailable_reply(failure):
    bot = stats_bot(get_tag=AsyncMock(return_value=None), create_tag=AsyncMock())
    message = prefix_message(".t add hello")
    reference = SimpleNamespace(
        resolved=None, message_id=100, type=discord.MessageReferenceType.default
    )
    message.reference = reference
    if failure == "deleted":
        reference.resolved = discord.DeletedReferencedMessage(reference)
    else:
        error_type = discord.NotFound if failure == "not_found" else discord.Forbidden
        response = SimpleNamespace(status=404 if failure == "not_found" else 403, reason=failure)
        message.channel.fetch_message = AsyncMock(side_effect=error_type(response, "unavailable"))
    await ui.handle_prefix_message(bot, message)
    assert "Unable to read the replied-to message" in message.reply.await_args.args[0]
    assert "view" not in message.reply.await_args.kwargs
    bot.db.create_tag.assert_not_awaited()
    await bot.close()


async def test_prefix_create_replied_file_size_error_is_sent_to_command():
    bot = stats_bot(get_tag=AsyncMock(return_value=None), create_tag=AsyncMock())
    attachment = SimpleNamespace(size=bot.max_media_bytes + 1, read=AsyncMock())
    target = prefix_message("", attachments=[attachment])
    message = prefix_message(".t add hello")
    message.reference = SimpleNamespace(resolved=target, type=discord.MessageReferenceType.default)
    await ui.handle_prefix_message(bot, message)
    assert "Each file must be" in message.reply.await_args.args[0]
    target.reply.assert_not_awaited()
    attachment.read.assert_not_awaited()
    bot.db.create_tag.assert_not_awaited()
    await bot.close()


@pytest.mark.parametrize("reference_type", [None, discord.MessageReferenceType.forward])
async def test_prefix_create_without_content_or_reply_reports_usage(reference_type):
    bot = stats_bot(get_tag=AsyncMock(return_value=None), create_tag=AsyncMock())
    message = prefix_message(".t add hello")
    if reference_type:
        message.reference = SimpleNamespace(type=reference_type)
    await ui.handle_prefix_message(bot, message)
    assert "Usage:" in message.reply.await_args.args[0]
    bot.db.create_tag.assert_not_awaited()
    await bot.close()


async def test_prefix_info_list_random_and_remove(monkeypatch):
    row = tag()
    bot = stats_bot(
        get_tag=AsyncMock(return_value=row),
        file_names=AsyncMock(return_value=[]),
        search=AsyncMock(return_value=[row]),
        random=AsyncMock(return_value=row),
        delete_matching=AsyncMock(return_value=1),
    )
    execute = AsyncMock()
    monkeypatch.setattr(ui, "execute_message", execute)

    info = prefix_message(".t info hello")
    await ui.handle_prefix_message(bot, info)
    assert info.reply.await_args.kwargs["embed"].title == "hello"

    listing = prefix_message(".t list")
    await ui.handle_prefix_message(bot, listing)
    assert listing.reply.await_args.kwargs["embed"].title == "User 2's tags (1)"

    random = prefix_message(".t random some arguments")
    await ui.handle_prefix_message(bot, random)
    execute.assert_awaited_once_with(
        bot, random, row, "some arguments", None, "Showing tag: `hello`"
    )

    remove = prefix_message(".t remove tag hello")
    await ui.handle_prefix_message(bot, remove)
    bot.db.delete_matching.assert_not_awaited()
    view = remove.reply.await_args.kwargs["view"]
    await view.confirm.callback(button_event())
    bot.db.delete_matching.assert_awaited_once_with(1, [10])
    await bot.close()


async def test_prefix_search_matches_server_tag_names():
    row = tag()
    bot = stats_bot(search=AsyncMock(return_value=[row]))

    search = prefix_message('.t search "hel lo"')
    await ui.handle_prefix_message(bot, search)
    bot.db.search.assert_awaited_once_with(1, name="hel lo")
    assert search.reply.await_args.kwargs["embed"].title == "Guild's tags (1)"

    for content in (".t search", '.t search ""'):
        bot.db.search.reset_mock()
        listing = prefix_message(content)
        await ui.handle_prefix_message(bot, listing)
        bot.db.search.assert_awaited_once_with(1, owner_id=2)
        assert listing.reply.await_args.kwargs["embed"].title == "User 2's tags (1)"

    bot.db.search = AsyncMock(return_value=[])
    missing = prefix_message(".t search nothing")
    await ui.handle_prefix_message(bot, missing)
    assert missing.reply.await_args.args[0] == ui.t("error.no_matching_tags")
    await bot.close()


def reply_target():
    return SimpleNamespace(
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=3),
        author=SimpleNamespace(
            id=4,
            mention="<@4>",
            bot=False,
            global_name="sour kraut",
            name="sauerkraut",
            display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/avatars/4/avatar.png"),
        ),
        attachments=[],
        embeds=[],
        content="Original message",
        jump_url="https://discord.com/channels/1/3/100",
    )


async def test_tag_reply_opens_private_recent_picker(monkeypatch):
    bot = stats_bot(recent_tags=AsyncMock(return_value=[tag(), tag(id=11, name="second")]))
    monkeypatch.setattr(ui, "can_send_tag", lambda event: True)
    event = interaction()
    shortcut = bot.tree.get_command("\U0001f4ac Tag reply", type=discord.AppCommandType.message)
    await shortcut.callback(event, reply_target())
    bot.db.recent_tags.assert_awaited_once_with(1, 2)
    modal = event.response.send_modal.await_args.args[0]
    assert modal.title == "tag reply"
    payload = modal.to_dict()
    selection = payload["components"][0]["component"]
    assert selection["type"] == 3
    assert [(item["label"], item["value"]) for item in selection["options"]] == [
        ("hello", "10"),
        ("second", "11"),
    ]
    assert payload["components"][2]["component"]["max_values"] == 1
    assert not payload["components"][1]["component"]["required"]
    event.response.defer.assert_not_awaited()

    bot.db.recent_tags.return_value = []
    empty = interaction()
    await shortcut.callback(empty, reply_target())
    assert "Run a tag with /t first" in empty.response.send_message.await_args.args[0]
    assert empty.response.send_message.await_args.kwargs["ephemeral"]
    empty.response.send_modal.assert_not_awaited()
    await bot.close()


async def test_tag_reply_rechecks_tag_and_uses_public_execution(monkeypatch):
    bot = stats_bot(get_tag_by_id=AsyncMock(return_value=tag(content="edited")))
    monkeypatch.setattr(ui, "can_send_tag", lambda event: True)
    execute = AsyncMock()
    monkeypatch.setattr(ui, "execute", execute)
    target = reply_target()
    modal = ui.RecentTagModal(bot, 2, target, [tag()])
    modal.tag_select._values = ["10"]
    modal.arguments._value = "some arguments"
    attachment = SimpleNamespace(url="https://cdn.discordapp.com/example.png")
    modal.upload._values = [attachment]
    event = interaction()
    event.channel_id = 3
    await modal.on_submit(event)
    bot.db.get_tag_by_id.assert_awaited_once_with(10)
    execute.assert_awaited_once_with(
        bot,
        event,
        bot.db.get_tag_by_id.return_value,
        "some arguments",
        attachment,
        reply_message=target,
    )
    event.response.defer.assert_not_awaited()

    execute.reset_mock()
    for row in (None, tag(guild_id=9)):
        bot.db.get_tag_by_id.return_value = row
        missing = interaction()
        missing.channel_id = 3
        await modal.on_submit(missing)
        assert missing.response.send_message.await_args.args[0] == "Unknown Tag"
        execute.assert_not_awaited()

    bot.db.get_tag_by_id.reset_mock()
    stranger = interaction(user_id=8)
    stranger.channel_id = 3
    await modal.on_submit(stranger)
    assert "Only the person" in stranger.response.send_message.await_args.args[0]
    bot.db.get_tag_by_id.assert_not_awaited()

    monkeypatch.setattr(ui, "can_send_tag", lambda event: False)
    denied = interaction()
    denied.channel_id = 3
    await modal.on_submit(denied)
    assert "send messages here" in denied.response.send_message.await_args.args[0]
    bot.db.get_tag_by_id.assert_not_awaited()
    await bot.close()


async def test_tag_reply_submission_posts_publicly_and_counts_usage(monkeypatch):
    row = tag(content="Tag output")
    bot = stats_bot(get_tag_by_id=AsyncMock(return_value=row), use=AsyncMock())
    monkeypatch.setattr(ui, "can_send_tag", lambda event: True)
    target = reply_target()
    modal = ui.RecentTagModal(bot, 2, target, [row])
    modal.tag_select._values = ["10"]
    event = interaction()
    event.guild = target.guild
    event.channel = target.channel
    event.channel_id = target.channel.id
    event.filesize_limit = 20_000_000
    event.edit_original_response = AsyncMock()
    await modal.on_submit(event)
    event.response.defer.assert_awaited_once_with(ephemeral=False, thinking=True)
    bot.db.use.assert_awaited_once_with(row, 1, 2)
    output = event.edit_original_response.await_args.kwargs
    assert output["content"] == "Tag output\n-# <@4>"
    assert "Reply to:" in output["embeds"][-1].description
    assert output["allowed_mentions"].to_dict()["users"] == [4]
    assert output["view"].children[0].user_id == 2
    await bot.close()


@pytest.mark.parametrize("thread", [False, True])
def test_tag_reply_checks_channel_permissions_for_member_and_bot(thread):
    event = interaction()
    event.guild = SimpleNamespace(me=SimpleNamespace(id=99))
    event.channel = MagicMock(spec=discord.Thread if thread else discord.TextChannel)
    allowed = discord.Permissions(view_channel=True)
    if thread:
        allowed.send_messages_in_threads = True
    else:
        allowed.send_messages = True
    event.channel.permissions_for.return_value = allowed
    assert ui.can_send_tag(event)
    assert [call.args[0].id for call in event.channel.permissions_for.call_args_list] == [2, 99]
    for values in ([discord.Permissions.none(), allowed], [allowed, discord.Permissions.none()]):
        event.channel.permissions_for.side_effect = values
        assert not ui.can_send_tag(event)
    event.channel = None
    assert not ui.can_send_tag(event)


def test_modal_uses_paragraph_and_file_upload():
    bot = SimpleNamespace(max_media_mb=20, max_media_bytes=20_000_000)
    modal = TagModal(bot, 1, 2, "hello", tag(content="existing"))
    payload = modal.to_dict()
    assert [component["type"] for component in payload["components"]] == [18, 18]
    assert payload["components"][0]["component"]["value"] == "existing"
    assert payload["components"][1]["component"]["type"] == 19
    assert payload["components"][1]["component"]["max_values"] == 10
    assert payload["components"][1]["label"] == "Files (max 20 MB each)"


async def test_save_tag_appends_uploads_and_keeps_referenced_files():
    db = SimpleNamespace(
        get_tag=AsyncMock(return_value=tag(content="{attach:old.png}")),
        file_names=AsyncMock(return_value=["old.png", "unused.png"]),
        save_content=AsyncMock(),
    )
    bot = SimpleNamespace(db=db)
    event = interaction()
    await save_tag(bot, event, "hello", "{attach:old.png}", [("old.png", b"n")], tag())
    tag_id, content, new_files, kept = db.save_content.await_args.args
    assert content == "{attach:old.png}\n{attach:old-2.png}"
    assert new_files == {"old-2.png": b"n"}
    assert kept == {"old.png"}


async def test_save_tag_rejects_other_owner_and_missing_files():
    db = SimpleNamespace(
        get_tag=AsyncMock(return_value=tag(owner_id=3)),
        file_names=AsyncMock(return_value=[]),
        save_content=AsyncMock(),
        create_tag=AsyncMock(),
    )
    bot = SimpleNamespace(db=db)
    event = interaction()
    assert "already exists" in await save_tag(bot, event, "hello", "x", [], tag(owner_id=3))
    db.get_tag.return_value = None
    message = await save_tag(bot, event, "new", "{attach:ghost.png}", [], None)
    assert "ghost.png" in message
    db.create_tag.assert_not_awaited()


def test_name_candidates_fit_limit():
    assert name_candidates("hello", 3) == ["hello1", "hello2", "hello3"]
    assert name_candidates("newserver1", 2) == ["newserver2", "newserver3"]
    assert name_candidates("v9", 2) == ["v10", "v11"]
    assert name_candidates("2024", 1) == ["2025"]
    long = name_candidates("x" * 100, 1)[0]
    assert len(long) == 100 and long.endswith("x1")


def conflict_bot(existing, taken=()):
    bot = UglyTags("postgresql://localhost/test")
    bot.db = SimpleNamespace(
        get_tag=AsyncMock(return_value=existing),
        taken_names=AsyncMock(return_value=set(taken)),
        file_names=AsyncMock(return_value=[]),
        save_content=AsyncMock(),
        create_tag=AsyncMock(),
        close=AsyncMock(),
    )
    return bot


def button_event(user_id=2):
    event = interaction(user_id)
    event.edit_original_response = AsyncMock()
    return event


async def test_create_on_own_tag_asks_before_overwriting():
    bot = conflict_bot(tag(), taken={"hello1"})
    event = interaction()
    await command(bot, "create")(event, "Hello", "new body")
    bot.db.save_content.assert_not_awaited()
    bot.db.create_tag.assert_not_awaited()
    assert "`Hello2`" in event.response.send_message.await_args.args[0]
    view = event.response.send_message.await_args.kwargs["view"]
    assert [item.label for item in view.children] == ["Overwrite", "Create as Hello2", "Cancel"]

    bot.db.get_tag.return_value = tag()
    click = button_event()
    await view.overwrite.callback(click)
    assert bot.db.save_content.await_args.args[:2] == (10, "new body")
    assert "edited" in click.edit_original_response.await_args.kwargs["content"]
    await bot.close()


async def test_create_on_other_users_tag_offers_new_name_only():
    bot = conflict_bot(tag(owner_id=3))
    event = interaction()
    await command(bot, "create")(event, "hello", "body")
    view = event.response.send_message.await_args.kwargs["view"]
    assert [item.label for item in view.children] == ["Create as hello1", "Cancel"]
    bot.db.get_tag.return_value = None
    click = button_event()
    await view.create_as.callback(click)
    args = bot.db.create_tag.await_args.args
    assert (args[1], args[2], args[3], args[4]) == (1, "hello1", "body", 2)
    assert "created" in click.edit_original_response.await_args.kwargs["content"]
    await bot.close()


async def test_administrators_can_overwrite_and_edit_others_tags():
    bot = conflict_bot(tag(owner_id=3))
    event = interaction(administrator=True)
    await command(bot, "create")(event, "hello", "admin body")
    assert "<@3>" in event.response.send_message.await_args.args[0]
    view = event.response.send_message.await_args.kwargs["view"]
    assert [item.label for item in view.children] == ["Overwrite", "Create as hello1", "Cancel"]
    click = button_event()
    click.user.guild_permissions = discord.Permissions(administrator=True)
    await view.overwrite.callback(click)
    assert bot.db.save_content.await_args.args[:2] == (10, "admin body")
    event = interaction(administrator=True)
    await command(bot, "edit")(event, "hello", None)
    assert event.response.send_modal.await_args.args[0].tag["owner_id"] == 3
    bot.db.rename = AsyncMock()
    event = interaction(administrator=True)
    await command(bot, "rename")(event, "hello", "renamed")
    bot.db.rename.assert_awaited_once_with(10, "renamed")
    await bot.close()


async def test_create_conflict_without_content_opens_form():
    bot = conflict_bot(tag())
    event = interaction()
    await command(bot, "create")(event, "hello", None)
    event.response.send_modal.assert_not_awaited()
    view = event.response.send_message.await_args.kwargs["view"]
    click = button_event()
    await view.create_as.callback(click)
    modal = click.response.send_modal.await_args.args[0]
    assert (modal.name, modal.tag) == ("hello1", None)
    click = button_event()
    await view.overwrite.callback(click)
    modal = click.response.send_modal.await_args.args[0]
    assert (modal.name, modal.tag["id"]) == ("hello", 10)
    await bot.close()


@pytest.mark.parametrize("command_name", ["create", "add"])
async def test_create_without_conflict_requires_confirmation(command_name):
    bot = conflict_bot(None)
    event = interaction()
    await command(bot, command_name)(event, "fresh", "body")
    bot.db.create_tag.assert_not_awaited()
    view = event.followup.send.await_args.kwargs["view"]
    click = button_event()
    await view.confirm.callback(click)
    assert bot.db.create_tag.await_args.args[2] == "fresh"
    assert "created" in click.edit_original_response.await_args.kwargs["content"]
    await bot.close()


@pytest.mark.parametrize("editing", [False, True])
@pytest.mark.parametrize("form", [False, True])
async def test_slash_save_confirmation(editing, form, monkeypatch):
    row = tag() if editing else None
    bot = conflict_bot(row)
    save = AsyncMock(return_value="saved")
    monkeypatch.setattr(ui, "save_tag", save)
    uploads = [("file.txt", b"data")] if form else []
    monkeypatch.setattr(ui, "read_uploads", AsyncMock(return_value=uploads))
    event = interaction()
    await command(bot, "edit" if editing else "create")(event, "hello", None if form else "body")
    if form:
        modal = event.response.send_modal.await_args.args[0]
        modal.content._value = "body"
        attachment = SimpleNamespace(size=4, filename="file.txt")
        modal.upload._values = [attachment]
        submit = interaction()
        await modal.on_submit(submit)
        event = submit
    save.assert_not_awaited()
    view = event.followup.send.await_args.kwargs["view"]
    assert isinstance(view, ui.SaveConfirmation)
    ui.read_uploads.assert_not_awaited()
    if form:
        assert view.attachments == [attachment]
    click = button_event()
    await view.confirm.callback(click)
    save.assert_awaited_once_with(bot, click, "hello", "body", uploads, row)
    assert view.attachments == []
    if form:
        ui.read_uploads.assert_awaited_once_with([attachment], bot.max_media_bytes)
    await view.confirm.callback(click)
    assert save.await_count == 1
    await bot.close()


@pytest.mark.parametrize("editing", [False, True])
@pytest.mark.parametrize("action", ["cancel", "timeout"])
async def test_save_cancellation_and_timeout_do_not_write(editing, action, monkeypatch):
    save = AsyncMock()
    monkeypatch.setattr(ui, "save_tag", save)
    view = ui.SaveConfirmation(None, 2, 1, "hello", "body", [], tag() if editing else None)
    view.message = AsyncMock()
    if action == "cancel":
        await view.cancel.callback(button_event())
        await view.confirm.callback(button_event())
    else:
        await view.on_timeout()
        assert view.message.edit.await_args.kwargs["view"] is None
    assert view.attachments == []
    save.assert_not_awaited()
    view.stop()


async def test_save_confirmation_checks_owner_and_current_permissions():
    bot = conflict_bot(tag(owner_id=3))
    view = ui.SaveConfirmation(bot, 2, 1, "hello", "body", [], tag(owner_id=3))
    assert not await view.interaction_check(button_event(3))
    await view.confirm.callback(button_event())
    bot.db.save_content.assert_not_awaited()
    bot.db.create_tag.assert_not_awaited()
    await bot.close()


@pytest.mark.parametrize("prefix", [False, True])
@pytest.mark.parametrize("owner_id", [2, 3])
async def test_delete_always_confirms(prefix, owner_id):
    bot = stats_bot(
        get_tag=AsyncMock(return_value=tag(owner_id=owner_id)),
        delete_matching=AsyncMock(return_value=1),
    )
    if prefix:
        event = prefix_message('.t delete "hello"', ban_members=True)
        await ui.handle_prefix_message(bot, event)
        view = event.reply.await_args.kwargs["view"]
    else:
        event = interaction(ban_members=True)
        await command(bot, "remove", "tag")(event, "hello")
        view = event.response.send_message.await_args.kwargs["view"]
    bot.db.delete_matching.assert_not_awaited()
    click = button_event()
    click.user.guild_permissions = discord.Permissions(ban_members=True)
    await view.confirm.callback(click)
    bot.db.delete_matching.assert_awaited_once_with(1, [10])
    await view.confirm.callback(click)
    assert bot.db.delete_matching.await_count == 1
    await bot.close()


@pytest.mark.parametrize("action", ["cancel", "timeout", "lost_permission", "replaced"])
async def test_delete_does_not_remove_without_valid_confirmation(action):
    bot = stats_bot(
        get_tag=AsyncMock(return_value=tag(owner_id=3)),
        delete_matching=AsyncMock(),
    )
    event = prefix_message(".t delete hello", ban_members=True)
    await ui.handle_prefix_message(bot, event)
    view = event.reply.await_args.kwargs["view"]
    click = button_event()
    if action == "cancel":
        await view.cancel.callback(click)
        await view.confirm.callback(click)
    elif action == "timeout":
        await view.on_timeout()
        assert view.message.edit.await_args.kwargs["view"] is None
    else:
        if action == "replaced":
            bot.db.get_tag.return_value = tag(id=11)
        await view.confirm.callback(click)
        assert click.edit_original_response.await_args.kwargs["view"] is None
    bot.db.delete_matching.assert_not_awaited()
    view.stop()
    await bot.close()


@pytest.mark.parametrize("administrator", [False, True])
async def test_remove_all_deletes_only_the_confirmed_guild_snapshot(administrator):
    rows = [tag(), tag(id=11, name="second")]
    bot = stats_bot(
        search=AsyncMock(return_value=rows),
        delete_matching=AsyncMock(return_value=2),
        delete_matching_owned=AsyncMock(return_value=2),
    )
    event = interaction(administrator=administrator)
    await command(bot, "remove", "all")(event, None, None, None)
    bot.db.search.assert_awaited_once_with(
        1, name=None, content=None, owner_id=None if administrator else 2
    )
    view = event.followup.send.await_args.kwargs["view"]
    bot.db.delete_matching.assert_not_awaited()
    bot.db.search.return_value = rows + [tag(id=12, name="new", owner_id=3)]
    click = button_event()
    click.user.guild_permissions = discord.Permissions(administrator=administrator)
    await view.confirm.callback(click)
    click.response.defer.assert_awaited_once()
    if administrator:
        bot.db.delete_matching.assert_awaited_once_with(1, [10, 11])
        bot.db.delete_matching_owned.assert_not_awaited()
    else:
        bot.db.delete_matching.assert_not_awaited()
        bot.db.delete_matching_owned.assert_awaited_once_with(1, [10, 11], 2)
    assert "removed 2 tags" in click.edit_original_response.await_args.kwargs["content"]
    await view.confirm.callback(click)
    delete = bot.db.delete_matching if administrator else bot.db.delete_matching_owned
    assert delete.await_count == 1
    await bot.close()


@pytest.mark.parametrize("action", ["cancel", "timeout", "lost_permission", "owner_changed"])
async def test_remove_all_rechecks_permissions_and_can_cancel(action):
    bot = stats_bot(
        search=AsyncMock(return_value=[tag()]),
        delete_matching=AsyncMock(),
        delete_matching_owned=AsyncMock(return_value=None),
    )
    event = interaction(administrator=action != "owner_changed")
    await command(bot, "remove", "all")(event, None, None, None)
    view = event.followup.send.await_args.kwargs["view"]
    click = button_event()
    if action == "cancel":
        await view.cancel.callback(click)
        await view.confirm.callback(click)
    elif action == "timeout":
        await view.on_timeout()
        assert view.message.edit.await_args.kwargs["view"] is None
    else:
        if action == "owner_changed":
            bot.db.search.return_value = [tag(owner_id=3)]
        await view.confirm.callback(click)
        assert "Not enough permissions" in click.edit_original_response.await_args.kwargs["content"]
    bot.db.delete_matching.assert_not_awaited()
    if action == "owner_changed":
        bot.db.delete_matching_owned.assert_awaited_once_with(1, [10], 2)
    else:
        bot.db.delete_matching_owned.assert_not_awaited()
    view.stop()
    await bot.close()


async def test_edit_and_rename_require_owner():
    bot = UglyTags("postgresql://localhost/test")
    bot.db = SimpleNamespace(get_tag=AsyncMock(return_value=tag(owner_id=3)), close=AsyncMock())
    event = interaction()
    await command(bot, "edit")(event, "hello", None)
    assert "not the owner" in event.response.send_message.await_args.args[0]
    event.response.send_modal.assert_not_awaited()
    event = interaction()
    await command(bot, "rename")(event, "hello", "new")
    assert "not the owner" in event.response.send_message.await_args.args[0]
    await bot.close()


async def test_remove_permissions_follow_notsobot():
    bot = UglyTags("postgresql://localhost/test")
    bot.db = SimpleNamespace(
        get_tag=AsyncMock(return_value=tag(owner_id=3)),
        delete=AsyncMock(),
        search=AsyncMock(return_value=[tag()]),
        close=AsyncMock(),
    )
    event = interaction()
    await command(bot, "remove", "tag")(event, "hello")
    assert "not the owner" in event.response.send_message.await_args.args[0]
    event = interaction(ban_members=True)
    await command(bot, "remove", "tag")(event, "hello")
    assert event.response.send_message.await_args.kwargs["view"] is not None
    bot.db.delete.assert_not_awaited()

    event = interaction(manage_guild=True)
    other = SimpleNamespace(id=3, bot=False, mention="<@3>")
    await command(bot, "remove", "all")(event, None, None, other)
    assert "Not enough permissions" in event.response.send_message.await_args.args[0]
    event = interaction()
    await command(bot, "remove", "all")(event, None, None, None)
    assert bot.db.search.await_args.kwargs["owner_id"] == 2
    event = interaction(administrator=True)
    await command(bot, "remove", "all")(event, None, None, None)
    assert bot.db.search.await_args.kwargs["owner_id"] is None
    await bot.close()


async def test_import_limits_members_to_their_own_tags(monkeypatch):
    bot = UglyTags("postgresql://localhost/test")
    calls = []

    async def fake_import(bot, guild_id, data, owner_id):
        calls.append(owner_id)
        return SimpleNamespace(imported=1, skipped=0, invalid=0, failed_files=0, not_owned=2)

    monkeypatch.setattr("uglytags.ui.import_tags", fake_import)
    upload = SimpleNamespace(size=10, read=AsyncMock(return_value=b"[]"))
    event = interaction()
    await command(bot, "import")(event, upload)
    assert calls == [2]
    assert "Skipped 2 tags owned by other users" in event.followup.send.await_args.args[0]
    event = interaction(manage_guild=True)
    await command(bot, "import")(event, upload)
    assert calls == [2, None]
    await bot.close()


async def test_export_includes_alias_targets():
    target = tag(id=11, name="target")
    alias = tag(id=12, name="alias", content="", reference_id=11, reference_name="target")
    alias.update(reference_owner_id=2, last_used_at=None)
    target.update(reference_owner_id=None, last_used_at=None)
    db = SimpleNamespace(
        get_tag_by_id=AsyncMock(return_value=target),
        file_names=AsyncMock(return_value=[]),
    )
    event = interaction()
    event.filesize_limit = 20_000_000
    await send_export(SimpleNamespace(db=db), event, [alias], {})
    file = event.followup.send.await_args.kwargs["file"]
    names = [item["name"] for item in json.loads(file.fp.read())["tags"]]
    assert names == ["alias", "target"]


def group_bot(tags_by_name, members=None):
    bot = UglyTags("postgresql://localhost/test")
    bot.db = SimpleNamespace(
        get_tag=AsyncMock(side_effect=lambda guild, name: tags_by_name.get(name.lower())),
        taken_names=AsyncMock(return_value=set()),
        create_group=AsyncMock(),
        add_member=AsyncMock(return_value=True),
        remove_member=AsyncMock(return_value=True),
        group_members=AsyncMock(return_value=members or []),
        random_member=AsyncMock(),
        use=AsyncMock(),
        close=AsyncMock(),
    )
    return bot


async def test_group_create_add_and_remove():
    cat = tag(id=20, name="cat1")
    cats = tag(id=30, name="cats", content="", is_group=True)
    bot = group_bot({"cat1": cat})
    event = interaction()
    await command(bot, "group", "create")(event, "cats", "cat1")
    args = bot.db.create_group.await_args.args
    assert (args[1], args[2], args[3], args[4]) == (1, "cats", 2, 20)
    assert "Created group `cats`" in event.response.send_message.await_args.args[0]

    bot = group_bot({"cat1": cat, "cats": cats})
    event = interaction()
    await command(bot, "group", "create")(event, "cats", "cat1")
    assert "`cats1`" in event.response.send_message.await_args.args[0]
    bot.db.create_group.assert_not_awaited()

    event = interaction()
    await command(bot, "group", "add")(event, "cats", "cat1")
    bot.db.add_member.assert_awaited_once_with(30, 20)
    event = interaction()
    await command(bot, "group", "remove")(event, "cats", "cat1")
    bot.db.remove_member.assert_awaited_once_with(30, 20)
    await bot.close()


async def test_group_rules():
    cats = tag(id=30, name="cats", content="", is_group=True, owner_id=3)
    dogs = tag(id=31, name="dogs", content="", is_group=True)
    plain = tag(id=20, name="plain")
    bot = group_bot({"cats": cats, "dogs": dogs, "plain": plain})
    event = interaction()
    await command(bot, "group", "add")(event, "cats", "plain")
    assert "not the owner" in event.response.send_message.await_args.args[0]
    event = interaction(administrator=True)
    await command(bot, "group", "add")(event, "cats", "dogs")
    assert "cannot contain other groups" in event.response.send_message.await_args.args[0]
    event = interaction()
    await command(bot, "group", "add")(event, "plain", "dogs")
    assert "not a group" in event.response.send_message.await_args.args[0]
    event = interaction()
    await command(bot, "edit")(event, "dogs", "text")
    assert "no content" in event.response.send_message.await_args.args[0]
    bot.db.add_member.assert_not_awaited()

    bot.db.file_names = AsyncMock(return_value=[])
    event = interaction()
    await command(bot, "create")(event, "dogs", "text")
    view = event.response.send_message.await_args.kwargs["view"]
    assert [item.label for item in view.children] == ["Create as dogs1", "Cancel"]
    await bot.close()


async def test_showing_group_runs_random_member():
    cats = tag(id=30, name="cats", content="", is_group=True)
    bot = group_bot({"cats": cats})
    bot.db.random_member.return_value = tag(id=20, name="cat1", content="meow")
    event = interaction()
    event.guild, event.channel, event.filesize_limit = None, None, 10_000_000
    event.edit_original_response = AsyncMock()
    await command_t(bot)(event, "cats", None, None)
    bot.db.random_member.assert_awaited_once_with(30)
    assert [call.args for call in bot.db.use.await_args_list] == [
        (cats, 1, 2),
        (bot.db.random_member.return_value, 1, 2),
    ]
    assert event.edit_original_response.await_args.kwargs["content"] == "meow"

    bot.db.random_member.return_value = None
    event.edit_original_response.reset_mock()
    await command_t(bot)(event, "cats", None, None)
    assert "no tags" in event.edit_original_response.await_args.kwargs["content"]
    await bot.close()


def command_t(bot):
    return bot.tree.get_command("t").callback


async def test_export_includes_group_members():
    cats = tag(id=30, name="cats", content="", is_group=True)
    cat = tag(id=20, name="cat1")
    for row in (cats, cat):
        row.update(reference_owner_id=None, last_used_at=None)
    db = SimpleNamespace(
        get_tag_by_id=AsyncMock(return_value=cat),
        group_members=AsyncMock(return_value=[{"id": 20, "name": "cat1"}]),
        file_names=AsyncMock(return_value=[]),
    )
    event = interaction()
    event.filesize_limit = 20_000_000
    await send_export(SimpleNamespace(db=db), event, [cats], {})
    exported = json.loads(event.followup.send.await_args.kwargs["file"].fp.read())["tags"]
    assert [item["name"] for item in exported] == ["cats", "cat1"]
    assert exported[0]["uglytags_group_members"] == ["20"]


def test_import_limits_from_environment():
    assert parse_import_limits({}) == ImportLimits()
    limits = parse_import_limits(
        {"MAX_IMPORT_MB": "100", "MAX_IMPORT_DOWNLOADS": "3000", "MAX_IMPORT_DOWNLOAD_MB": "2000"}
    )
    assert limits == ImportLimits(100_000_000, 3000, 2_000_000_000)
    assert parse_import_limits({"MAX_IMPORT_MB": ""}) == ImportLimits()
    for name, value in (("MAX_IMPORT_DOWNLOADS", "0"), ("MAX_IMPORT_MB", "1.5")):
        with pytest.raises(ValueError, match=name):
            parse_import_limits({name: value})


async def test_import_rejects_files_over_configured_size():
    bot = UglyTags("postgresql://localhost/test", import_limits=ImportLimits(file_bytes=5_000_000))
    event = interaction(manage_guild=True)
    await command(bot, "import")(event, SimpleNamespace(size=5_000_001))
    assert "at most 5 MB" in event.response.send_message.await_args.args[0]
    await bot.close()


def test_format_size():
    assert format_size(0) == "0 B"
    assert format_size(999) == "999 B"
    assert format_size(1_500) == "1.50 KB"
    assert format_size(20_000_000) == "20.00 MB"
    assert format_size(3_250_000_000) == "3.25 GB"


def stats_bot(**methods):
    bot = UglyTags("postgresql://localhost/test")
    bot.db = SimpleNamespace(close=AsyncMock(), **methods)
    return bot


def stats_event(**permissions):
    event = interaction(**permissions)
    event.guild = SimpleNamespace(name="Server")
    return event


def ranked_row(index, **overrides):
    row = tag(id=index, name=f"t{index}", owner_id=5)
    row.update(ranked_uses=index, last_used_at=None, size=1500)
    row.update(overrides)
    return row


async def test_prune_requires_manage_server_and_criteria():
    bot = stats_bot(select_tags=AsyncMock(return_value=[]))
    event = stats_event()
    await command(bot, "prune")(event, 30, None, None, None, 7)
    assert "permission" in event.response.send_message.await_args.args[0]
    event = stats_event(manage_guild=True)
    await command(bot, "prune")(event, None, None, None, None, 7)
    assert "at least one" in event.response.send_message.await_args.args[0]
    bot.db.select_tags.assert_not_awaited()
    event = stats_event(manage_guild=True)
    owner = SimpleNamespace(id=5, display_name="Dvir")
    await command(bot, "prune")(event, 30, None, 5, owner, 7)
    assert bot.db.select_tags.await_args.kwargs == {
        "sort": "least",
        "owner_id": 5,
        "unused_days": 30,
        "max_uses": None,
        "min_age_days": 7,
        "limit": 5,
    }
    message = event.followup.send.await_args.args[0]
    assert message.startswith("No tags match. (Tags owned by Dvir, not used in 30 days")
    await bot.close()


async def test_prune_shows_every_tag_before_removing():
    rows = [ranked_row(index) for index in range(25)]
    bot = stats_bot(
        select_tags=AsyncMock(return_value=rows), delete_matching=AsyncMock(return_value=25)
    )
    event = stats_event(manage_guild=True)
    await command(bot, "prune")(event, 30, None, None, None, 7)
    kwargs = event.followup.send.await_args.kwargs
    view, embed = kwargs["view"], kwargs["embed"]
    assert event.followup.send.await_args.args[0].startswith("Review the 25 tags below")
    assert embed.title == "25 tags will be removed"
    assert embed.description.startswith("1. `t0` by <@5>\n    0 uses, last used never")
    assert "1.50 KB" in embed.description
    assert len(view.pages) == 3
    labels = [item.label for item in view.children]
    assert labels == ["<", ">", "Remove these tags", "Cancel", "Export"]

    click = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock(), edit_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )
    await view.next.callback(click)
    assert click.response.edit_message.await_args.kwargs["embed"].description.startswith("11.")
    await view.confirm.callback(click)
    bot.db.delete_matching.assert_awaited_once_with(1, [row["id"] for row in rows])
    assert "Removed 25 tags" in click.edit_original_response.await_args.kwargs["content"]
    await bot.close()


async def test_stats_commands_render():
    now = datetime.now(UTC)
    bot = stats_bot(
        server_stats=AsyncMock(
            return_value={
                "tags": 3,
                "groups": 1,
                "aliases": 0,
                "uses": 12,
                "period_uses": 4,
                "never_used": 1,
                "once": 2,
                "stale": 2,
                "stale_90": 1,
                "stale_365": 0,
                "storage": 2_500_000,
                "top_tags": [{"name": "hello", "uses": 9}],
                "top_users": [{"user_id": 5, "uses": 4}],
            }
        ),
        select_tags=AsyncMock(return_value=[ranked_row(9, name="hello", created_at=now)]),
        storage=AsyncMock(
            return_value={
                "total": 3000,
                "files": 2000,
                "file_count": 1,
                "text": 1000,
                "variables": 0,
                "top_tags": [{"name": "big", "size": 3000}],
                "top_owners": [{"owner_id": 9, "size": 3000}],
            }
        ),
    )
    names = {command.name for command in bot.tree.get_command("tag").get_command("stats").commands}
    assert names == {"overview", "tag", "member", "ranking", "storage"}

    event = stats_event()
    await command(bot, "stats", "overview")(event, 30)
    embed = event.followup.send.await_args.kwargs["embed"]
    assert embed.description == "Last 30 days"
    assert "**Storage**: 2.50 MB" in embed.fields[0].value
    assert "**Not used in 90+ days**: 1" in embed.fields[1].value
    assert "`hello`: 9 uses" in embed.fields[2].value
    assert "<@5>: 4 uses" in embed.fields[3].value
    assert bot.db.server_stats.await_args.args[1] > now - timedelta(days=31)

    event = stats_event()
    await command(bot, "stats", "ranking")(event, "least", 0, None, None, None, None)
    embed = event.followup.send.await_args.kwargs["embed"]
    assert embed.title == "Least used: 1 tags in Server"
    assert embed.description.startswith("1. `hello` by <@5>\n    9 uses, last used never")
    assert bot.db.select_tags.await_args.kwargs["sort"] == "least"

    event = stats_event()
    owner = SimpleNamespace(id=5, display_name="Dvir")
    await command(bot, "stats", "ranking")(event, "most", 7, owner, 30, 2, 14)
    kwargs = bot.db.select_tags.await_args.kwargs
    assert (kwargs["owner_id"], kwargs["unused_days"], kwargs["max_uses"]) == (5, 30, 2)
    assert kwargs["min_age_days"] == 14
    footer = event.followup.send.await_args.kwargs["embed"].footer.text
    assert "Last 7 days" in footer and "owned by Dvir" in footer

    event = stats_event()
    await command(bot, "stats", "storage")(event)
    embed = event.followup.send.await_args.kwargs["embed"]
    assert "**Total**: 3.00 KB" in embed.description
    assert "<@9>: 3.00 KB" in embed.fields[1].value
    await bot.close()


async def test_info_previews_privately_without_counting_a_use():
    bot = UglyTags("postgresql://localhost/test")
    shown = tag(name="bacon", content="crispy {args}")
    bot.db = SimpleNamespace(
        get_tag=AsyncMock(return_value=shown),
        file_names=AsyncMock(return_value=["a.mp4"]),
        use=AsyncMock(),
        close=AsyncMock(),
    )
    event = interaction()
    event.guild, event.channel, event.filesize_limit = None, None, 10_000_000
    event.edit_original_response = AsyncMock()
    await command(bot, "info")(event, "bacon", "strips", None)
    assert event.response.defer.await_args.kwargs["ephemeral"]
    bot.db.use.assert_not_awaited()
    sent = event.edit_original_response.await_args.kwargs
    assert sent["content"] == "crispy strips"
    assert sent["embeds"][0].title == "bacon"
    assert "`a.mp4`" in sent["embeds"][0].fields[0].value
    await bot.close()


def edited(message, content, minutes=1):
    message.content = content
    message.edited_at = datetime.now(UTC)
    message.created_at = message.edited_at - timedelta(minutes=minutes)
    return message


async def test_edited_command_edits_its_earlier_reply():
    bot = stats_bot(get_tag=AsyncMock(return_value=None))
    message = prefix_message(".t first")
    sent = message.reply.return_value
    await ui.handle_prefix_message(bot, message)
    message.reply.assert_awaited_once()

    await ui.handle_prefix_edit(bot, edited(message, ".t second"), None)
    message.reply.assert_awaited_once()
    sent.edit.assert_awaited_once()
    assert sent.edit.await_args.kwargs["content"] == ui.t("error.unknown_tag")

    await ui.handle_prefix_edit(bot, edited(message, ".t second"), None)
    sent.edit.assert_awaited_once()
    await bot.close()


async def test_edit_replaces_reply_that_can_no_longer_be_edited():
    bot = stats_bot(get_tag=AsyncMock(return_value=None))
    message = prefix_message(".t first")
    old = message.reply.return_value
    old.edit.side_effect = discord.HTTPException(SimpleNamespace(status=400, reason=""), "bad")
    await ui.handle_prefix_message(bot, message)
    await ui.handle_prefix_edit(bot, edited(message, ".t second"), None)
    old.delete.assert_awaited_once()
    assert message.reply.await_count == 2
    await bot.close()


async def test_edit_ignores_late_edits_and_unchanged_content():
    bot = stats_bot(get_tag=AsyncMock(return_value=None))
    message = prefix_message(".t first")
    assert not ui.is_current_edit(edited(message, ".t second", minutes=16))
    assert ui.is_current_edit(edited(message, ".t second"))
    await ui.handle_prefix_edit(bot, edited(message, ".t second"), ".t second")
    message.reply.assert_not_awaited()
    await bot.close()


async def test_editing_a_command_into_text_removes_the_reply():
    bot = stats_bot(get_tag=AsyncMock(return_value=None))
    message = prefix_message(".t first")
    sent = message.reply.return_value
    await ui.handle_prefix_message(bot, message)
    await ui.handle_prefix_edit(bot, edited(message, "never mind"), None)
    sent.delete.assert_awaited_once()
    assert ui.render.REPLIES.get(message.id) is None
    await bot.close()


async def test_deleting_a_command_removes_the_reply():
    bot = stats_bot(get_tag=AsyncMock(return_value=None))
    message = prefix_message(".t first")
    sent = message.reply.return_value
    await ui.handle_prefix_message(bot, message)
    await ui.handle_prefix_delete(message.id)
    sent.delete.assert_awaited_once()
    await ui.handle_prefix_delete(message.id)
    sent.delete.assert_awaited_once()
    await bot.close()


async def test_undo_deletes_your_last_replies_and_commands():
    bot = stats_bot(get_tag=AsyncMock(return_value=None))
    commands = [prefix_message(f".t tag{number}") for number in range(3)]
    for command in commands:
        await ui.handle_prefix_message(bot, command)
    other = prefix_message(".t other", user_id=9)
    await ui.handle_prefix_message(bot, other)

    undo = prefix_message(".undo 2")
    undo.delete = AsyncMock()
    partial = SimpleNamespace(delete=AsyncMock())
    undo.channel.get_partial_message = MagicMock(return_value=partial)
    undo.channel.permissions_for = lambda member: discord.Permissions(manage_messages=True)
    undo.guild.me = object()
    assert await ui.handle_prefix_message(bot, undo)

    assert commands[2].reply.return_value.delete.await_count == 1
    assert commands[1].reply.return_value.delete.await_count == 1
    commands[0].reply.return_value.delete.assert_not_awaited()
    other.reply.return_value.delete.assert_not_awaited()
    assert partial.delete.await_count == 2
    assert "2 commands" in undo.channel.send.await_args.args[0]
    assert undo.channel.send.await_args.kwargs["delete_after"] == ui.UNDO_NOTICE_SECONDS
    undo.delete.assert_awaited_once()
    undo.reply.assert_not_awaited()

    again = prefix_message(".undo")
    again.channel.permissions_for = lambda member: discord.Permissions.none()
    again.guild.me = object()
    await ui.handle_prefix_message(bot, again)
    assert commands[0].reply.return_value.delete.await_count == 1
    assert "Successfully deleted 1 commands" in again.reply.await_args.args[0]
    await ui.handle_prefix_message(bot, again)
    assert again.reply.await_args.args[0] == ui.t("undo.none")

    usage = prefix_message(".undo many")
    await ui.handle_prefix_message(bot, usage)
    assert "Usage:" in usage.reply.await_args.args[0]
    await bot.close()


async def test_failed_edit_resends_files_from_the_start():
    message = prefix_message(".t first")
    old = AsyncMock()

    async def fail(**kwargs):
        for file in kwargs["attachments"]:
            file.fp.read()
        raise discord.HTTPException(SimpleNamespace(status=400, reason=""), "bad")

    old.edit.side_effect = fail
    ui.render.REPLIES.put(message, old, None)
    file = ui.render.to_discord_file(ui.tagscript.TagFile("data", "a.txt"))
    await ui.render.respond(message, "hi", files=[file])
    assert message.reply.await_args.kwargs["files"][0].fp.read() == b"data"


async def test_undo_edited_from_a_replied_command_does_not_wait_on_itself():
    bot = stats_bot(get_tag=AsyncMock(return_value=None))
    message = prefix_message(".t first")
    await ui.handle_prefix_message(bot, message)
    message.channel.permissions_for = lambda member: discord.Permissions.none()
    message.guild.me = object()
    await asyncio.wait_for(ui.handle_prefix_edit(bot, edited(message, ".undo"), None), 2)
    assert message.reply.await_args.args[0] == ui.t("undo.none")
    await bot.close()


async def test_undo_with_a_huge_number_clamps_instead_of_failing():
    bot = stats_bot()
    message = prefix_message(f".undo {'9' * 5000}")
    message.channel.permissions_for = lambda member: discord.Permissions.none()
    message.guild.me = object()
    await ui.handle_prefix_message(bot, message)
    assert message.reply.await_args.args[0] == ui.t("undo.none")
    await bot.close()


async def test_stale_update_events_do_not_rerun_commands():
    message = edited(prefix_message(".t first"), ".t first")
    assert ui.is_current_edit(message)
    message.edited_at -= timedelta(minutes=5)
    message.created_at -= timedelta(minutes=5)
    assert not ui.is_current_edit(message)


async def test_edits_of_untracked_non_commands_are_ignored():
    assert not ui.is_current_edit(edited(prefix_message("hello"), "hello world"))
    assert ui.is_current_edit(edited(prefix_message("hello"), ".undo"))
    assert ui.is_prefix_command(".T x") and ui.is_prefix_command(".undo 2")
    assert not ui.is_prefix_command(".undoing") and not ui.is_prefix_command("hi")


async def test_list_reply_without_a_view_can_be_edited_and_deleted():
    bot = stats_bot(get_tag=AsyncMock(return_value=None))
    message = prefix_message(".t first")
    await ui.render.respond(message, "one", view=discord.utils.MISSING)
    sent = message.reply.return_value
    await ui.render.respond(message, "two", view=discord.utils.MISSING)
    assert sent.edit.await_args.kwargs["view"] is None
    await ui.handle_prefix_delete(message.id)
    sent.edit.return_value.delete.assert_awaited_once()
    await bot.close()


async def test_edit_without_a_tracked_reply_needs_known_changed_content():
    bot = stats_bot(get_tag=AsyncMock(return_value=None))
    message = prefix_message(".t first")
    await ui.handle_prefix_edit(bot, edited(message, ".t first"), None)
    await ui.handle_prefix_edit(bot, edited(message, ".t first"), ".t first")
    message.reply.assert_not_awaited()
    await ui.handle_prefix_edit(bot, edited(message, ".t first"), "oops")
    message.reply.assert_awaited_once()
    await bot.close()


async def test_undo_picks_replies_by_command_order_and_keeps_failed_deletes():
    bot = stats_bot(get_tag=AsyncMock(return_value=None))
    older, newer = prefix_message(".t a"), prefix_message(".t b")
    for command in (older, newer):
        await ui.handle_prefix_message(bot, command)
    await ui.handle_prefix_edit(bot, edited(older, ".t c"), None)
    newer_reply = newer.reply.return_value
    newer_reply.delete.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason=""), "no")

    undo = prefix_message(".undo")
    undo.channel.permissions_for = lambda member: discord.Permissions.none()
    undo.guild.me = object()
    await ui.handle_prefix_message(bot, undo)
    newer_reply.delete.assert_awaited_once()
    assert ui.render.REPLIES.get(newer.id) is not None
    assert "1 messages could not be deleted" in undo.reply.await_args.args[0]
    await bot.close()


async def test_undo_keeps_leading_zero_amounts():
    bot = stats_bot(get_tag=AsyncMock(return_value=None))
    commands = [prefix_message(f".t t{number}") for number in range(6)]
    for command in commands:
        await ui.handle_prefix_message(bot, command)
    undo = prefix_message(".undo 00000000000005")
    undo.channel.permissions_for = lambda member: discord.Permissions.none()
    undo.guild.me = object()
    await ui.handle_prefix_message(bot, undo)
    assert "5 commands" in undo.reply.await_args.args[0]
    await bot.close()


def test_raw_edit_event_carries_the_updated_message():
    assert "message" in discord.RawMessageUpdateEvent.__slots__
    assert "cached_message" in discord.RawMessageUpdateEvent.__slots__


def payload_for(message, before=None):
    cached = SimpleNamespace(content=before) if before is not None else None
    return SimpleNamespace(message=message, cached_message=cached, message_id=message.id)


async def test_bot_event_handlers_route_messages_edits_and_deletes():
    bot = stats_bot(get_tag=AsyncMock(return_value=None))
    message = prefix_message(".t first")
    sent = message.reply.return_value
    await bot.on_message(prefix_message("not a command"))
    await bot.on_message(message)
    message.reply.assert_awaited_once()

    await bot.on_raw_message_edit(payload_for(edited(message, "plain text"), ".t first"))
    sent.delete.assert_awaited_once()

    again = prefix_message(".t one")
    await bot.on_message(again)
    await bot.on_raw_message_delete(SimpleNamespace(message_id=again.id))
    again.reply.return_value.delete.assert_awaited_once()

    many = [prefix_message(".t a"), prefix_message(".t b")]
    for item in many:
        await bot.on_message(item)
    await bot.on_raw_bulk_message_delete(SimpleNamespace(message_ids={item.id for item in many}))
    for item in many:
        item.reply.return_value.delete.assert_awaited_once()
    assert not ui.render.REPLIES.locks
    await bot.close()


async def test_prefix_failures_reply_inside_the_message_guard(monkeypatch):
    bot = stats_bot()
    message = prefix_message(".t first")
    monkeypatch.setattr(ui, "prefix_show", AsyncMock(side_effect=RuntimeError("boom")))
    await bot.on_message(message)
    assert message.reply.await_args.args[0] == ui.t("error.generic")
    assert not ui.render.REPLIES.locks
    await bot.close()


async def test_tracker_holds_views_weakly_and_evicts_by_age_and_count(monkeypatch):
    tracker = ui.render.ReplyTracker()
    message = prefix_message(".t first")
    view = discord.ui.View()
    tracker.put(message, AsyncMock(), view)
    entry = tracker.get(message.id)
    assert entry.view() is view
    del view
    gc.collect()
    entry.stop_view()

    monkeypatch.setattr(ui.render, "MAX_TRACKED_REPLIES", 2)
    others = [prefix_message(".t x") for _ in range(3)]
    for other in others:
        tracker.put(other, AsyncMock(), None)
    assert list(tracker.entries) == [other.id for other in others[1:]]
    now = ui.render.time.monotonic()
    monkeypatch.setattr(ui.render.time, "monotonic", lambda: now + ui.render.REPLY_WINDOW + 1)
    tracker.put(message, AsyncMock(), None)
    assert list(tracker.entries) == [message.id]


async def test_undo_skips_commands_being_edited_without_waiting():
    bot = stats_bot(get_tag=AsyncMock(return_value=None))
    busy, idle = prefix_message(".t a"), prefix_message(".t b")
    for command in (busy, idle):
        await ui.handle_prefix_message(bot, command)
    undo = prefix_message(".undo 2")
    undo.channel.permissions_for = lambda member: discord.Permissions.none()
    undo.guild.me = object()
    async with ui.render.REPLIES.guard(busy.id):
        await asyncio.wait_for(ui.handle_prefix_message(bot, undo), 2)
    busy.reply.return_value.delete.assert_not_awaited()
    idle.reply.return_value.delete.assert_awaited_once()
    await bot.close()
