import asyncio
import base64
import binascii
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import aiohttp
import discord

from uglytags.db import new_id
from uglytags.media import (
    ATTACH_REFERENCE,
    BYTES_PER_MB,
    MAX_FILES,
    MediaError,
    download,
    filename_from_url,
    is_discord_url,
    refresh_urls,
    safe_filename,
    unique_filename,
)

log = logging.getLogger(__name__)

MAX_CONTENT = 4000
MAX_NAME = 100
DOWNLOAD_CONCURRENCY = 4
FILES_KEY = "uglytags_files"
GROUP_KEY = "uglytags_group_members"


@dataclass(frozen=True)
class ImportLimits:
    file_bytes: int = 50 * BYTES_PER_MB
    downloads: int = 500
    download_bytes: int = 200 * BYTES_PER_MB


class TransferError(Exception):
    pass


@dataclass
class ImportReport:
    imported: int = 0
    skipped: int = 0
    invalid: int = 0
    failed_files: int = 0
    not_owned: int = 0


def clean_name(value: str) -> str:
    return value.replace("\r", "").replace("\n", "").strip()


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def export_payload(
    guild_id: int,
    tags: list[dict],
    files: dict[int, dict[str, bytes]],
    options: dict,
    groups: dict[int, list[int]] | None = None,
) -> tuple[str, bytes]:
    groups = groups or {}
    exported = []
    for tag in tags:
        reference = None
        if tag["reference_id"]:
            reference = {
                "id": str(tag["reference_id"]),
                "name": tag["reference_name"],
                "content": "",
                "server_id": str(guild_id),
                "user": {"id": str(tag["reference_owner_id"])},
            }
        exported.append(
            {
                "content": tag["content"],
                "created": _iso(tag["created_at"]),
                "edited": _iso(tag["edited_at"]),
                "global": False,
                "guild_id": str(guild_id),
                "id": str(tag["id"]),
                "is_command": False,
                "is_on_directory": False,
                "last_used": _iso(tag["last_used_at"]),
                "locked": False,
                "name": tag["name"],
                "nsfw": False,
                "reference_tag": reference,
                "server_id": str(guild_id),
                "user": {"id": str(tag["owner_id"])},
                "uses": tag["uses"],
                FILES_KEY: [
                    {"filename": name, "data": base64.b64encode(data).decode()}
                    for name, data in files.get(tag["id"], {}).items()
                ],
            }
        )
        if tag.get("is_group"):
            exported[-1][GROUP_KEY] = [str(member) for member in groups.get(tag["id"], [])]
    options = {key: value for key, value in options.items() if value}
    timestamp = int(time.time() * 1000)
    if options:
        filename = f"tags-exported-{guild_id}-{len(tags)}-filtered-{timestamp}.json"
    else:
        filename = f"tags-exported-{guild_id}-{len(tags)}.json"
    payload = {
        "options": options or None,
        "serverId": str(guild_id),
        "tags": exported,
        "timestamp": timestamp,
    }
    return filename, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def _snowflake(value) -> int | None:
    text = str(value or "")
    if not (text.isdigit() and 15 <= len(text) <= 21):
        return None
    number = int(text)
    return number if number < 2**63 else None


def _time(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, int | float) or str(value).isdigit():
        number = float(value)
        return datetime.fromtimestamp(number / 1000 if number > 1e11 else number, UTC)
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def parse_entries(data: bytes, report: ImportReport) -> list[dict]:
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, ValueError) as error:
        raise TransferError("error.import_json") from error
    raw_tags = payload.get("tags") if isinstance(payload, dict) else payload
    if not isinstance(raw_tags, list):
        raise TransferError("error.import_json")
    entries = []
    for raw in raw_tags:
        entry = _entry(raw)
        if entry is None:
            report.invalid += 1
        else:
            entries.append(entry)
    return entries


