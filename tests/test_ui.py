import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from uglytags.bot import UglyTags
from uglytags.ui import TagModal, name_candidates, save_tag, send_export


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


def command(bot, *path):
    group = bot.tree.get_command("tag")
    for name in path:
        group = group.get_command(name)
    return group.callback


async def test_commands_and_intents():
    bot = UglyTags("postgresql://localhost/test")
    assert {command.name for command in bot.tree.get_commands()} == {"t", "tag"}
    show = bot.tree.get_command("t")
    assert show.guild_only
    assert [parameter.name for parameter in show.parameters] == ["name", "arguments", "attachment"]
    assert show.parameters[0].required and show.parameters[0].autocomplete
    group = bot.tree.get_command("tag")
    assert group.guild_only
    assert {command.qualified_name for command in group.walk_commands()} >= {
        "tag create",
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
    assert bot.intents.members and bot.intents.presences and bot.intents.message_content
    payload = group.to_dict(bot.tree)
    for option in payload["options"]:
        assert len(option["description"]) <= 100
    await bot.close()


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


async def test_create_without_conflict_saves_directly():
    bot = conflict_bot(None)
    event = interaction()
    await command(bot, "create")(event, "fresh", "body")
    assert bot.db.create_tag.await_args.args[2] == "fresh"
    assert "created" in event.followup.send.await_args.args[0]
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


async def test_import_requires_manage_server():
    bot = UglyTags("postgresql://localhost/test")
    event = interaction()
    await command(bot, "import")(event, SimpleNamespace(size=10))
    assert "permission" in event.response.send_message.await_args.args[0]
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
