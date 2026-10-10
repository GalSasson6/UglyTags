import asyncio
import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from psycopg.conninfo import make_conninfo

from uglytags.db import Database, StorageLimitError, new_id


@pytest_asyncio.fixture
async def db():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    schema = "test_" + uuid.uuid4().hex
    connection = await psycopg.AsyncConnection.connect(url, autocommit=True)
    await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    database = Database(make_conninfo(url, options=f"-c search_path={schema}"))
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
    await db.use(tag_id)
    renamed = await db.get_tag_by_id(tag_id)
    assert (renamed["name"], renamed["uses"]) == ("Renamed", 1)
    assert renamed["last_used_at"] is not None


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

    def entry(tag_id, name, content="body", reference_id=None, files=None):
        return {
            "id": tag_id,
            "name": name,
            "content": content,
            "owner_id": 7,
            "reference_id": reference_id,
            "uses": 2,
            "created_at": now,
            "edited_at": None,
            "last_used_at": None,
            "files": files or {},
        }

    first, taken, alias, orphan = new_id(), new_id(), new_id(), new_id()
    imported = await db.import_tags(
        1,
        [
            entry(alias, "alias", "", first),
            entry(first, "first", "{attach:a.txt}", files={"a.txt": b"a"}),
            entry(taken, "TAKEN"),
            entry(orphan, "orphan", "", taken),
        ],
    )
    assert imported == ["first", "alias"]
    assert await db.file(first, "a.txt") == b"a"
    assert (await db.get_tag(1, "alias"))["reference_content"] == "{attach:a.txt}"
    assert await db.existing_ids([first, orphan]) == {first}
    assert await db.taken_names(1, ["FIRST", "Alias-2", "taken"]) == {"first", "taken"}
