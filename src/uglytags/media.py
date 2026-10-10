import re
import time
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlsplit

import aiohttp
import discord

BYTES_PER_MB = 1_000_000
DEFAULT_MAX_MEDIA_MB = 20
MAX_FILES = 10
MAX_FILENAME = 100
DISCORD_CDN_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}
ATTACHMENT_URL = re.compile(
    r"https?://(?:media\.discordapp\.net|cdn\.discordapp\.com)/attachments/[0-9]*/[0-9]*/"
    r"[A-Za-z0-9_.-]*(?:\?[a-zA-Z0-9&=]*)?"
)
ATTACH_REFERENCE = re.compile(
    r"(\{\s*(?:attachment|attach|file|attachmentspoiler|attachspoiler|filespoiler)\s*:)"
    r"([^|{}]*)",
    re.IGNORECASE,
)


class MediaError(Exception):
    pass


def parse_media_limit_mb(value: str | None) -> int:
    try:
        limit = DEFAULT_MAX_MEDIA_MB if value is None else int(value)
    except ValueError as error:
        raise ValueError("MAX_MEDIA_MB must be a whole number") from error
    if limit <= 0:
        raise ValueError("MAX_MEDIA_MB must be greater than zero")
    return limit


def safe_filename(name: str) -> str:
    name = PurePosixPath(name.replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "file"
    stem, dot, extension = name.rpartition(".")
    if not dot:
        return name[:MAX_FILENAME]
    extension = extension[:16]
    return f"{stem[: MAX_FILENAME - len(extension) - 1]}.{extension}"


def unique_filename(name: str, taken) -> str:
    candidate, number = name, 2
    stem, dot, extension = name.rpartition(".")
    if not dot:
        stem, extension = name, ""
    while candidate.lower() in {value.lower() for value in taken}:
        suffix = f"-{number}"
        candidate = f"{stem[: MAX_FILENAME - len(suffix) - len(extension) - 1]}{suffix}"
        candidate += f".{extension}" if extension else ""
        number += 1
    return candidate


def is_discord_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname in DISCORD_CDN_HOSTS


def attachment_expired(url: str, now: float | None = None) -> bool:
    if not ATTACHMENT_URL.fullmatch(url):
        return False
    expires = parse_qs(urlsplit(url).query).get("ex")
    try:
        return not expires or int(expires[0], 16) <= (now or time.time())
    except ValueError:
        return True


async def refresh_urls(http, urls: list[str]) -> dict[str, str]:
    expired = list(dict.fromkeys(url for url in urls if attachment_expired(url)))[:50]
    if not expired:
        return {}
    route = discord.http.Route("POST", "/attachments/refresh-urls")
    response = await http.request(route, json={"attachment_urls": expired})
    return {item["original"]: item["refreshed"] for item in response.get("refreshed_urls", [])}


async def download(session: aiohttp.ClientSession, url: str, max_bytes: int) -> bytes:
    if not is_discord_url(url):
        raise MediaError("Only Discord attachment and avatar URLs can be attached")
    timeout = aiohttp.ClientTimeout(total=30)
    async with session.get(url, allow_redirects=False, timeout=timeout) as response:
        if response.status != 200:
            raise MediaError(f"Attachment download failed with status {response.status}")
        if response.content_length is not None and response.content_length > max_bytes:
            raise MediaError(f"Attachment surpassed max file size of {max_bytes} bytes")
        data = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            data.extend(chunk)
            if len(data) > max_bytes:
                raise MediaError(f"Attachment surpassed max file size of {max_bytes} bytes")
    if not data:
        raise MediaError("Attachment is empty")
    return bytes(data)


def filename_from_url(url: str) -> str:
    return safe_filename(PurePosixPath(urlsplit(url).path).name or "file")


def referenced_files(content: str) -> set[str]:
    return {
        match[2].strip()
        for match in ATTACH_REFERENCE.finditer(content)
        if match[2].strip() and "://" not in match[2]
    }


def attach_references(names: list[str]) -> str:
    return "".join(f"{{attach:{name}}}" for name in names)


async def read_uploads(attachments, max_bytes: int) -> list[tuple[str, bytes]]:
    uploads = []
    for attachment in attachments:
        if not 0 < attachment.size <= max_bytes:
            raise MediaError("error.media_size")
        data = await attachment.read()
        if not 0 < len(data) <= max_bytes:
            raise MediaError("error.media_size")
        uploads.append((safe_filename(attachment.filename), data))
    return uploads