def _entry(raw) -> dict | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
        return None
    name = clean_name(raw["name"])
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    owner_id = _snowflake(user.get("id") or raw.get("user_id"))
    reference = raw.get("reference_tag") if isinstance(raw.get("reference_tag"), dict) else None
    reference_id = _snowflake(reference.get("id")) if reference else None
    content = raw.get("content") if isinstance(raw.get("content"), str) else ""
    members = raw.get(GROUP_KEY)
    if not 0 < len(name) <= MAX_NAME or owner_id is None:
        return None
    if members is not None:
        if not isinstance(members, list) or reference or content:
            return None
        members = [_snowflake(member) for member in members]
        if None in members:
            return None
    elif reference:
        if reference_id is None:
            return None
        content = ""
    elif not content.strip():
        return None
    files = {}
    for item in raw.get(FILES_KEY) or []:
        try:
            filename = unique_filename(safe_filename(item["filename"]), files)
            files[filename] = base64.b64decode(item["data"], validate=True)
        except (KeyError, TypeError, ValueError, binascii.Error):
            return None
        if not files[filename]:
            return None
    uses = raw.get("uses")
    return {
        "id": _snowflake(raw.get("id")) or new_id(),
        "name": name,
        "content": content,
        "owner_id": owner_id,
        "reference_id": reference_id,
        "uses": uses if isinstance(uses, int) and not isinstance(uses, bool) and uses >= 0 else 0,
        "created_at": _time(raw.get("created")) or datetime.now(UTC),
        "edited_at": _time(raw.get("edited")),
        "last_used_at": _time(raw.get("last_used")),
        "files": files,
        "is_group": members is not None,
        "members": members or [],
    }


def _attachment_url(match) -> str:
    return match[2].strip().removeprefix("<").removesuffix(">")


def _discord_urls(content: str) -> list[str]:
    urls = map(_attachment_url, ATTACH_REFERENCE.finditer(content))
    return list(dict.fromkeys(url for url in urls if is_discord_url(url)))


async def store_attachments(
    entries: list[dict],
    http,
    session,
    max_bytes: int,
    report: ImportReport,
    limits: ImportLimits,
):
    wanted = []
    for entry in entries:
        for url in _discord_urls(entry["content"])[: MAX_FILES - len(entry["files"])]:
            if url not in wanted:
                wanted.append(url)
    wanted = wanted[: limits.downloads]
    refreshed = {}
    for index in range(0, len(wanted), 50):
        try:
            refreshed.update(await refresh_urls(http, wanted[index : index + 50]))
        except discord.HTTPException as error:
            log.warning("Attachment URL refresh failed: %s", error)
    cache, budget = {}, limits.download_bytes
    semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

    async def fetch(url: str):
        nonlocal budget
        async with semaphore:
            if budget <= 0:
                return
            try:
                data = await download(session, refreshed.get(url, url), min(max_bytes, budget))
            except (MediaError, aiohttp.ClientError, TimeoutError) as error:
                log.info("Import attachment download failed: %s", error)
                return
            if len(data) <= budget:
                budget -= len(data)
                cache[url] = data

    await asyncio.gather(*(fetch(url) for url in wanted))
    for entry in entries:

        def replace(match, entry=entry):
            url = _attachment_url(match)
            if not is_discord_url(url):
                return match[0]
            if url not in cache or len(entry["files"]) >= MAX_FILES:
                report.failed_files += 1
                return match[0]
            filename = unique_filename(filename_from_url(url), entry["files"])
            entry["files"][filename] = cache[url]
            return match[1] + filename

        entry["content"] = ATTACH_REFERENCE.sub(replace, entry["content"])


def acceptable(entry: dict, max_bytes: int) -> bool:
    return (
        len(entry["content"]) <= MAX_CONTENT
        and len(entry["files"]) <= MAX_FILES
        and all(len(data) <= max_bytes for data in entry["files"].values())
    )


async def import_tags(bot, guild_id: int, data: bytes, owner_id: int | None = None) -> ImportReport:
    report = ImportReport()
    entries = parse_entries(data, report)
    if owner_id is not None:
        owned = [entry for entry in entries if entry["owner_id"] == owner_id]
        report.not_owned = len(entries) - len(owned)
        entries = owned
    candidates = [entry for entry in entries if acceptable(entry, bot.max_media_bytes)]
    report.invalid += len(entries) - len(candidates)
    await store_attachments(
        candidates, bot.http, bot.session, bot.max_media_bytes, report, bot.import_limits
    )
    valid = [entry for entry in candidates if acceptable(entry, bot.max_media_bytes)]
    report.invalid += len(candidates) - len(valid)
    taken = await bot.db.existing_ids([entry["id"] for entry in valid])
    used, remapped = set(taken), {}
    for entry in valid:
        old = entry["id"]
        if old in used:
            entry["id"] = new_id()
            if old in taken:
                remapped[old] = entry["id"]
        used.add(entry["id"])
    for entry in valid:
        if entry["reference_id"] in remapped:
            entry["reference_id"] = remapped[entry["reference_id"]]
        entry["members"] = [remapped.get(member, member) for member in entry["members"]]
    imported = await bot.db.import_tags(guild_id, valid)
    report.imported = len(imported)
    report.skipped = len(valid) - len(imported)
    return report
