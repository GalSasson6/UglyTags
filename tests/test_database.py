import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from importlib.resources import files

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import make_conninfo

from uglytags.db import (
    MAX_GROUP_MEMBERS,
    Database,
    GroupFullError,
    StorageLimitError,
    new_id,
)


@pytest_asyncio.fixture
async def db():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    schema = "test_" + uuid.uuid4().hex
    connection = await psycopg.AsyncConnection.connect(url, autocommit=True)
    await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    database = Database(make_conninfo(url, options=f"-c search_path={schema},public"))
    try:
        await database.open()
        await database.open()
        yield database
    finally:
        await database.close()
        await connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        await connection.close()


async def test_tag_lifecycle_and_case_insensitive_names(db):
    tag_id = new_id()
    await db.create_tag(tag_id, 1, "Hello", "hi {attach:a.png}", 7, {"a.png": b"a"})
    with pytest.raises(psycopg.errors.UniqueViolation):
        await db.create_tag(new_id(), 1, "hello", "dupe", 8)
    await db.create_tag(new_id(), 2, "hello", "other guild", 8)
    tag = await db.get_tag(1, "HELLO")
    assert (tag["id"], tag["content"], tag["owner_id"]) == (tag_id, "hi {attach:a.png}", 7)
    assert await db.file_names(tag_id) == ["a.png"]
    await db.save_content(tag_id, "new {attach:b.png}", {"b.png": b"b"}, set())
    assert await db.file_names(tag_id) == ["b.png"]
    assert await db.file(tag_id, "b.png") == b"b"
    assert (await db.get_tag(1, "hello"))["edited_at"] is not None
    await db.rename(tag_id, "Renamed")
    await db.use(await db.get_tag_by_id(tag_id), 1, 5)
    renamed = await db.get_tag_by_id(tag_id)
    assert (renamed["name"], renamed["uses"]) == ("Renamed", 1)
    assert renamed["last_used_at"] is not None


async def test_recent_tags_are_personal_guild_scoped_unique_and_limited(db):
    ids = [new_id() for _ in range(27)]
    for index, tag_id in enumerate(ids):
        await db.create_tag(tag_id, 1, f"tag{index:02}", "hi", 7)
        await db.use(await db.get_tag_by_id(tag_id), 1, 5)
        await db.execute(
            "UPDATE tag_uses SET used_at = now() - (%s * INTERVAL '1 minute') WHERE tag_id = %s",
            (index, tag_id),
        )
    await db.use(await db.get_tag_by_id(ids[4]), 1, 5)
    await db.rename(ids[4], "renamed")
    other_guild = new_id()
    await db.create_tag(other_guild, 2, "other guild", "hi", 7)
    await db.use(await db.get_tag_by_id(other_guild), 2, 5)
    await db.use(await db.get_tag_by_id(ids[26]), 1, 6)
    recent = await db.recent_tags(1, 5)
    assert len(recent) == 25
    assert [row["id"] for row in recent] == [ids[4], *ids[:4], *ids[5:25]]
    assert recent[0]["name"] == "renamed"
    assert [row["id"] for row in await db.recent_tags(1, 6)] == [ids[26]]
    assert await db.recent_tags(1, 99) == []
    await db.delete(ids[4])
    assert all(row["id"] != ids[4] for row in await db.recent_tags(1, 5))
    await db.execute(
        "UPDATE tag_uses SET used_at = now() - INTERVAL '2 years' WHERE guild_id = %s", (1,)
    )
    assert await db.recent_tags(1, 5) == []


async def test_aliases_follow_and_cascade(db):
    tag_id, alias_id = new_id(), new_id()
    await db.create_tag(tag_id, 1, "original", "content", 7)
    await db.save_alias(alias_id, 1, "short", 8, tag_id)
    alias = await db.get_tag(1, "short")
    assert alias["reference_content"] == "content"
    assert alias["reference_name"] == "original"
    await db.save_content(alias_id, "now regular", {}, set())
    assert (await db.get_tag(1, "short"))["reference_id"] is None
    await db.save_alias(new_id(), 1, "again", 8, tag_id)
    await db.delete(tag_id)
    assert await db.get_tag(1, "again") is None
    assert await db.get_tag(1, "short") is not None


async def test_search_can_include_similar_names(db):
    for guild_id, name, owner in (
        (1, "the helo", 7),
        (1, "hello world", 7),
        (1, "hello there", 8),
        (1, "banana", 7),
        (2, "hello", 7),
    ):
        await db.create_tag(new_id(), guild_id, name, "body", owner)
    assert [tag["name"] for tag in await db.search(1, name="helo")] == ["the helo"]
    similar = await db.search(1, name="helo", similar=True)
    assert [tag["name"] for tag in similar] == ["the helo", "hello there", "hello world"]
    owned = await db.search(1, name="helo", owner_id=7, similar=True)
    assert [tag["name"] for tag in owned] == ["the helo", "hello world"]


