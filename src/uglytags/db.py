import secrets
import time
from datetime import UTC, datetime
from importlib.resources import files

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

DISCORD_EPOCH = 1420070400000
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
REGULAR = "t.reference_id IS NULL AND NOT t.is_group"
SIZE_COLUMNS = """
    octet_length(t.name) + octet_length(t.content) AS text,
    (SELECT COALESCE(sum(octet_length(f.data)), 0) FROM tag_files f WHERE f.tag_id = t.id)
        AS files,
    (SELECT count(*) FROM tag_files f WHERE f.tag_id = t.id) AS file_count,
    (SELECT COALESCE(sum(octet_length(v.name) + octet_length(v.value)), 0)
        FROM tag_variables v WHERE v.tag_id = t.id) AS variables
"""
SIZE_TOTAL = "text + files + variables"
TAG_COLUMNS = """
    t.id, t.guild_id, t.name, t.content, t.owner_id, t.reference_id, t.uses,
    t.created_at, t.edited_at, t.last_used_at, t.is_group,
    r.name AS reference_name, r.content AS reference_content, r.owner_id AS reference_owner_id,
    r.is_group AS reference_is_group
"""
TAG_FROM = "tags t LEFT JOIN tags r ON r.id = t.reference_id"
MAX_STORAGE_AMOUNT = 5
SIMILARITY = 0.3
WORD_SIMILARITY = 0.5
MAX_GROUP_MEMBERS = 100


class StorageLimitError(Exception):
    pass


class GroupFullError(Exception):
    pass


def is_group(tag: dict) -> bool:
    return bool(tag.get("is_group") or tag.get("reference_is_group"))


def target_id(tag: dict) -> int:
    return tag["reference_id"] or tag["id"]


def new_id() -> int:
    return ((int(time.time() * 1000) - DISCORD_EPOCH) << 22) | secrets.randbits(22)


