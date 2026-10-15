import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from uglytags import components, render, tagscript


def parsed(data):
    return components.parse_component(data)


def test_component_validation_matches_notsobot_rules():
    button = parsed({"type": 2, "label": "x" * 100, "style": 9, "run": "{args}"})
    assert button == {"type": 2, "style": 1, "label": "x" * 80, "run": "{args}"}
    assert parsed({"type": 2, "url": "https://a.test", "label": "l"})["style"] == 5
    with pytest.raises(components.ComponentError, match="Action Rows must contain"):
        parsed({"type": 1, "components": []})
    with pytest.raises(components.ComponentError, match="Containers must be a Parent"):
        parsed({"type": 1, "components": [{"type": 17, "components": [{"type": 10}]}]})
    with pytest.raises(components.ComponentError, match="attachment://"):
        parsed({"type": 13, "file": {"url": "https://a.test/x"}})
    gallery = parsed({"type": 12, "items": [{"media": {"url": "https://a.test/x.png"}}]})
    assert gallery["items"][0]["media"]["url"] == "https://a.test/x.png"
    with pytest.raises(components.ComponentError, match="Thumbnails"):
        parsed({"type": 11, "media": {"url": "https://a.test/x.png"}})
    with pytest.raises(components.ComponentError, match="not both"):
        components.check_limit([{"type": 1}, {"type": 2}])
    section = {
        "type": 9,
        "components": [{"type": 10, "content": "x"}] * 3,
        "accessory": {"type": 2, "label": "a"},
    }
    container = parsed({"type": 17, "components": [section] * 14})
    with pytest.raises(components.ComponentError, match="at most 40"):
        components.check_limit([container])


def runtime():
    return tagscript.Runtime(
        client=SimpleNamespace(db=None),
        guild=None,
        channel=SimpleNamespace(id=1, name="c"),
        user=SimpleNamespace(id=7),
    )


async def result_of(script: str):
    return await tagscript.parse(runtime(), script)


TRASH_ID = "uglytags:delete:7"


def custom_ids(payload) -> list[str]:
    ids = []
    for component in payload:
        if component.get("custom_id"):
            ids.append(component["custom_id"])
        ids += custom_ids(component.get("components", []))
        if "accessory" in component:
            ids += custom_ids([component["accessory"]])
    return ids


async def test_prepare_plain_message_with_files_and_embeds():
    result = await result_of('hello{attachtext:file body}{embedjson:{"title":"t"}}')
    prepared = render.prepare(runtime(), result)
    assert prepared.content == "hello"
    assert prepared.embeds[0].title == "t"
    kwargs = prepared.edit_kwargs()
    assert kwargs["attachments"][0].filename == "content.txt"
    send_kwargs = prepared.send_kwargs()
    assert "attachments" not in send_kwargs
    assert send_kwargs["files"][0].filename == "content.txt"
    trash = prepared.view.to_components()[0]["components"][0]
    assert (trash["custom_id"], trash["style"], trash["emoji"]["name"]) == (
        TRASH_ID,
        4,
        render.TRASH_EMOJI,
    )
    assert render.prepare(runtime(), await result_of("   ")).content == "Tag returned no content"


async def test_message_result_uses_send_payload():
    sent = SimpleNamespace(edit=AsyncMock())
    message = SimpleNamespace(
        id=1,
        content=".t hi",
        author=SimpleNamespace(id=7),
        channel=SimpleNamespace(id=5),
        reply=AsyncMock(return_value=sent),
    )
    result = await result_of("hello{attachtext:file body}")
    await render.send_message_result(message, runtime(), result)
    assert message.reply.await_args.args[0] == "hello"
    kwargs = message.reply.await_args.kwargs
    assert kwargs["files"][0].filename == "content.txt"
    assert "attachments" not in kwargs
    assert not kwargs["mention_author"]


async def test_prepare_legacy_buttons_and_selects():
    row = {
        "type": 1,
        "components": [
            {"type": 2, "label": "A", "run": "a"},
            {"type": 2, "label": "Link", "url": "https://example.com"},
        ],
    }
    menu = {
        "type": 1,
        "components": [{"type": 3, "max_values": 5, "options": [{"label": "x", "value": "x"}]}],
    }
    result = await result_of(
        f"text{{componentjson:{json.dumps(row)}}}{{componentjson:{json.dumps(menu)}}}"
    )
    prepared = render.prepare(runtime(), result)
    assert isinstance(prepared.view, render.SessionView)
    payload = prepared.view.to_components()
    assert [row["type"] for row in payload] == [1, 1, 1]
    assert payload[0]["components"][1]["url"] == "https://example.com"
    assert payload[1]["components"][0]["max_values"] == 1
    assert payload[2]["components"][0]["custom_id"] == TRASH_ID
    assert prepared.content == "text"


