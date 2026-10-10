import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from uglytags import tagscript
from uglytags.db import StorageLimitError
from uglytags.tagscript import Runtime, TagError, parse, split, string_arguments


class FakeDB:
    def __init__(self, tags=None, files=None):
        self.tags = {tag["id"]: tag for tag in tags or []}
        self.files = files or {}
        self.storage = {}
        self.use = AsyncMock()

    async def get_tag_by_id(self, tag_id):
        return self.tags.get(tag_id)

    async def file(self, tag_id, filename):
        return self.files.get((tag_id, filename))

    async def get_variable(self, tag_id, scope, storage_id, name):
        return self.storage.get((tag_id, scope, storage_id), {}).get(name)

    async def set_variable(self, tag_id, scope, storage_id, name, value):
        values = self.storage.setdefault((tag_id, scope, storage_id), {})
        if name not in values and len(values) >= 5:
            raise StorageLimitError
        values[name] = value

    async def delete_variable(self, tag_id, scope, storage_id, name):
        self.storage.get((tag_id, scope, storage_id), {}).pop(name, None)

    async def variables(self, tag_id, scope, storage_id):
        return dict(self.storage.get((tag_id, scope, storage_id), {}))


def make_tag(tag_id=111111111111111111, content="", name="test", reference=None):
    return {
        "id": tag_id,
        "guild_id": 1,
        "name": name,
        "content": content,
        "owner_id": 7,
        "reference_id": reference,
        "reference_content": None,
        "created_at": datetime.now(UTC),
    }


def runtime(db=None, tag=None, **kwargs):
    user = SimpleNamespace(
        id=7, name="bob", display_name="Bob", mention="<@7>", discriminator="0", global_name=None
    )
    guild = SimpleNamespace(id=1, name="Server", member_count=3, channels=[], members=[])
    return Runtime(
        client=SimpleNamespace(db=db or FakeDB()),
        guild=guild,
        channel=SimpleNamespace(id=5, name="general", is_nsfw=lambda: False),
        user=user,
        tag=tag or make_tag(),
        **kwargs,
    )


async def run(value, args="", rt=None):
    return (await parse(rt or runtime(), value, args)).text


def test_split_respects_nesting_escapes_and_amount():
    assert split("a|b|c") == ["a", "b", "c"]
    assert split("a|{b|c}|d") == ["a", "{b|c}", "d"]
    assert split("a\\|b|c") == ["a|b", "c"]
    assert split("a|b|c|d", 2) == ["a", "b|c|d"]
    assert split("a|{x}|c|d", 2) == ["a", "{x}|c|d"]
    assert split("no splitter") == ["no splitter"]


def test_string_arguments_support_quotes():
    assert string_arguments('one "two three" four') == ["one", "two three", "four"]
    assert string_arguments("\u201cfancy quotes\u201d x") == ["fancy quotes", "x"]
    assert string_arguments("") == []


