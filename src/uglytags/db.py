import secrets
import time
from importlib.resources import files

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

DISCORD_EPOCH = 1420070400000
TAG_COLUMNS = """
    t.id, t.guild_id, t.name, t.content, t.owner_id, t.reference_id, t.uses,
    t.created_at, t.edited_at, t.last_used_at,
    r.name AS reference_name, r.content AS reference_content, r.owner_id AS reference_owner_id
"""
TAG_FROM = "tags t LEFT JOIN tags r ON r.id = t.reference_id"
MAX_STORAGE_AMOUNT = 5


class StorageLimitError(Exception):
    pass


def new_id() -> int:
    return ((int(time.time() * 1000) - DISCORD_EPOCH) << 22) | secrets.randbits(22)


def like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def filters(guild_id: int, name=None, content=None, owner_id=None) -> tuple[str, list]:
    clauses, params = ["t.guild_id = %s"], [guild_id]
    if name:
        clauses.append("t.name ILIKE %s")
        params.append(like_pattern(name))
    if content:
        clauses.append("COALESCE(r.content, t.content) ILIKE %s")
        params.append(like_pattern(content))
    if owner_id is not None:
        clauses.append("t.owner_id = %s")
        params.append(owner_id)
    return " AND ".join(clauses), params


class Database:
    def __init__(self, url: str):
        self.pool = AsyncConnectionPool(
            url,
            min_size=1,
            max_size=5,
            open=False,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )

    async def open(self):
        await self.pool.open(wait=True)
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(481975002)")
            await connection.execute(files("uglytags").joinpath("schema.sql").read_text())

    async def close(self):
        await self.pool.close()

    async def execute(self, query: str, params: tuple | list = ()):
        async with self.pool.connection() as connection:
            return await connection.execute(query, params)

    async def get_tag(self, guild_id: int, name: str):
        cursor = await self.execute(
            f"""SELECT {TAG_COLUMNS} FROM {TAG_FROM}
                WHERE t.guild_id = %s AND lower(t.name) = lower(%s)""",
            (guild_id, name),
        )
        return await cursor.fetchone()

    async def get_tag_by_id(self, tag_id: int):
        cursor = await self.execute(
            f"SELECT {TAG_COLUMNS} FROM {TAG_FROM} WHERE t.id = %s", (tag_id,)
        )
        return await cursor.fetchone()

    async def create_tag(
        self,
        tag_id: int,
        guild_id: int,
        name: str,
        content: str,
        owner_id: int,
        new_files: dict[str, bytes] | None = None,
    ):
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute(
                """INSERT INTO tags (id, guild_id, name, content, owner_id)
                   VALUES (%s, %s, %s, %s, %s)""",
                (tag_id, guild_id, name, content, owner_id),
            )
            await self._store_files(connection, tag_id, new_files or {}, None)

    async def save_content(
        self,
        tag_id: int,
        content: str,
        new_files: dict[str, bytes],
        keep: set[str],
    ):
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute(
                """UPDATE tags SET content = %s, reference_id = NULL, edited_at = now()
                   WHERE id = %s""",
                (content, tag_id),
            )
            await self._store_files(connection, tag_id, new_files, keep)

    async def _store_files(self, connection, tag_id: int, new_files: dict[str, bytes], keep):
        for filename, data in new_files.items():
            await connection.execute(
                """INSERT INTO tag_files (tag_id, filename, data) VALUES (%s, %s, %s)
                   ON CONFLICT (tag_id, filename) DO UPDATE SET data = EXCLUDED.data""",
                (tag_id, filename, data),
            )
        if keep is not None:
            await connection.execute(
                "DELETE FROM tag_files WHERE tag_id = %s AND NOT (filename = ANY(%s))",
                (tag_id, list(keep | set(new_files))),
            )

    async def save_alias(
        self, tag_id: int, guild_id: int, name: str, owner_id: int, reference_id: int
    ):
        await self.execute(
            """INSERT INTO tags (id, guild_id, name, owner_id, reference_id)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET reference_id = EXCLUDED.reference_id,
                   edited_at = now()""",
            (tag_id, guild_id, name, owner_id, reference_id),
        )

    async def rename(self, tag_id: int, name: str):
        await self.execute(
            "UPDATE tags SET name = %s, edited_at = now() WHERE id = %s", (name, tag_id)
        )

    async def delete(self, tag_id: int):
        await self.execute("DELETE FROM tags WHERE id = %s", (tag_id,))

    async def search(self, guild_id: int, *, name=None, content=None, owner_id=None):
        where, params = filters(guild_id, name, content, owner_id)
        cursor = await self.execute(
            f"SELECT {TAG_COLUMNS} FROM {TAG_FROM} WHERE {where} ORDER BY lower(t.name)", params
        )
        return await cursor.fetchall()

    async def delete_matching(self, guild_id: int, tag_ids: list[int]) -> int:
        cursor = await self.execute(
            "DELETE FROM tags WHERE guild_id = %s AND id = ANY(%s)", (guild_id, tag_ids)
        )
        return cursor.rowcount

    async def random(self, guild_id: int, *, name=None, content=None, owner_id=None):
        where, params = filters(guild_id, name, content, owner_id)
        cursor = await self.execute(
            f"SELECT {TAG_COLUMNS} FROM {TAG_FROM} WHERE {where} ORDER BY random() LIMIT 1",
            params,
        )
        return await cursor.fetchone()

    async def use(self, tag_id: int):
        await self.execute(
            "UPDATE tags SET uses = uses + 1, last_used_at = now() WHERE id = %s", (tag_id,)
        )

    async def autocomplete(self, guild_id: int, query: str, owner_id: int | None = None):
        where, params = filters(guild_id, query or None, None, owner_id)
        cursor = await self.execute(
            f"""SELECT t.name FROM {TAG_FROM} WHERE {where}
                ORDER BY lower(t.name) NOT LIKE lower(%s), t.uses DESC, lower(t.name) LIMIT 25""",
            [*params, f"{query}%"],
        )
        return [row["name"] for row in await cursor.fetchall()]

    async def file_names(self, tag_id: int) -> list[str]:
        cursor = await self.execute(
            "SELECT filename FROM tag_files WHERE tag_id = %s ORDER BY filename", (tag_id,)
        )
        return [row["filename"] for row in await cursor.fetchall()]

    async def file(self, tag_id: int, filename: str) -> bytes | None:
        cursor = await self.execute(
            "SELECT data FROM tag_files WHERE tag_id = %s AND filename = %s", (tag_id, filename)
        )
        row = await cursor.fetchone()
        return row["data"] if row else None

    async def get_variable(self, tag_id: int, scope: str, storage_id: int, name: str):
        cursor = await self.execute(
            """SELECT value FROM tag_variables
               WHERE tag_id = %s AND scope = %s AND storage_id = %s AND name = %s""",
            (tag_id, scope, storage_id, name),
        )
        row = await cursor.fetchone()
        return row["value"] if row else None

    async def set_variable(self, tag_id: int, scope: str, storage_id: int, name: str, value: str):
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute("SELECT 1 FROM tags WHERE id = %s FOR UPDATE", (tag_id,))
            cursor = await connection.execute(
                """SELECT count(*) FILTER (WHERE name <> %s) AS others FROM tag_variables
                   WHERE tag_id = %s AND scope = %s AND storage_id = %s""",
                (name, tag_id, scope, storage_id),
            )
            if (await cursor.fetchone())["others"] >= MAX_STORAGE_AMOUNT:
                raise StorageLimitError
            await connection.execute(
                """INSERT INTO tag_variables (tag_id, scope, storage_id, name, value)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (tag_id, scope, storage_id, name)
                   DO UPDATE SET value = EXCLUDED.value""",
                (tag_id, scope, storage_id, name, value),
            )

    async def delete_variable(self, tag_id: int, scope: str, storage_id: int, name: str):
        await self.execute(
            """DELETE FROM tag_variables
               WHERE tag_id = %s AND scope = %s AND storage_id = %s AND name = %s""",
            (tag_id, scope, storage_id, name),
        )

    async def variables(self, tag_id: int, scope: str, storage_id: int) -> dict[str, str]:
        cursor = await self.execute(
            """SELECT name, value FROM tag_variables
               WHERE tag_id = %s AND scope = %s AND storage_id = %s ORDER BY name""",
            (tag_id, scope, storage_id),
        )
        return {row["name"]: row["value"] for row in await cursor.fetchall()}

    async def existing_ids(self, tag_ids: list[int]) -> set[int]:
        cursor = await self.execute("SELECT id FROM tags WHERE id = ANY(%s)", (tag_ids,))
        return {row["id"] for row in await cursor.fetchall()}

    async def taken_names(self, guild_id: int, names: list[str]) -> set[str]:
        cursor = await self.execute(
            "SELECT lower(name) AS name FROM tags WHERE guild_id = %s AND lower(name) = ANY(%s)",
            (guild_id, [name.lower() for name in names]),
        )
        return {row["name"] for row in await cursor.fetchall()}

    async def import_tags(self, guild_id: int, tags: list[dict]) -> list[str]:
        imported, imported_ids = [], set()
        async with self.pool.connection() as connection, connection.transaction():
            for tag in sorted(tags, key=lambda tag: tag["reference_id"] is not None):
                if tag["reference_id"] is not None and tag["reference_id"] not in imported_ids:
                    continue
                cursor = await connection.execute(
                    """INSERT INTO tags (id, guild_id, name, content, owner_id, reference_id,
                           uses, created_at, edited_at, last_used_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING RETURNING id""",
                    (
                        tag["id"],
                        guild_id,
                        tag["name"],
                        tag["content"],
                        tag["owner_id"],
                        tag["reference_id"],
                        tag["uses"],
                        tag["created_at"],
                        tag["edited_at"],
                        tag["last_used_at"],
                    ),
                )
                if await cursor.fetchone() is None:
                    continue
                imported.append(tag["name"])
                imported_ids.add(tag["id"])
                await self._store_files(connection, tag["id"], tag["files"], None)
        return imported