async def test_similar_names_suggests_close_tags_in_the_guild(db):
    for guild_id, name in ((1, "hello"), (1, "Help"), (1, "world"), (2, "hellos")):
        await db.create_tag(new_id(), guild_id, name, "body", 7)
    assert await db.similar_names(1, "helo") == ["hello", "Help"]
    assert await db.similar_names(1, "HELLO", limit=1) == ["hello"]
    assert await db.similar_names(1, "zzzz") == []


async def test_search_random_autocomplete_and_bulk_delete(db):
    for name, owner in (("apple", 1), ("banana", 2), ("apricot", 1), ("100%_off", 2)):
        await db.create_tag(new_id(), 1, name, f"{name} body", owner)
    assert [tag["name"] for tag in await db.search(1, name="ap")] == ["apple", "apricot"]
    assert [tag["name"] for tag in await db.search(1, owner_id=2)] == ["100%_off", "banana"]
    assert [tag["name"] for tag in await db.search(1, name="%_")] == ["100%_off"]
    assert [tag["name"] for tag in await db.search(1, content="BANANA b")] == ["banana"]
    assert (await db.random(1, owner_id=1))["name"] in {"apple", "apricot"}
    assert await db.random(1, name="zzz") is None
    assert (await db.autocomplete(1, "ap"))[0] in {"apple", "apricot"}
    assert await db.autocomplete(1, "", 2) == ["100%_off", "banana"]
    ids = [tag["id"] for tag in await db.search(1, owner_id=1)]
    assert await db.delete_matching(2, ids) == 0
    assert await db.delete_matching(1, ids) == 2
    assert len(await db.search(1)) == 2


async def test_bulk_delete_server_snapshot_and_cascades(db):
    tag_id, alias_id, other_id = new_id(), new_id(), new_id()
    await db.create_tag(tag_id, 1, "original", "{attach:a.txt}", 7, {"a.txt": b"a"})
    await db.save_alias(alias_id, 1, "alias", 8, tag_id)
    await db.create_tag(other_id, 2, "original", "other server", 7)
    await db.set_variable(tag_id, "server", 1, "count", "1")
    await db.use(await db.get_tag_by_id(tag_id), 1, 7)
    snapshot = [tag["id"] for tag in await db.search(1)]
    new_tag_id = new_id()
    await db.create_tag(new_tag_id, 1, "new", "added after confirmation prompt", 8)
    await db.delete_matching(1, snapshot + [other_id])
    assert [tag["id"] for tag in await db.search(1)] == [new_tag_id]
    assert (await db.get_tag(2, "original"))["id"] == other_id
    assert await db.file_names(tag_id) == []
    assert await db.variables(tag_id, "server", 1) == {}
    assert (await db.fetchone("SELECT count(*) AS count FROM tag_uses"))["count"] == 0
    assert await db.delete_matching(1, snapshot) == 0


async def test_bulk_delete_owned_checks_only_guild_scoped_snapshot(db):
    own_id, other_id, other_guild_id = new_id(), new_id(), new_id()
    await db.create_tag(own_id, 1, "own", "x", 7)
    await db.create_tag(other_id, 1, "other", "x", 8)
    await db.create_tag(other_guild_id, 2, "other guild", "x", 7)

    assert await db.delete_matching_owned(1, [own_id, other_id, other_guild_id], 7) is None
    assert await db.get_tag_by_id(own_id) is not None
    assert await db.get_tag_by_id(other_id) is not None
    assert await db.delete_matching_owned(1, [own_id, other_guild_id], 7) == 1
    assert await db.get_tag_by_id(own_id) is None
    assert await db.get_tag_by_id(other_id) is not None
    assert await db.get_tag_by_id(other_guild_id) is not None


async def test_storage_variables_are_scoped_and_limited(db):
    tag_id = new_id()
    await db.create_tag(tag_id, 1, "counter", "x", 7)
    await db.set_variable(tag_id, "server", 1, "count", "1")
    await db.set_variable(tag_id, "server", 1, "count", "2")
    assert await db.get_variable(tag_id, "server", 1, "count") == "2"
    assert await db.get_variable(tag_id, "user", 1, "count") is None
    for index in range(4):
        await db.set_variable(tag_id, "server", 1, f"k{index}", "v")
    with pytest.raises(StorageLimitError):
        await db.set_variable(tag_id, "server", 1, "extra", "v")
    await db.set_variable(tag_id, "server", 1, "k0", "updated")
    results = await asyncio.gather(
        db.set_variable(tag_id, "user", 5, "a", "1"),
        db.set_variable(tag_id, "user", 5, "b", "1"),
        return_exceptions=True,
    )
    assert not any(isinstance(result, Exception) for result in results)
    assert len(await db.variables(tag_id, "server", 1)) == 5
    await db.delete_variable(tag_id, "server", 1, "count")
    assert "count" not in await db.variables(tag_id, "server", 1)
    await db.delete(tag_id)
    assert await db.variables(tag_id, "user", 5) == {}