@pytest.mark.parametrize(
    ("script", "args", "expected"),
    [
        ("hi {args}", "a b", "hi a b"),
        ("{arg:1}|{arg:-1}|{argslen}", 'a "b c" d', "b c|d|3"),
        ("{args:1|3}", "a b c d", "b c"),
        ("{argsafe}", "a|b", "a\\|b"),
        ("{if:{arg}|=|yes|then:Y|else:N}", "yes", "Y"),
        ("{if:{arg}|=|yes|then:Y|else:N}", "no", "N"),
        ("{if:10|>|9|then:big|finally:!}", "", "big!"),
        ("{if:ABC|~|abc|then:same}", "", "same"),
        ("{if:1.50|~|1.5|then:same}", "", "same"),
        ("{if:a|bad|b|then:x}", "", "{if:a|bad|b|then:x}"),
        ("{set:x|5}{get:x}{get:missing|fallback}", "", "5fallback"),
        ("{set:x|1}{delete:x}{get:x|gone}", "", "gone"),
        ("{and:a|b|c}{and:a||c}{or:|x|y}", "", "cx"),
        ("{includes:{arg}|a|b}{includes:c|a|b}", "b", "truefalse"),
        ("{iferror:{math:1/}|fallback}", "", "fallback"),
        ("{evalsilent:{math:(}}after", "", "after"),
        ("{foreach:3|[{get:idx}]}", "", "[0][1][2]"),
        ('{foreach:["x","y"]|{get:idx}={get:valuex};}', "", "0=x;1=y;"),
        ('{foreach:{"a":1}|{get:idx}:{get:valuex}}', "", "a:1"),
        ("{lower:ABC}{upper:abc}{reverse:abc}{len:hello}", "", "abcABCcba5"),
        ("{substring:hello|1|3}{substring:hello|3|1}{indexof:l|hello}", "", "elel2"),
        ("{repeat:3|ab}", "", "ababab"),
        ("{replace:o|0|hello world}", "", "hell0 w0rld"),
        ("{replace:(\\w+) (\\w+)|with:$2 $1|in:hello world}", "", "world hello"),
        ("{replace:L|with:$&$&|in:hello}", "", "hellllo"),
        ("{url:a b&c}", "", "a%20b%26c"),
        ('{jsonify:a"b}', "", '"a\\"b"'),
        ("{traversejson:a.b[1]|{bracketleft}}", "", "{traversejson:a.b[1]|{}"),
        ('{traversejson:a.b[1]|{"a":{"b":[1,2]}}}', "", "2"),
        ("{type:number|12abc}{type:float|1.5x}{type:number|x|7}", "", "121.57"),
        ("{max:1|5|3}{min:4|2}{abs:-3}{cos:0}", "", "5231"),
        ("{math:2 + 2 * 3}|{math:10/4}|{math:1/3}|{math:2^10}", "", "8|2.5|0.33|1024"),
        ("{markupbold:x}{markupcodestring:y}{markupspoiler:z}", "", "**x**`y`||z||"),
        ("{markuptime:1700000000000|R}", "", "<t:1700000000:R>"),
        ("{markuplistnumbered:a|b}", "", "1. a\n2. b"),
        ("{oneof:hel|world|hello}", "", "hello"),
        ("{unknown:thing} {ignore:{args}} {note:hidden}done", "z", "{unknown:thing} {args} done"),
        ("{newline}x{bracketleft}{bracketright}{splitterargument}", "", "\nx{}|"),
        ("{eval:{bracketleft}args{bracketright}}", "q", "q"),
        ("{unixsnowflake:175928847299117063}", "", "1462015105796"),
        ("{tagname} {tagid} {tagownerid} {ismaintag} {isfromcomponent}", "", None),
        (
            "{username} {userid} {usernick} {channel} {prefix} {guild}",
            "",
            "bob 7 Bob general / Server",
        ),
    ],
)
async def test_tagscript_functions(script, args, expected):
    result = await run(script, args)
    if expected is None:
        assert result == "test 111111111111111111 7 true false"
    else:
        assert result == expected


async def test_exit_discards_or_keeps_output():
    assert await run("a{exit:bye}b") == "bye"
    assert await run("a{exit}b") == "Tag Exited"
    assert await run("a{exitsilent:bye}b") == "abye"
    assert await run("{iferror:{exit:stop}|x}after") == "stop"


async def test_choose_and_range_stay_in_bounds():
    for _ in range(20):
        assert await run("{choose:a|b}") in {"a", "b"}
        assert 1 <= int(await run("{range:1|3}")) <= 3
        assert 0 <= int(await run("{range:2}")) <= 2
    assert len(await run("{uuid}")) == 36
    assert (await run("{uuid:7}"))[14] == "7"


async def test_iteration_limit_stops_evaluation():
    rt = runtime()
    script = "{set:a|{args}}" * 3
    result = await parse(rt, script, "", None, tagscript.Limits(iterations=2))
    assert result.text == "{set:a|{args}}" * 2


async def test_variable_and_text_limits():
    with pytest.raises(TagError, match="private"):
        await run("{set:__args|x}")
    limits = tagscript.Limits(variables=1)
    with pytest.raises(TagError, match="max variable"):
        await parse(runtime(), "{set:a|1}{set:b|2}", "", None, limits)
    with pytest.raises(TagError, match="Text exceeded"):
        await run("{repeat:20|aaaaaaaaaa}", rt=runtime(max_file_size=100))
    with pytest.raises(TagError, match="not supported"):
        await run("{ai:hello}")
    with pytest.raises(TagError, match="not supported"):
        await run("{getglobal:x}")
    with pytest.raises(TagError, match="Text exceeded"):
        await run(
            "{replace:.|with:xxxxxxxxxx|in:aaaaaaaaaaaaaaaaaaaa}", rt=runtime(max_file_size=100)
        )


def test_natural_language_times():
    now = 1_000_000.0
    assert tagscript.parse_time("tomorrow", now) == (now + 86400) * 1000
    assert tagscript.parse_time("in 2 hours", now) == (now + 7200) * 1000
    assert tagscript.parse_time("5 minutes ago", now) == (now - 300) * 1000
    assert tagscript.parse_time("an hour ago", now) == (now - 3600) * 1000
    assert tagscript.parse_time("1700000000000") == 1700000000000
    assert tagscript.parse_time("2024-01-01T00:00:00Z") == 1704067200000
    assert tagscript.parse_time("2 hours") is None
    assert tagscript.parse_time("whenever") is None