async def test_delete_button_fits_or_is_skipped():
    button = json.dumps({"type": 2, "label": "x", "run": "y"})
    result = await result_of(f"{{componentjson:{button}}}" * 3)
    payload = render.prepare(runtime(), result).view.to_components()
    assert custom_ids(payload)[-1] == TRASH_ID
    assert len(payload) == 1
    row = json.dumps({"type": 1, "components": [json.loads(button)] * 5})
    result = await result_of(f"{{componentjson:{row}}}" * 5)
    payload = render.prepare(runtime(), result).view.to_components()
    assert TRASH_ID not in custom_ids(payload)


async def test_prepare_components_v2_moves_text_into_display():
    container = {
        "type": 17,
        "accent_color": "#ff0000",
        "components": [
            {"type": 10, "content": "inside"},
            {"type": 14, "spacing": 2},
            {
                "type": 9,
                "components": [{"type": 10, "content": "section"}],
                "accessory": {"type": 2, "label": "Go", "run": "x"},
            },
        ],
    }
    result = await result_of(
        f'text{{componentjson:{json.dumps(container)}}}{{embedjson:{{"title":"t"}}}}'
    )
    prepared = render.prepare(runtime(), result)
    assert isinstance(prepared.view, discord.ui.LayoutView)
    assert prepared.content is None
    assert prepared.embeds == []
    payload = prepared.view.to_components()
    assert (payload[0]["type"], payload[0]["content"]) == (10, "text")
    assert payload[1]["type"] == 17
    assert payload[1]["accent_color"] == 0xFF0000
    assert payload[-1]["type"] == 1
    assert payload[-1]["components"][0]["custom_id"] == TRASH_ID