async def test_import_skips_conflicts_and_orphan_aliases(db):
    await db.create_tag(new_id(), 1, "taken", "x", 7)
    now = datetime.now(UTC)

    def entry(tag_id, name, content="body", reference_id=None, files=None, members=None):
        return {
            "id": tag_id,
            "name": name,
            "content": "" if members is not None else content,
            "owner_id": 7,
            "reference_id": reference_id,
            "uses": 2,
            "created_at": now,
            "edited_at": None,
            "last_used_at": None,
            "files": files or {},
            "is_group": members is not None,
            "members": members or [],
        }

    first, taken, alias, orphan = new_id(), new_id(), new_id(), new_id()
    group, group_alias = new_id(), new_id()
    imported = await db.import_tags(
        1,
        [
            entry(alias, "alias", "", first),
            entry(first, "first", "{attach:a.txt}", files={"a.txt": b"a"}),
            entry(taken, "TAKEN"),
            entry(orphan, "orphan", "", taken),
            entry(group, "group", members=[first, alias, taken, group_alias, group]),
            entry(group_alias, "group alias", "", group),
        ],
    )
    assert imported == ["first", "group", "alias", "group alias"]
    assert await db.file(first, "a.txt") == b"a"
    assert (await db.get_tag(1, "alias"))["reference_content"] == "{attach:a.txt}"
    assert await db.existing_ids([first, orphan]) == {first}
    assert await db.taken_names(1, ["FIRST", "Alias-2", "taken"]) == {"first", "taken"}
    assert [member["name"] for member in await db.group_members(group)] == ["alias", "first"]


async def test_groups(db):
    first, second, group = new_id(), new_id(), new_id()
    await db.create_tag(first, 1, "cat1", "meow", 7)
    await db.create_tag(second, 1, "cat2", "purr", 8)
    await db.create_group(group, 1, "Cats", 7, first)
    row = await db.get_tag(1, "cats")
    assert (row["is_group"], row["content"], row["reference_id"]) == (True, "", None)
    assert await db.add_member(group, second)
    assert not await db.add_member(group, second)
    assert await db.autocomplete(1, "", groups=True) == ["Cats"]
    assert (await db.random_member(group))["name"] in {"cat1", "cat2"}
    alias = new_id()
    await db.save_alias(alias, 1, "kitties", 7, group)
    assert (await db.get_tag(1, "kitties"))["reference_is_group"]
    await db.delete(second)
    assert [member["name"] for member in await db.group_members(group)] == ["cat1"]
    assert await db.remove_member(group, first)
    assert not await db.remove_member(group, first)
    assert await db.random_member(group) is None
    with pytest.raises(psycopg.errors.CheckViolation):
        await db.execute("UPDATE tags SET content = 'x' WHERE id = %s", (group,))
    for index in range(MAX_GROUP_MEMBERS):
        member = new_id()
        await db.create_tag(member, 1, f"m{index}", "x", 7)
        await db.add_member(group, member)
    extra = new_id()
    await db.create_tag(extra, 1, "extra", "x", 7)
    with pytest.raises(GroupFullError):
        await db.add_member(group, extra)


async def test_migrates_version_one_schema():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    schema = "test_" + uuid.uuid4().hex
    connection = await psycopg.AsyncConnection.connect(url, autocommit=True)
    await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    conninfo = make_conninfo(url, options=f"-c search_path={schema},public")
    try:
        version_one = (
            files("uglytags").joinpath("schema.sql").read_text().split("-- Migration 2")[0]
        )
        async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as old:
            await old.execute(version_one)
            await old.execute(
                """INSERT INTO tags (id, guild_id, name, content, owner_id)
                   VALUES (1, 1, 'old', 'x', 7)"""
            )
        database = Database(conninfo)
        try:
            await database.open()
            await database.open()
            assert (await database.get_tag(1, "old"))["is_group"] is False
            cursor = await database.execute(
                """SELECT conname FROM pg_constraint
                   WHERE conrelid = 'tags'::regclass AND contype = 'c'"""
            )
            names = {row["conname"] for row in await cursor.fetchall()}
            assert "tags_check" not in names and "tags_kind_check" in names
            await database.create_group(2, 1, "group", 7, 1)
        finally:
            await database.close()
    finally:
        await connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        await connection.close()


