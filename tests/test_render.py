import json
from types import SimpleNamespace

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


async def test_prepare_plain_message_with_files_and_embeds():
    result = await result_of('hello{attachtext:file body}{embedjson:{"title":"t"}}')
    prepared = render.prepare(runtime(), result)
    assert prepared.content == "hello"
    assert prepared.embeds[0].title == "t"
    kwargs = prepared.edit_kwargs()
    assert kwargs["attachments"][0].filename == "content.txt"
    assert render.prepare(runtime(), await result_of("   ")).content == "Tag returned no content"


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
    assert [row["type"] for row in payload] == [1, 1]
    assert payload[0]["components"][1]["url"] == "https://example.com"
    assert payload[1]["components"][0]["max_values"] == 1
    assert prepared.content == "text"


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