async def test_persistent_storage_is_scoped_and_limited():
    db = FakeDB()
    rt = runtime(db)
    assert await run("{setserver:count|1}{getserver:count}", rt=rt) == "1"
    assert await run("{getuser:count|none}{getchannel:count|none}", rt=rt) == "nonenone"
    assert await run("{setuser:a|x}{variablesuser}", rt=rt) == '{"a":"x"}'
    assert await run("{deleteuser:a}{getuser:a|gone}", rt=rt) == "gone"
    for index in range(4):
        await run(f"{{setserver:k{index}|v}}", rt=rt)
    with pytest.raises(TagError, match="max server storage"):
        await run("{setserver:extra|v}", rt=rt)
    alias = runtime(db, make_tag(222222222222222222, reference=111111111111111111))
    assert await run("{getserver:count}", rt=alias) == "1"


async def test_nested_tag_runs_with_its_own_arguments():
    child = make_tag(333333333333333333, "child:{args}:{tagname}", name="child")
    db = FakeDB([child])
    rt = runtime(db)
    assert await run("{tag:333333333333333333|x y} {tagname} {args}", "z", rt) == (
        "child:x y:child test z"
    )
    db.use.assert_awaited_once_with(333333333333333333)
    other = make_tag(444444444444444444, "x")
    other["guild_id"] = 2
    db.tags[other["id"]] = other
    with pytest.raises(TagError, match="Cannot call"):
        await run("{tag:444444444444444444}", rt=rt)
    with pytest.raises(TagError, match="Invalid Tag Id"):
        await run("{tag:name}", rt=rt)
    loop = make_tag(555555555555555555, "{tag:555555555555555555}")
    db.tags[loop["id"]] = loop
    with pytest.raises(TagError, match="too many tag executions"):
        await run("{tag:555555555555555555}", rt=rt)


async def test_stored_and_text_attachments():
    db = FakeDB(files={(111111111111111111, "cat.png"): b"png"})
    result = await parse(runtime(db), "{attach:cat.png|kitty.png|a cat}{attachtext:hello}")
    assert [file.filename for file in result.files] == ["kitty.png", "content.2.txt"]
    assert result.files[0].data == b"png"
    assert result.files[0].description == "a cat"
    result = await parse(runtime(db), "{attachspoiler:cat.png}{getfilename:1}")
    assert result.files[0].spoiler
    assert result.text == "cat.png"
    result = await parse(runtime(db), "{attach:cat.png}{setfilename:1|dog}{getfilename:1}")
    assert result.text == "dog.png"
    with pytest.raises(TagError, match="does not exist"):
        await parse(runtime(db), "{attach:missing.png}")
    with pytest.raises(TagError, match="Only Discord"):
        await parse(runtime(db, cache={}), "{attach:https://example.com/a.png}")


async def test_embeds_pages_and_components():
    result = await parse(runtime(), '{embedjson:{"title":"Hi","color":255}}text')
    assert result.embeds[0].title == "Hi"
    assert result.text == "text"
    assert (await parse(runtime(), "{embedjson:{}}")).text == "{embedjson:{}}"
    result = await parse(runtime(), '{pagejson:[{"content":"a"},{"embed":{"title":"b"}}]}')
    assert [page["content"] for page in result.pages] == ["a", None]
    with pytest.raises(TagError, match="Invalid Page"):
        await parse(runtime(), '{pagejson:[{"nothing":1}]}')
    button = json.dumps({"type": 2, "label": "Go", "run": "clicked"})
    result = await parse(runtime(), f"{{componentjson:{button}}}{{componentsontimeout:late}}")
    assert result.components["components"][0]["run"] == "clicked"
    assert result.components["on_timeout"] == "late"
    with pytest.raises(TagError, match="url or a label"):
        await parse(runtime(), '{componentjson:{"type":2}}')
    with pytest.raises(TagError, match="Invalid Component"):
        await parse(runtime(), "{componentjson:not json}")


async def test_nsfw_requires_age_restricted_channel():
    with pytest.raises(TagError, match="NSFW"):
        await run("{nsfw}")
    rt = runtime()
    rt.channel.is_nsfw = lambda: True
    assert await run("{nsfw}ok", rt=rt) == "ok"


async def test_attachment_option_feeds_media_functions():
    rt = runtime(attachment_url="https://cdn.discordapp.com/attachments/1/2/a.png")
    assert await run("{image}", rt=rt) == rt.attachment_url
    rt = runtime()
    assert await run("{image:https://example.com/x.png}", rt=rt) == "https://example.com/x.png"
    assert await run("{image:<:smile:123456789012345678>}", rt=rt) == (
        "https://cdn.discordapp.com/emojis/123456789012345678.png"
    )