async def test_use_log_stats_and_ranking(db):
    plain, alias, group, other = new_id(), new_id(), new_id(), new_id()
    await db.create_tag(plain, 1, "hello", "hi", 7, {"a.png": b"12345"})
    await db.create_tag(other, 1, "quiet", "x" * 10, 8)
    await db.save_alias(alias, 1, "hey", 7, plain)
    await db.create_group(group, 1, "greetings", 7, plain)
    await db.use(await db.get_tag_by_id(plain), 1, 100)
    await db.use(await db.get_tag_by_id(alias), 1, 100)
    await db.use(await db.get_tag_by_id(group), 1, 200)
    await db.use(await db.get_tag_by_id(plain), 1, 200)
    hello = await db.get_tag(1, "hello")
    assert hello["uses"] == 3 and hello["last_used_at"] is not None
    await db.execute("UPDATE tag_uses SET used_at = now() - INTERVAL '40 days' WHERE user_id = 100")

    stats = await db.server_stats(1, None)
    assert (stats["tags"], stats["groups"], stats["aliases"]) == (4, 1, 1)
    assert stats["uses"] == 3
    assert stats["never_used"] == 1
    assert stats["storage"] > 5
    assert [row["user_id"] for row in stats["top_users"]] == [100, 200]
    recent = await db.server_stats(1, datetime.now(UTC) - timedelta(days=30))
    assert recent["period_uses"] == 1
    assert [row["user_id"] for row in recent["top_users"]] == [200]

    tag_stats = await db.tag_stats(plain, datetime.now(UTC) - timedelta(days=30))
    assert tag_stats["period_uses"] == 1
    assert tag_stats["size"]["files"] == 5 and tag_stats["size"]["file_count"] == 1
    user = await db.user_stats(1, 200, None)
    assert (user["tags"], user["runs"], user["top_tags"][0]["name"]) == (0, 1, "hello")
    owner = await db.user_stats(1, 7, None)
    assert owner["tags"] == 3 and owner["uses"] == 3 and owner["storage"] > 5

    async def ranking(sort, **options):
        return await db.select_tags(1, sort=sort, **options)

    assert [row["name"] for row in await ranking("most")][0] == "hello"
    assert [row["name"] for row in await ranking("least")][0] == "quiet"
    assert (await ranking("recent"))[-1]["last_used_at"] is None
    assert [row["name"] for row in await ranking("most", owner_id=8)] == ["quiet"]
    hello_row = (await ranking("most"))[0]
    assert (hello_row["ranked_uses"], hello_row["size"]) == (3, 5 + len("hello") + len("hi"))
    recent_rows = await ranking("most", since=datetime.now(UTC) - timedelta(days=30))
    assert {row["name"]: row["ranked_uses"] for row in recent_rows}["hello"] == 1
    assert stats["once"] == 3 and stats["stale_90"] == 0

    await db.execute("UPDATE tag_uses SET used_at = now() - INTERVAL '2 years' WHERE user_id = 100")
    assert await db.cleanup_uses() == 3
    assert (await db.tag_stats(plain, None))["period_uses"] == 1


async def test_prune_candidates_and_storage(db):
    ids = {}
    for name, uses, last_days, age_days in (
        ("fresh", 0, None, 1),
        ("never", 0, None, 30),
        ("stale", 5, 100, 200),
        ("busy", 50, 1, 200),
        ("rare", 1, 10, 200),
    ):
        ids[name] = new_id()
        await db.create_tag(ids[name], 1, name, "x", 7)
        await db.execute(
            """UPDATE tags SET uses = %s, created_at = now() - make_interval(days => %s),
                   last_used_at = CASE WHEN %s::int IS NULL THEN NULL
                       ELSE now() - make_interval(days => %s) END
               WHERE id = %s""",
            (uses, age_days, last_days, last_days or 0, ids[name]),
        )

    async def names(**options):
        options.setdefault("min_age_days", 7)
        least_used = options.pop("least_used", None)
        rows = await db.select_tags(1, sort="least", limit=least_used, **options)
        return [tag["name"] for tag in rows]

    assert await names(unused_days=30) == ["never", "stale"]
    assert await names(unused_days=30, min_age_days=0) == ["never", "fresh", "stale"]
    assert await names(max_uses=2) == ["never", "rare"]
    assert await names(least_used=2) == ["never", "rare"]
    assert await names(unused_days=5, max_uses=10) == ["never", "rare", "stale"]
    assert await names(unused_days=5, least_used=1) == ["never"]
    assert await names(unused_days=5, owner_id=99) == []

    await db.create_tag(new_id(), 2, "elsewhere", "x" * 1000, 9, {"big.bin": b"0" * 2000})
    storage = await db.storage(2)
    assert storage["files"] == 2000 and storage["file_count"] == 1
    assert storage["total"] == storage["files"] + storage["text"] + storage["variables"]
    assert storage["top_tags"][0]["name"] == "elsewhere"
    assert storage["top_owners"][0]["owner_id"] == 9
    assert (await db.storage(1))["files"] == 0