def like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def filters(
    guild_id: int, name=None, content=None, owner_id=None, similar=False
) -> tuple[str, list]:
    clauses, params = ["t.guild_id = %s"], [guild_id]
    if name and similar:
        clauses.append(
            f"""(t.name ILIKE %s OR word_similarity(%s, t.name) >= {WORD_SIMILARITY}
                OR similarity(t.name, %s) > {SIMILARITY})"""
        )
        params += [like_pattern(name), name, name]
    elif name:
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

    async def similar_names(self, guild_id: int, name: str, limit: int = 3) -> list[str]:
        cursor = await self.execute(
            f"""SELECT name FROM (
                    SELECT name, uses, similarity(name, %s) AS score FROM tags WHERE guild_id = %s
                ) s WHERE score > {SIMILARITY} ORDER BY score DESC, uses DESC, lower(name)
                LIMIT %s""",
            (name, guild_id, limit),
        )
        return [row["name"] for row in await cursor.fetchall()]

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

    async def search(self, guild_id: int, *, name=None, content=None, owner_id=None, similar=False):
        where, params = filters(guild_id, name, content, owner_id, similar)
        order = "lower(t.name)"
        if name and similar:
            # Substring matches first, then the closest similar names.
            order = f"t.name NOT ILIKE %s, word_similarity(%s, t.name) DESC, {order}"
            params += [like_pattern(name), name]
        cursor = await self.execute(
            f"SELECT {TAG_COLUMNS} FROM {TAG_FROM} WHERE {where} ORDER BY {order}", params
        )
        return await cursor.fetchall()

    async def delete_matching(self, guild_id: int, tag_ids: list[int]) -> int:
        cursor = await self.execute(
            "DELETE FROM tags WHERE guild_id = %s AND id = ANY(%s)", (guild_id, tag_ids)
        )
        return cursor.rowcount

    async def delete_matching_owned(
        self, guild_id: int, tag_ids: list[int], owner_id: int
    ) -> int | None:
        async with self.pool.connection() as connection, connection.transaction():
            cursor = await connection.execute(
                """SELECT id, owner_id FROM tags
                   WHERE guild_id = %s AND id = ANY(%s) FOR UPDATE""",
                (guild_id, tag_ids),
            )
            if any(tag["owner_id"] != owner_id for tag in await cursor.fetchall()):
                return None
            cursor = await connection.execute(
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

    async def use(self, tag: dict, guild_id: int, user_id: int):
        tag_ids = [tag["id"]] + ([tag["reference_id"]] if tag.get("reference_id") else [])
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute(
                "UPDATE tags SET uses = uses + 1, last_used_at = now() WHERE id = ANY(%s)",
                (tag_ids,),
            )
            async with connection.cursor() as cursor:
                await cursor.executemany(
                    "INSERT INTO tag_uses (tag_id, guild_id, user_id) VALUES (%s, %s, %s)",
                    [(tag_id, guild_id, user_id) for tag_id in tag_ids],
                )

    async def cleanup_uses(self) -> int:
        cursor = await self.execute(
            "DELETE FROM tag_uses WHERE used_at < now() - INTERVAL '1 year'"
        )
        return cursor.rowcount

    async def recent_tags(self, guild_id: int, user_id: int):
        cursor = await self.execute(
            f"""SELECT {TAG_COLUMNS} FROM {TAG_FROM}
                JOIN (
                    SELECT tag_id, max(used_at) AS used_at FROM tag_uses
                    WHERE guild_id = %s AND user_id = %s
                        AND used_at >= now() - INTERVAL '1 year'
                    GROUP BY tag_id
                ) recent ON recent.tag_id = t.id
                WHERE t.guild_id = %s
                ORDER BY recent.used_at DESC, lower(t.name), t.id LIMIT 25""",
            (guild_id, user_id, guild_id),
        )
        return await cursor.fetchall()

    async def fetchall(self, query: str, params: tuple | list = ()):
        cursor = await self.execute(query, params)
        return await cursor.fetchall()

    async def fetchone(self, query: str, params: tuple | list = ()):
        cursor = await self.execute(query, params)
        return await cursor.fetchone()

    async def server_stats(self, guild_id: int, since: datetime | None) -> dict:
        summary = await self.fetchone(
            f"""SELECT count(*) AS tags,
                    count(*) FILTER (WHERE is_group) AS groups,
                    count(*) FILTER (WHERE reference_id IS NOT NULL) AS aliases,
                    COALESCE(sum(uses) FILTER (WHERE {REGULAR}), 0) AS uses,
                    count(*) FILTER (WHERE uses = 0) AS never_used,
                    count(*) FILTER (WHERE uses <= 1) AS once,
                    count(*) FILTER (
                        WHERE COALESCE(last_used_at, created_at) < now() - INTERVAL '30 days'
                    ) AS stale,
                    count(*) FILTER (
                        WHERE COALESCE(last_used_at, created_at) < now() - INTERVAL '90 days'
                    ) AS stale_90,
                    count(*) FILTER (
                        WHERE COALESCE(last_used_at, created_at) < now() - INTERVAL '365 days'
                    ) AS stale_365
                FROM tags t WHERE guild_id = %s""",
            (guild_id,),
        )
        period = await self.fetchone(
            f"""SELECT count(*) AS uses FROM tag_uses u JOIN tags t ON t.id = u.tag_id
                WHERE u.guild_id = %s AND u.used_at >= %s AND {REGULAR}""",
            (guild_id, since or EPOCH),
        )
        if since:
            top_tags = await self.fetchall(
                """SELECT t.name, count(*) AS uses FROM tag_uses u JOIN tags t ON t.id = u.tag_id
                   WHERE u.guild_id = %s AND u.used_at >= %s
                   GROUP BY t.id, t.name ORDER BY uses DESC, lower(t.name) LIMIT 10""",
                (guild_id, since),
            )
        else:
            top_tags = await self.fetchall(
                """SELECT name, uses FROM tags WHERE guild_id = %s AND uses > 0
                   ORDER BY uses DESC, lower(name) LIMIT 10""",
                (guild_id,),
            )
        top_users = await self.fetchall(
            f"""SELECT u.user_id, count(*) AS uses FROM tag_uses u JOIN tags t ON t.id = u.tag_id
                WHERE u.guild_id = %s AND u.used_at >= %s AND {REGULAR}
                GROUP BY u.user_id ORDER BY uses DESC LIMIT 5""",
            (guild_id, since or EPOCH),
        )
        storage = await self.storage(guild_id)
        return {
            **summary,
            "period_uses": period["uses"],
            "top_tags": top_tags,
            "top_users": top_users,
            "storage": storage["total"],
        }

    async def tag_stats(self, tag_id: int, since: datetime | None) -> dict:
        period = await self.fetchone(
            "SELECT count(*) AS uses FROM tag_uses WHERE tag_id = %s AND used_at >= %s",
            (tag_id, since or EPOCH),
        )
        top_users = await self.fetchall(
            """SELECT user_id, count(*) AS uses FROM tag_uses
               WHERE tag_id = %s AND used_at >= %s
               GROUP BY user_id ORDER BY uses DESC LIMIT 5""",
            (tag_id, since or EPOCH),
        )
        size = await self.fetchone(
            f"SELECT {SIZE_COLUMNS} FROM tags t WHERE t.id = %s",
            (tag_id,),
        )
        return {"period_uses": period["uses"], "top_users": top_users, "size": size}

    async def user_stats(self, guild_id: int, user_id: int, since: datetime | None) -> dict:
        owned = await self.fetchone(
            f"""SELECT count(*) AS tags, COALESCE(sum(uses) FILTER (WHERE {REGULAR}), 0) AS uses
                FROM tags t WHERE guild_id = %s AND owner_id = %s""",
            (guild_id, user_id),
        )
        period = await self.fetchone(
            f"""SELECT count(*) FILTER (WHERE t.owner_id = %s) AS owned_uses,
                    count(*) FILTER (WHERE u.user_id = %s) AS runs
                FROM tag_uses u JOIN tags t ON t.id = u.tag_id
                WHERE u.guild_id = %s AND u.used_at >= %s AND {REGULAR}""",
            (user_id, user_id, guild_id, since or EPOCH),
        )
        top_tags = await self.fetchall(
            f"""SELECT t.name, count(*) AS uses FROM tag_uses u JOIN tags t ON t.id = u.tag_id
                WHERE u.guild_id = %s AND u.user_id = %s AND u.used_at >= %s AND {REGULAR}
                GROUP BY t.id, t.name ORDER BY uses DESC, lower(t.name) LIMIT 10""",
            (guild_id, user_id, since or EPOCH),
        )
        storage = await self.fetchone(
            f"""SELECT COALESCE(sum({SIZE_TOTAL}), 0) AS total
                FROM (SELECT {SIZE_COLUMNS} FROM tags t
                      WHERE t.guild_id = %s AND t.owner_id = %s) sizes""",
            (guild_id, user_id),
        )
        return {
            "tags": owned["tags"],
            "uses": owned["uses"],
            "period_owned_uses": period["owned_uses"],
            "runs": period["runs"],
            "top_tags": top_tags,
            "storage": storage["total"],
        }

    async def select_tags(
        self,
        guild_id: int,
        *,
        sort: str = "most",
        since: datetime | None = None,
        owner_id: int | None = None,
        unused_days: int | None = None,
        max_uses: int | None = None,
        min_age_days: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        count = "COALESCE(p.uses, 0)" if since else "t.uses"
        order = {
            "most": f"{count} DESC, lower(t.name)",
            "least": f"{count} ASC, COALESCE(t.last_used_at, t.created_at) ASC, lower(t.name)",
            "recent": "t.last_used_at DESC NULLS LAST, lower(t.name)",
            "unused": "COALESCE(t.last_used_at, t.created_at) ASC, lower(t.name)",
            "newest": "t.created_at DESC, lower(t.name)",
            "oldest": "t.created_at ASC, lower(t.name)",
        }[sort]
        clauses, params = ["t.guild_id = %s"], [guild_id]
        if owner_id is not None:
            clauses.append("t.owner_id = %s")
            params.append(owner_id)
        if unused_days is not None:
            clauses.append(
                "(t.last_used_at IS NULL OR t.last_used_at < now() - make_interval(days => %s))"
            )
            params.append(unused_days)
        if max_uses is not None:
            clauses.append("t.uses < %s")
            params.append(max_uses)
        if min_age_days:
            clauses.append("t.created_at <= now() - make_interval(days => %s)")
            params.append(min_age_days)
        query = f"""SELECT {TAG_COLUMNS}, {count} AS ranked_uses, {SIZE_COLUMNS}
                    FROM {TAG_FROM} LEFT JOIN (
                        SELECT tag_id, count(*) AS uses FROM tag_uses
                        WHERE guild_id = %s AND used_at >= %s GROUP BY tag_id
                    ) p ON p.tag_id = t.id
                    WHERE {" AND ".join(clauses)} ORDER BY {order}"""
        params = [guild_id, since or EPOCH, *params]
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        rows = await self.fetchall(query, params)
        for row in rows:
            row["size"] = row["text"] + row["files"] + row["variables"]
        return rows

    async def storage(self, guild_id: int) -> dict:
        totals = await self.fetchone(
            f"""SELECT COALESCE(sum(files), 0) AS files, COALESCE(sum(text), 0) AS text,
                    COALESCE(sum(variables), 0) AS variables,
                    COALESCE(sum({SIZE_TOTAL}), 0) AS total,
                    COALESCE(sum(file_count), 0) AS file_count
                FROM (SELECT {SIZE_COLUMNS} FROM tags t WHERE t.guild_id = %s) sizes""",
            (guild_id,),
        )
        top_tags = await self.fetchall(
            f"""SELECT name, {SIZE_TOTAL} AS size
                FROM (SELECT t.name, {SIZE_COLUMNS} FROM tags t WHERE t.guild_id = %s) sizes
                ORDER BY size DESC, lower(name) LIMIT 10""",
            (guild_id,),
        )
        top_owners = await self.fetchall(
            f"""SELECT owner_id, sum({SIZE_TOTAL}) AS size
                FROM (SELECT t.owner_id, {SIZE_COLUMNS} FROM tags t WHERE t.guild_id = %s) sizes
                GROUP BY owner_id ORDER BY size DESC LIMIT 5""",
            (guild_id,),
        )
        return {**totals, "top_tags": top_tags, "top_owners": top_owners}

    async def autocomplete(
        self, guild_id: int, query: str, owner_id: int | None = None, groups: bool = False
    ):
        where, params = filters(guild_id, query or None, None, owner_id)
        if groups:
            where += " AND t.is_group"
        cursor = await self.execute(
            f"""SELECT t.name FROM {TAG_FROM} WHERE {where}
                ORDER BY lower(t.name) NOT LIKE lower(%s), t.uses DESC, lower(t.name) LIMIT 25""",
            [*params, f"{query}%"],
        )
        return [row["name"] for row in await cursor.fetchall()]

    async def create_group(
        self, group_id: int, guild_id: int, name: str, owner_id: int, member_id: int
    ):
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute(
                """INSERT INTO tags (id, guild_id, name, owner_id, is_group)
                   VALUES (%s, %s, %s, %s, TRUE)""",
                (group_id, guild_id, name, owner_id),
            )
            await connection.execute(
                "INSERT INTO tag_group_members (group_id, tag_id) VALUES (%s, %s)",
                (group_id, member_id),
            )

    async def add_member(self, group_id: int, tag_id: int) -> bool:
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute("SELECT 1 FROM tags WHERE id = %s FOR UPDATE", (group_id,))
            cursor = await connection.execute(
                "SELECT count(*) AS members FROM tag_group_members WHERE group_id = %s",
                (group_id,),
            )
            if (await cursor.fetchone())["members"] >= MAX_GROUP_MEMBERS:
                raise GroupFullError
            cursor = await connection.execute(
                """INSERT INTO tag_group_members (group_id, tag_id) VALUES (%s, %s)
                   ON CONFLICT DO NOTHING RETURNING tag_id""",
                (group_id, tag_id),
            )
            return await cursor.fetchone() is not None

    async def remove_member(self, group_id: int, tag_id: int) -> bool:
        cursor = await self.execute(
            "DELETE FROM tag_group_members WHERE group_id = %s AND tag_id = %s",
            (group_id, tag_id),
        )
        return cursor.rowcount > 0

    async def group_members(self, group_id: int) -> list[dict]:
        cursor = await self.execute(
            """SELECT t.id, t.name FROM tag_group_members m JOIN tags t ON t.id = m.tag_id
               WHERE m.group_id = %s ORDER BY lower(t.name)""",
            (group_id,),
        )
        return await cursor.fetchall()

    async def random_member(self, group_id: int):
        cursor = await self.execute(
            f"""SELECT {TAG_COLUMNS} FROM tag_group_members m
                JOIN tags t ON t.id = m.tag_id LEFT JOIN tags r ON r.id = t.reference_id
                WHERE m.group_id = %s ORDER BY random() LIMIT 1""",
            (group_id,),
        )
        return await cursor.fetchone()

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
                           uses, created_at, edited_at, last_used_at, is_group)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                        tag["is_group"],
                    ),
                )
                if await cursor.fetchone() is None:
                    continue
                imported.append(tag["name"])
                imported_ids.add(tag["id"])
                await self._store_files(connection, tag["id"], tag["files"], None)
            group_ids = {tag["id"] for tag in tags if tag["is_group"]}
            excluded = group_ids | {tag["id"] for tag in tags if tag["reference_id"] in group_ids}
            for tag in tags:
                if not tag["is_group"] or tag["id"] not in imported_ids:
                    continue
                members = [
                    member
                    for member in tag["members"]
                    if member in imported_ids and member not in excluded
                ]
                for member in members[:MAX_GROUP_MEMBERS]:
                    await connection.execute(
                        """INSERT INTO tag_group_members (group_id, tag_id) VALUES (%s, %s)
                           ON CONFLICT DO NOTHING""",
                        (tag["id"], member),
                    )
        return imported