async def test_delete_button_only_for_requester_and_survives_restart():
    item = render.DeleteButton(7)
    match = re.fullmatch(render.DeleteButton.__discord_ui_compiled_template__, TRASH_ID)
    restored = await render.DeleteButton.from_custom_id(None, item.item, match)
    assert restored.user_id == 7

    other = SimpleNamespace(
        user=SimpleNamespace(id=8),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        delete_original_response=AsyncMock(),
    )
    await restored.callback(other)
    assert "Only the person" in other.response.send_message.await_args.args[0]
    other.delete_original_response.assert_not_awaited()

    admin = SimpleNamespace(
        user=SimpleNamespace(id=9, guild_permissions=discord.Permissions(administrator=True)),
        response=SimpleNamespace(defer=AsyncMock()),
        delete_original_response=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await restored.callback(admin)
    admin.delete_original_response.assert_awaited_once()
    assert admin.followup.send.await_args.args[0] == "✅ Message deleted."

    owner = SimpleNamespace(
        user=SimpleNamespace(id=7),
        response=SimpleNamespace(defer=AsyncMock()),
        delete_original_response=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await restored.callback(owner)
    owner.delete_original_response.assert_awaited_once()
    assert owner.followup.send.await_args.kwargs["ephemeral"]


async def test_delete_falls_back_to_message_delete():
    owner = SimpleNamespace(
        user=SimpleNamespace(id=7),
        response=SimpleNamespace(defer=AsyncMock()),
        delete_original_response=AsyncMock(
            side_effect=discord.HTTPException(SimpleNamespace(status=404, reason="x"), "gone")
        ),
        message=SimpleNamespace(delete=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await render.DeleteButton(7).callback(owner)
    owner.message.delete.assert_awaited_once()


async def test_timeout_and_errors_keep_delete_button():
    button = json.dumps({"type": 2, "label": "x", "run": "y"})
    result = await result_of(f"{{componentjson:{button}}}")
    editor = AsyncMock()
    prepared = render.prepare(runtime(), result, editor=editor)
    await prepared.view.session.timeout()
    view = editor.await_args.kwargs["view"]
    assert custom_ids(view.to_components()) == [TRASH_ID]
    await prepared.view.session.fail("broken")
    assert custom_ids(editor.await_args.kwargs["view"].to_components()) == [TRASH_ID]


async def test_pages_attach_referenced_files():
    script = (
        "{attachtext:a}{attachtext:b}"
        '{pagejson:[{"content":"one","files":["attachment://content.txt"]},'
        '{"embed":{"title":"two","image":{"url":"attachment://content.2.txt"}}}]}'
    )
    result = await result_of(script)
    assert [[file.filename for file in page] for page in render.page_files(result)] == [
        ["content.txt"],
        ["content.2.txt"],
    ]
    prepared = render.prepare(runtime(), result)
    assert isinstance(prepared.view, render.Paginator)
    assert prepared.content == "one"
    assert prepared.view.first.disabled and not prepared.view.next.disabled
    payload = prepared.view.to_components()
    assert payload[1]["components"][0]["custom_id"] == TRASH_ID
    stranger = SimpleNamespace(
        user=SimpleNamespace(id=9),
        data={"custom_id": TRASH_ID},
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    assert await prepared.view.interaction_check(stranger)
    stranger.data = {"custom_id": "next"}
    assert not await prepared.view.interaction_check(stranger)


async def test_lead_embed_joins_plain_output_and_follows_pages():
    lead = discord.Embed(title="info")
    event = SimpleNamespace(
        user=SimpleNamespace(id=7),
        edit_original_response=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    result = await result_of('text{embedjson:{"title":"tag"}}')
    await render.send_result(event, runtime(), result, lead_embed=lead)
    embeds = event.edit_original_response.await_args.kwargs["embeds"]
    assert [embed.title for embed in embeds] == ["info", "tag"]
    event.followup.send.assert_not_awaited()

    result = await result_of('{pagejson:[{"content":"a"},{"content":"b"}]}')
    await render.send_result(event, runtime(), result, lead_embed=lead)
    assert event.edit_original_response.await_args.kwargs["embeds"] == []
    assert event.followup.send.await_args.kwargs["embed"] is lead


@pytest.mark.parametrize(
    "script",
    [
        'text{embedjson:{"title":"tag"}}{attachtext:file}',
        '{pagejson:[{"content":"a"},{"content":"b"}]}',
        '{componentjson:{"type":17,"components":[{"type":10,"content":"inside"}]}}',
    ],
)
@pytest.mark.parametrize("bot_author", [False, True])
async def test_recent_reply_card_and_ping_work_with_all_output_types(script, bot_author):
    target = SimpleNamespace(
        content="Original **message** <@8>",
        jump_url="https://discord.com/channels/1/2/3",
        author=SimpleNamespace(
            id=4,
            mention="<@4>",
            bot=bot_author,
            global_name="sour kraut",
            name="sauerkraut",
            display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/avatars/4/avatar.png"),
        ),
        attachments=[],
        embeds=[],
    )
    card = render.reply_embed(target)
    assert card.description == (
        "**[Reply to:](https://discord.com/channels/1/2/3)** Original \\*\\*message\\*\\* <@8>"
    )
    assert card.author.name == "sour kraut\u2004\u21a9\ufe0f"
    assert card.author.icon_url == target.author.display_avatar.url
    event = SimpleNamespace(edit_original_response=AsyncMock())
    result = await result_of(script)
    await render.send_result(event, runtime(), result, reply_message=target)
    kwargs = event.edit_original_response.await_args.kwargs
    mentions = kwargs["allowed_mentions"].to_dict()
    assert mentions.get("users", []) == ([] if bot_author else [4])
    assert mentions["parse"] == []
    assert not mentions.get("replied_user", False)
    view = kwargs["view"]
    assert TRASH_ID in custom_ids(view.to_components())
    if isinstance(view, discord.ui.LayoutView):
        payload = view.to_components()
        assert payload[-3]["content"] == "-# <@4>"
        assert payload[-2]["type"] == 17
        section = payload[-2]["components"][0]
        assert card.description in section["components"][0]["content"]
        assert section["accessory"]["media"]["url"] == target.author.display_avatar.url
    else:
        assert kwargs["content"].endswith("\n-# <@4>")
        assert "Reply to:" not in kwargs["content"]
        assert kwargs["embeds"][-1].to_dict() == card.to_dict()
        if isinstance(view, render.Paginator):
            click = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))
            await view.go(click, 1)
            updated = click.response.edit_message.await_args.kwargs
            assert updated["content"] == "b\n-# <@4>"
            assert updated["embeds"][-1].to_dict() == card.to_dict()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("this is a TEST do not WORRY", "this is a TEST do not WORRY\n-# <@4>"),
        (None, "-# <@4>"),
        ("first line\nsecond line", "first line\nsecond line\n-# <@4>"),
    ],
)
def test_reply_mention_matches_copycat_app_reply_format(content, expected):
    target = SimpleNamespace(
        content="original",
        jump_url="https://discord.com/channels/1/2/3",
        author=SimpleNamespace(
            mention="<@4>",
            global_name="author",
            name="author",
            display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/avatars/4/avatar.png"),
        ),
        attachments=[],
        embeds=[],
    )
    prepared = render.add_reply(render.Prepared(content=content), target)
    assert prepared.edit_kwargs()["content"] == expected


async def test_reply_card_handles_attachment_only_and_truncated_messages():
    target = SimpleNamespace(
        content="",
        jump_url="https://discord.com/channels/1/2/3",
        author=SimpleNamespace(
            global_name=None,
            name="author",
            display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/avatars/4/avatar.png"),
        ),
        attachments=[object()],
        embeds=[],
    )
    card = render.reply_embed(target)
    assert card.author.name == "author\u2004\u21a9\ufe0f"
    assert "(click to see attachment)" in card.description
    assert card.description.endswith(" \U0001f4ce")
    target.content = "x" * 101
    assert "x" * 100 + "\u2026" in render.reply_embed(target).description


@pytest.mark.parametrize("v2", [False, True])
async def test_reply_card_survives_component_execution_and_timeout(v2):
    target = SimpleNamespace(
        content="i can change her",
        jump_url="https://discord.com/channels/1/2/3",
        author=SimpleNamespace(
            id=4,
            mention="<@4>",
            global_name="sour kraut",
            name="sauerkraut",
            display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/avatars/4/avatar.png"),
        ),
        attachments=[],
        embeds=[],
    )
    button = {"type": 2, "label": "Update", "run": "changed"}
    component = {"type": 17, "components": [{"type": 1, "components": [button]}]} if v2 else button
    result = await result_of(f"initial{{componentjson:{json.dumps(component)}}}")
    result.components["on_timeout"] = "timed out"
    editor = AsyncMock()
    prepared = render.prepare(runtime(), result, editor=editor, reply_message=target)
    session = prepared.view.session
    event = SimpleNamespace(
        user=SimpleNamespace(id=7),
        response=SimpleNamespace(edit_message=AsyncMock()),
        edit_original_response=editor,
    )
    await session.run(event, "changed", None)
    changed = editor.await_args.kwargs
    assert changed["content"] == "changed\n-# <@4>"
    assert changed["embeds"][-1].description == (
        "**[Reply to:](https://discord.com/channels/1/2/3)** i can change her"
    )
    await session.timeout()
    assert editor.await_args.kwargs["content"] == "timed out\n-# <@4>"
    assert editor.await_args.kwargs["embeds"][-1].author.name.startswith("sour kraut")


async def test_reply_card_reports_capacity_errors_instead_of_dropping_output():
    target = SimpleNamespace(
        content="text",
        jump_url="https://discord.com/channels/1/2/3",
        author=SimpleNamespace(
            mention="<@4>",
            global_name=None,
            name="author",
            display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/avatars/4/avatar.png"),
        ),
        attachments=[],
        embeds=[],
    )
    result = await result_of('{embedjson:{"title":"tag"}}' * 10)
    with pytest.raises(tagscript.TagError, match="all 10 embed slots"):
        render.prepare(runtime(), result, reply_message=target)
    assert len(result.embeds) == 10
    result = await result_of('{pagejson:[{"content":"page"}]}')
    result.pages[0]["embeds"] = [discord.Embed(title="tag")] * 10
    with pytest.raises(tagscript.TagError, match="all 10 embed slots"):
        render.prepare(runtime(), result, reply_message=target)
    component = json.dumps({"type": 10, "content": "text"})
    result = await result_of(f"{{componentjson:{component}}}" * 34)
    with pytest.raises(tagscript.TagError, match="too many components"):
        render.prepare(runtime(), result, reply_message=target)
