import difflib
import json
import math
import random
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import discord
import regex

from uglytags import components as component_tools
from uglytags.db import StorageLimitError, is_group, target_id
from uglytags.mathexpr import MathError, evaluate
from uglytags.media import (
    MediaError,
    attachment_expired,
    download,
    filename_from_url,
    is_discord_url,
    refresh_urls,
    safe_filename,
)
from uglytags.serialize import (
    channel_json,
    guild_json,
    member_json,
    member_or_user_json,
    message_json,
    user_json,
)

BRACKET_LEFT = "{"
BRACKET_RIGHT = "}"
IGNORE = "\\"
SPLITTER_ARGUMENT = "|"
SPLITTER_FUNCTION = ":"
PRIVATE_PREFIX = "__"
SPLITTER = re.compile(r"(?<!\\)\|")

AI_EXECUTIONS = "__aiExecutions"
ARGS = "__args"
ARGS_STRING = "__argsString"
COMPONENT_EXECUTIONS = "__componentExecutions"
FILE_SIZE = "__fileSize"
FILES = "__files"
IS_FROM_CHILD_PARSING = "__isFromChildParsing"
ITERATIONS_REMAINING = "__iterationsRemaining"
NETWORK_REQUESTS = "__networkRequests"
PARENT_TAG_ID = "__parentTagId"
SETTINGS = "__settings"
TAG_EXECUTIONS = "__tagExecutions"
VALUES = "__values"
ID_X = "idx"
VALUE_X = "valuex"
PRIVATE_VARIABLES = (
    AI_EXECUTIONS,
    ARGS,
    ARGS_STRING,
    COMPONENT_EXECUTIONS,
    FILE_SIZE,
    IS_FROM_CHILD_PARSING,
    ITERATIONS_REMAINING,
    NETWORK_REQUESTS,
    PARENT_TAG_ID,
    SETTINGS,
    TAG_EXECUTIONS,
    VALUES,
    ID_X,
    VALUE_X,
)
DEFAULTS = {
    AI_EXECUTIONS: int,
    COMPONENT_EXECUTIONS: int,
    FILE_SIZE: int,
    IS_FROM_CHILD_PARSING: int,
    NETWORK_REQUESTS: int,
    PARENT_TAG_ID: str,
    SETTINGS: dict,
    TAG_EXECUTIONS: int,
}
SETTING_ALLOW_EVERYONE = "ALLOW_COMPONENT_EXECUTIONS_FROM_EVERYONE"
SETTING_NATIVE_MEDIA = "DISPLAY_MEDIA_AS_NATIVE"
IGNORED_SETTINGS = {
    "AI_MODEL",
    "AI_PERSONALITY",
    "MEDIA_AV_FALLBACK",
    "MEDIA_IV_FALLBACK",
    "ML_IMAGINE_DO_NOT_ERROR",
    "ML_IMAGINE_MODEL",
}
QUOTES = {
    '"': '"',
    "'": "'",
    "\u2019": "\u2019",
    "\u201a": "\u201b",
    "\u201c": "\u201d",
    "\u201e": "\u201f",
    "\u300c": "\u300d",
    "\u300e": "\u300f",
    "\u301d": "\u301e",
    "\ufe41": "\ufe42",
    "\ufe43": "\ufe44",
    "\uff02": "\uff02",
    "\uff62": "\uff63",
    "\u00ab": "\u00bb",
    "\u300a": "\u300b",
    "\u3008": "\u3009",
}
TIMESTAMP_STYLES = {
    "BOTH_LONG": "F",
    "BOTH_SHORT": "f",
    "DATE_LONG": "D",
    "DATE_SHORT": "d",
    "RELATIVE": "R",
    "TIME_LONG": "T",
    "TIME_SHORT": "t",
}
IMAGE_EXTENSIONS = {"bmp", "heic", "gif", "ico", "jpg", "jpeg", "png", "raw", "tiff", "webp"}
VIDEO_EXTENSIONS = {"mov", "mp4", "webm", "mkv", "avi", "m4v"}
AUDIO_EXTENSIONS = {"flac", "mp3", "wav", "ogg", "m4a", "opus"}
UNSUPPORTED = {
    "ai",
    "api.create.reminder",
    "api.search.duckduckgo.images",
    "api.search.imgur",
    "api.utilities.locations",
    "api.utilities.weather",
    "attachmentvoice",
    "attachvoice",
    "filevoice",
    "identify",
    "interrogate",
    "ocr",
    "edit",
    "editurl",
    "imagine",
    "imagineurl",
    "nsfwfilter",
    "request",
    "translate",
    "download",
    "text",
    "downloadfromhtml",
    "textfromhtml",
    "transcribe",
    "getglobal",
    "setglobal",
    "variablesglobal",
    *(
        f"{name}{suffix}"
        for name in ("mediascript", "mscript", "imagescript", "iscript")
        for suffix in ("", "maybeurl", "url")
    ),
    *(
        f"{prefix}.{engine}"
        for prefix in ("search", "s")
        for engine in (
            "duckduckgo.images",
            "ddg.images",
            "google.images",
            "g.images",
            "youtube",
            "yt",
        )
    ),
}
MENTION = re.compile(r"<@!?(\d{15,21})>|(\d{15,21})")
CHANNEL_MENTION = re.compile(r"<#(\d{15,21})>|(\d{15,21})")
CUSTOM_EMOJI = re.compile(r"<(a?):\w+:(\d{15,21})>")
CODEBLOCK = re.compile(r"```(?:([a-z0-9-]+?)\n+)?\n*([\s\S]+?)\n*```", re.IGNORECASE)
SNOWFLAKE = re.compile(r"\d{15,21}")
JS_INT = re.compile(r"\s*([+-]?)(0[xX][0-9a-fA-F]+|\d+)")
JS_FLOAT = re.compile(r"\s*([+-]?(?:Infinity|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?))")
JS_NUMBER = re.compile(r"[+-]?(?:Infinity|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")
DISCORD_EPOCH = 1420070400000


class TagError(Exception):
    pass


class TagExit(Exception):
    def __init__(self, message: str, silent: bool = False):
        super().__init__(message)
        self.message = message
        self.silent = silent


@dataclass
class Limits:
    attachments: int = 10
    component_executions: int = 2
    embeds: int = 10
    iterations: int = 450
    network_requests: int = 15
    pages: int = 1000
    storage_key_length: int = 128
    storage_value_length: int = 16384
    tag_executions: int = 2
    regex_seconds: float = 0.1
    variable_key_length: int = 64
    variable_length: int = 1024 * 1024
    variables: int = 100


@dataclass
class TagFile:
    data: bytes | str
    filename: str
    description: str | None = None
    spoiler: bool = False
    url: str = ""


@dataclass
class TagResult:
    files: list
    variables: dict
    limits: Limits
    text: str = ""
    embeds: list = field(default_factory=list)
    components: dict | None = None
    pages: list = field(default_factory=list)


@dataclass
class Runtime:
    client: Any
    guild: Any
    channel: Any
    user: Any
    tag: dict | None = None
    attachment_url: str | None = None
    max_file_size: int = 10 * 1024 * 1024
    cache: dict = field(default_factory=dict)

    @property
    def db(self):
        return self.client.db


FUNCTIONS = {}
LAZY = {}


def function(*names: str, lazy: bool = False):
    def register(handler):
        for name in names:
            (LAZY if lazy else FUNCTIONS)[name] = handler
        return handler

    return register


def js_parse_int(value: str) -> int | None:
    match = JS_INT.match(value or "")
    if not match:
        return None
    sign, digits = match.groups()
    number = int(digits, 16) if digits[:2].lower() == "0x" else int(digits)
    return -number if sign == "-" else number


def js_parse_float(value: str) -> float | None:
    match = JS_FLOAT.match(value or "")
    return float(match[1].replace("Infinity", "inf")) if match else None


def js_number(value: str) -> float | None:
    text = value.strip()
    if not text:
        return 0.0
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", text):
        return float(int(text, 16))
    if JS_NUMBER.fullmatch(text):
        return float(text.replace("Infinity", "inf"))
    return None


def js_str(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if value == 0:
            return "0"
        if 1e-7 <= abs(value) < 1e21:
            text = format(Decimal(repr(value)), "f")
            return text[:-2] if text.endswith(".0") else text
        mantissa, _, exponent = repr(value).partition("e")
        mantissa = mantissa[:-2] if mantissa.endswith(".0") else mantissa
        exponent_value = int(exponent)
        return f"{mantissa}e{'+' if exponent_value > 0 else '-'}{abs(exponent_value)}"
    if isinstance(value, dict | list):
        return to_json(value)
    return str(value)


def to_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def js_substring(text: str, start: int, end: int | None) -> str:
    length = len(text)
    start = min(max(start, 0), length)
    end = length if end is None else min(max(end, 0), length)
    if start > end:
        start, end = end, start
    return text[start:end]


def split(value: str, amount: int = 0) -> list[str]:
    if SPLITTER_ARGUMENT not in value:
        return [value]
    depth, position, text, args = 0, 0, "", []
    while position < len(value):
        if amount and amount <= len(args):
            if args:
                args[-1] += value[position - 1 :]
            break
        if depth == 0 and not text:
            next_left = value.find(BRACKET_LEFT, position)
            if next_left == -1:
                remaining = value[position:]
                parts = SPLITTER.split(remaining)
                if amount and len(parts) > amount - len(args):
                    consumed = 0
                    for part in parts[: amount - len(args) - 1]:
                        args.append(part.replace("\\|", "|"))
                        consumed += len(part) + 1
                    args.append(remaining[consumed:])
                else:
                    args.extend(part.replace("\\|", "|") for part in parts)
                position = len(value)
                continue
        character = value[position]
        position += 1
        text += character
        if character == SPLITTER_ARGUMENT:
            if depth <= 0:
                args.append(text[:-1])
                text = ""
        elif character == IGNORE:
            following = value[position : position + 1]
            if following == BRACKET_LEFT:
                depth -= 1
            elif following == BRACKET_RIGHT:
                depth += 1
            elif following == SPLITTER_ARGUMENT:
                text = text[:-1] + SPLITTER_ARGUMENT
                position += 1
        elif character == BRACKET_LEFT:
            depth += 1
        elif character == BRACKET_RIGHT:
            depth -= 1
    if text:
        args.append(text)
    return args


def string_arguments(value: str) -> list[str]:
    results = []
    while value:
        result, value = value[0], value[1:]
        if result in QUOTES:
            index = value.find(QUOTES[result], 1)
            if index != -1:
                results.append(value[:index])
                value = value[index + 1 :].strip()
                continue
        index = value.find(" ")
        if index == -1:
            result, value = result + value, ""
        else:
            result, value = result + value[:index], value[index:].strip()
        results.append(result)
    return results


def parse_inner(value: str) -> tuple[str, str]:
    value = value[1:-1].strip()
    name, splitter, arg = value.partition(SPLITTER_FUNCTION)
    return name.lower(), arg if splitter else ""


def count_network(tag: TagResult, amount: int = 1):
    tag.variables[NETWORK_REQUESTS] += amount
    if tag.limits.network_requests < tag.variables[NETWORK_REQUESTS]:
        raise TagError(
            "Tag attempted to use too many network requests "
            f"(Max {tag.limits.network_requests:,} Requests)"
        )


def count_tag_execution(tag: TagResult):
    tag.variables[TAG_EXECUTIONS] += 1
    if tag.limits.tag_executions < tag.variables[TAG_EXECUTIONS]:
        raise TagError(
            f"Tag attempted to use too many tag executions (Max {tag.limits.tag_executions:,} "
            "Executions)"
        )


def count_component_execution(tag: TagResult):
    tag.variables[COMPONENT_EXECUTIONS] += 1
    if tag.limits.component_executions < tag.variables[COMPONENT_EXECUTIONS]:
        raise TagError(
            "Tag attempted to use too many Component Executions "
            f"(Max {tag.limits.component_executions:,} Executions)"
        )


def reset_limits(tag: TagResult):
    tag.variables.update(
        {
            ITERATIONS_REMAINING: tag.limits.iterations,
            AI_EXECUTIONS: 0,
            COMPONENT_EXECUTIONS: 0,
            NETWORK_REQUESTS: 0,
            FILE_SIZE: 0,
            TAG_EXECUTIONS: 0,
        }
    )
    tag.files = []


def normalize(main: TagResult, other: TagResult, content: bool = True):
    if content:
        main.text += other.text
    if other.components:
        if main.components:
            main.components["components"] += other.components["components"]
            check_components(main.components)
            if other.components.get("on_timeout"):
                main.components["on_timeout"] = other.components["on_timeout"]
        else:
            main.components = other.components
    if main.limits.embeds < len(main.embeds) + len(other.embeds):
        raise TagError(f"Embeds surpassed max embeds length of {main.limits.embeds}")
    main.embeds.extend(other.embeds)
    if other.files is not main.files:
        main.files.extend(file for file in other.files if file not in main.files)
    if main.limits.attachments < len(main.files):
        raise TagError(f"Attachments surpassed max attachments length of {main.limits.attachments}")
    if main.limits.pages < len(main.pages) + len(other.pages):
        raise TagError(f"Pages surpassed max page length of {main.limits.pages}")
    main.pages.extend(other.pages)


def check_components(components: dict):
    try:
        component_tools.check_limit(components["components"])
    except component_tools.ComponentError as error:
        raise TagError(str(error)) from error


async def parse_value(rt: Runtime, tag: TagResult, value: str) -> str:
    if BRACKET_LEFT in value:
        result = await parse(rt, value, "", tag.variables, tag.limits, tag.files)
        normalize(tag, result, False)
        return result.text
    return value.strip()


async def parse(
    rt: Runtime,
    value: str,
    args: str = "",
    variables: dict | None = None,
    limits: Limits | None = None,
    files: list | None = None,
) -> TagResult:
    variables = {} if variables is None else variables
    limits = limits or Limits()
    first = ITERATIONS_REMAINING not in variables
    if first:
        variables[ITERATIONS_REMAINING] = limits.iterations
        variables[ARGS_STRING] = args
        variables[ARGS] = string_arguments(args)
    for key, default in DEFAULTS.items():
        variables.setdefault(key, default())
    tag = TagResult(files=[] if files is None else files, variables=variables, limits=limits)
    variables[ITERATIONS_REMAINING] -= 1

    depth, buffer, position = 0, "", 0
    while position < len(value):
        if rt.max_file_size < len(tag.text):
            raise TagError(f"Text exceeded {rt.max_file_size} bytes")
        if variables[ITERATIONS_REMAINING] <= 0:
            tag.text += value[position:]
            break
        if depth == 0:
            next_left = value.find(BRACKET_LEFT, position)
            if next_left == -1:
                tag.text += value[position:]
                break
            tag.text += value[position:next_left]
            position = next_left
        check_limits(tag)
        character = value[position]
        position += 1
        buffer += character
        if character == IGNORE:
            following = value[position : position + 1]
            if following == BRACKET_LEFT:
                depth -= 1
            elif following == BRACKET_RIGHT:
                depth += 1
        elif character == BRACKET_LEFT:
            depth += 1
        elif character == BRACKET_RIGHT:
            depth -= 1
            if depth <= 0:
                try:
                    await dispatch(rt, tag, buffer)
                except TagExit as error:
                    if not first:
                        raise
                    if error.silent:
                        tag.text += error.message
                    else:
                        tag.components = None
                        tag.embeds = []
                        tag.files.clear()
                        tag.pages = []
                        tag.text = error.message or "Tag Exited"
                    buffer = ""
                    break
                buffer = ""
                if rt.max_file_size < len(tag.text):
                    raise TagError(f"Text exceeded {rt.max_file_size} bytes")
    tag.text += buffer
    if first:
        tag.text = tag.text.replace("\u200b", "\n")
    return tag


def check_limits(tag: TagResult):
    if tag.limits.tag_executions < tag.variables[TAG_EXECUTIONS]:
        raise TagError(
            f"Tag attempted to use too many tag executions (Max {tag.limits.tag_executions:,} "
            "Executions)"
        )
    if tag.limits.component_executions < tag.variables[COMPONENT_EXECUTIONS]:
        raise TagError(
            "Tag attempted to use too many Component Executions "
            f"(Max {tag.limits.component_executions:,} Executions)"
        )


async def dispatch(rt: Runtime, tag: TagResult, buffer: str):
    name, arg = parse_inner(buffer)
    if name in LAZY:
        if not await LAZY[name](rt, arg, tag):
            tag.text += buffer
        return
    if name == "ignore":
        tag.text += arg
        return
    if name == "note":
        return
    text = await parse_value(rt, tag, arg)
    if arg != text:
        arg = text
        splitter = buffer.find(SPLITTER_FUNCTION)
        if splitter != -1:
            buffer = buffer[:splitter] + SPLITTER_FUNCTION + arg + BRACKET_RIGHT
    if name in FUNCTIONS:
        if not await FUNCTIONS[name](rt, arg, tag):
            tag.text += buffer
    elif name in UNSUPPORTED:
        raise TagError(f"{{{name}}} is not supported by this bot")
    else:
        tag.text += buffer


def repeat_symbol(symbol: str):
    async def handler(rt, arg, tag):
        if arg:
            amount = js_parse_int(arg)
            if amount is None:
                return False
            if amount <= 0 or tag.limits.variable_length < amount:
                raise TagError(
                    "Cannot repeat character less than 0 or more than "
                    f"{tag.limits.variable_length} times"
                )
            tag.text += symbol * amount
        else:
            tag.text += symbol
        return True

    return handler


for _names, _symbol in (
    (("bracketleft",), BRACKET_LEFT),
    (("bracketright",), BRACKET_RIGHT),
    (("newline",), "\u200b"),
    (("splitterargument",), SPLITTER_ARGUMENT),
    (("splitterfunction",), SPLITTER_FUNCTION),
):
    function(*_names)(repeat_symbol(_symbol))


def escape_splitters(value: str) -> str:
    return re.sub(r"(?<!\\)\|", r"\\|", value)


def argument_index(arg: str, args: list[str]) -> int | None:
    index = js_parse_int(arg or "0")
    if index is None:
        return None
    return len(args) + index if index < 0 else index


@function("arg")
async def arg_function(rt, arg, tag):
    args = tag.variables[ARGS]
    index = argument_index(arg, args)
    if index is None:
        return False
    if 0 <= index < len(args):
        tag.text += args[index]
    return True


@function("argsafe")
async def arg_safe(rt, arg, tag):
    args = tag.variables[ARGS]
    index = argument_index(arg, args)
    if index is None:
        return False
    if 0 <= index < len(args):
        tag.text += escape_splitters(args[index])
    return True


def sliced_arguments(arg: str, tag: TagResult) -> str | None:
    args = tag.variables[ARGS]
    if not arg:
        return tag.variables[ARGS_STRING]
    start_text, *rest = split(arg, 2)
    start = argument_index(start_text, args)
    if start is None:
        return None
    stop = js_parse_int(rest[0]) if rest and rest[0] else len(args)
    if stop is None:
        stop = len(args)
    if stop < 0:
        stop += len(args)
    return " ".join(args[max(start, 0) : max(stop, 0)])


@function("args")
async def args_function(rt, arg, tag):
    text = sliced_arguments(arg, tag)
    if text is None:
        return False
    tag.text += text
    return True


@function("argssafe")
async def args_safe(rt, arg, tag):
    text = sliced_arguments(arg, tag)
    if text is None:
        return False
    tag.text += escape_splitters(text)
    return True


@function("argslen")
async def args_length(rt, arg, tag):
    tag.text += str(len(tag.variables[ARGS]))
    return True


def check_attachment_room(tag: TagResult):
    if tag.limits.attachments <= len(tag.files):
        raise TagError(f"Attachments surpassed max attachments length of {tag.limits.attachments}")


def storage_tag_id(rt: Runtime) -> int | None:
    if not rt.tag:
        return None
    return rt.tag.get("reference_id") or rt.tag["id"]


async def fetch_url(rt: Runtime, tag: TagResult, url: str) -> bytes:
    if not is_discord_url(url):
        raise TagError("Only Discord attachment and avatar URLs can be attached")
    if url not in rt.cache:
        count_network(tag)
        try:
            refreshed = await refresh_urls(rt.client.http, [url]) if attachment_expired(url) else {}
            rt.cache[url] = await download(
                rt.client.session, refreshed.get(url, url), rt.max_file_size
            )
        except MediaError as error:
            raise TagError(str(error)) from error
        except (discord.HTTPException, OSError, TimeoutError) as error:
            raise TagError("Attachment could not be downloaded") from error
    return rt.cache[url]


@function("attachment", "attach", "file")
async def attachment(rt, arg, tag, spoiler: bool = False):
    if tag.limits.attachments == 0:
        return False
    check_attachment_room(tag)
    parts = split(arg, 3)
    url = parts[0].strip().removeprefix("<").removesuffix(">")
    filename = parts[1].strip() if len(parts) > 1 else ""
    description = parts[2] if len(parts) > 2 else None
    if not url:
        return False
    if "://" in url:
        data = await fetch_url(rt, tag, url)
        name = filename_from_url(url)
    else:
        tag_id = storage_tag_id(rt)
        data = await rt.db.file(tag_id, url) if tag_id else None
        if data is None:
            raise TagError(f"File {url} does not exist on this tag")
        name = url
    if rt.max_file_size < len(data):
        raise TagError(f"Attachment surpassed max file size of {rt.max_file_size} bytes")
    tag.variables[FILE_SIZE] += len(data)
    tag.files.append(
        TagFile(data, safe_filename(filename) if filename else name, description, spoiler, url)
    )
    return True


@function("attachmentspoiler", "attachspoiler", "filespoiler")
async def attachment_spoiler(rt, arg, tag):
    return await attachment(rt, arg, tag, True)


@function("attachmenttext", "attachtext", "filetext")
async def attachment_text(rt, arg, tag):
    if tag.limits.attachments == 0:
        return False
    check_attachment_room(tag)
    filename = f"content.{len(tag.files) + 1}" if tag.files else "content"
    if rt.max_file_size < len(arg):
        raise TagError(f"Attachment surpassed max file size of {rt.max_file_size} bytes")
    tag.variables[FILE_SIZE] += len(arg)
    tag.files.append(TagFile(arg, f"{filename}.txt"))
    return True


@function("getfilename", lazy=True)
async def get_filename(rt, arg, tag):
    arg = arg.strip()
    if not arg:
        return False
    index = js_parse_int(arg)
    if index is None:
        return False
    if not 0 <= index - 1 < len(tag.files):
        raise TagError(f"File Id {index} does not exist")
    tag.text += tag.files[index - 1].filename
    return True


@function("setfilename", lazy=True)
async def set_filename(rt, arg, tag):
    if SPLITTER_ARGUMENT not in arg:
        return False
    parts = split(arg, 3)
    index = js_parse_int(await parse_value(rt, tag, parts[0]))
    if index is None or len(parts) < 2:
        return False
    if not 0 <= index - 1 < len(tag.files):
        raise TagError(f"File Id {index} does not exist")
    file = tag.files[index - 1]
    stem, dot, old_extension = file.filename.rpartition(".")
    if not dot:
        stem, old_extension = file.filename, ""
    value = await parse_value(rt, tag, parts[1]) or stem
    extension = old_extension if len(parts) < 3 else await parse_value(rt, tag, parts[2])
    file.filename = safe_filename(".".join(part for part in (value, extension) if part))
    return True


async def find_member(rt: Runtime, query: str):
    query = query.strip()
    if not query:
        return None
    match = MENTION.fullmatch(query)
    if match:
        user_id = int(match[1] or match[2])
        member = rt.guild.get_member(user_id) if rt.guild else None
        if member:
            return member
        try:
            return await rt.guild.fetch_member(user_id)
        except discord.HTTPException:
            pass
        try:
            return await rt.client.fetch_user(user_id)
        except discord.HTTPException:
            return None
    if not rt.guild:
        return None
    if not rt.guild.chunked:
        await rt.guild.chunk()
    lowered = query.removeprefix("@").lower()

    def names(member):
        return [
            value.lower()
            for value in (
                member.name,
                member.global_name,
                member.nick,
                f"{member.name}#{member.discriminator}",
            )
            if value
        ]

    members = rt.guild.members
    for check in (
        lambda value: value == lowered,
        lambda value: value.startswith(lowered),
        lambda value: lowered in value,
    ):
        for member in members:
            if any(check(value) for value in names(member)):
                return member
    return None


def find_channel(rt: Runtime, query: str):
    query = query.strip()
    if not query or not rt.guild:
        return None
    match = CHANNEL_MENTION.fullmatch(query)
    if match:
        return rt.guild.get_channel_or_thread(int(match[1] or match[2]))
    lowered = query.removeprefix("#").lower()
    for channel in [*rt.guild.channels, *rt.guild.threads]:
        if channel.name.lower() == lowered:
            return channel
    return None


def avatar_url(user, display: bool = True) -> str:
    asset = user.display_avatar if display else (user.avatar or user.default_avatar)
    return asset.with_size(1024).url


@function("avatar")
async def avatar(rt, arg, tag):
    if arg:
        count_network(tag)
        user = await find_member(rt, arg)
        if user:
            tag.text += avatar_url(user)
    else:
        tag.text += avatar_url(rt.user)
    return True


@function("useravatar")
async def user_avatar(rt, arg, tag):
    if arg:
        count_network(tag)
        user = await find_member(rt, arg)
        if user:
            tag.text += avatar_url(user, False)
    else:
        tag.text += avatar_url(rt.user, False)
    return True


def user_property(getter, *names):
    async def handler(rt, arg, tag):
        if arg:
            count_network(tag)
            user = await find_member(rt, arg)
            if user:
                tag.text += getter(user)
        else:
            tag.text += getter(rt.user)
        return True

    function(*names)(handler)


user_property(lambda user: user.discriminator, "userdiscriminator", "userdiscrim", "discrim")
user_property(lambda user: str(user.id), "userid", "id")
user_property(lambda user: user.mention, "usermention", "mention")
user_property(lambda user: user.name, "username", "name", "user")
user_property(lambda user: user.display_name, "usernick", "nick")
user_property(str, "usertag")


@function("userstatus")
async def user_status(rt, arg, tag):
    member = rt.user
    if arg:
        count_network(tag)
        member = await find_member(rt, arg)
    if isinstance(member, discord.Member):
        tag.text += str(member.status)
    return True


async def guild_members(rt: Runtime) -> list:
    if not rt.guild.chunked:
        await rt.guild.chunk()
    return list(rt.guild.members)


def random_member_function(getter, online: bool, *names):
    async def handler(rt, arg, tag):
        if rt.guild:
            members = await guild_members(rt)
            if online:
                members = [member for member in members if member.status != discord.Status.offline]
            if members:
                tag.text += getter(random.choice(members))
        else:
            tag.text += getter(random.choice([rt.user, rt.client.user]))
        return True

    function(*names)(handler)


random_member_function(lambda member: member.display_name, False, "randomuser", "randuser")
random_member_function(lambda member: str(member.id), False, "randomuserid", "randuserid")
random_member_function(str, False, "randomusertag", "randusertag")
random_member_function(lambda member: member.display_name, True, "randomonline", "randonline")
random_member_function(lambda member: str(member.id), True, "randomonlineid", "randonlineid")
random_member_function(str, True, "randomonlinetag", "randonlinetag")


@function("channel")
async def channel_name(rt, arg, tag):
    if arg:
        channel = find_channel(rt, arg)
        if channel:
            tag.text += channel.name
    else:
        tag.text += rt.channel.name if rt.guild else "Direct Message"
    return True


@function("channelid")
async def channel_id(rt, arg, tag):
    if arg:
        channel = find_channel(rt, arg)
        if channel:
            tag.text += str(channel.id)
    else:
        tag.text += str(rt.channel.id)
    return True


@function("channelmention")
async def channel_mention(rt, arg, tag):
    if arg:
        channel = find_channel(rt, arg)
        if channel:
            tag.text += channel.mention
    else:
        tag.text += f"<#{rt.channel.id}>"
    return True


def random_channel(rt: Runtime):
    channels = list(rt.guild.channels) if rt.guild else []
    return random.choice(channels) if channels else None


@function("randchannel")
async def random_channel_name(rt, arg, tag):
    channel = random_channel(rt)
    tag.text += channel.name if channel else ("" if rt.guild else "Direct Message")
    return True


@function("randchannelid")
async def random_channel_id(rt, arg, tag):
    channel = random_channel(rt) or rt.channel
    tag.text += str(channel.id)
    return True


@function("randchannelmention")
async def random_channel_mention(rt, arg, tag):
    channel = random_channel(rt) or rt.channel
    tag.text += f"<#{channel.id}>"
    return True


@function("guild", "server")
async def guild_name(rt, arg, tag):
    tag.text += rt.guild.name if rt.guild else "Direct Message"
    return True


@function("guildcount", "membercount", "servercount")
async def guild_count(rt, arg, tag):
    tag.text += str(rt.guild.member_count) if rt.guild else "2"
    return True


@function("guildid", "serverid", "sid", "gid")
async def guild_id(rt, arg, tag):
    tag.text += str(rt.guild.id) if rt.guild else "0"
    return True


@function("json.channel", "channeljson")
async def json_channel(rt, arg, tag):
    channel = find_channel(rt, arg) if arg else rt.channel
    if channel:
        tag.text += to_json(channel_json(channel))
    return True


@function("json.guild")
async def json_guild(rt, arg, tag):
    if rt.guild:
        tag.text += to_json(guild_json(rt.guild))
    return True


@function("json.member")
async def json_member(rt, arg, tag):
    member = rt.user
    if arg:
        count_network(tag)
        member = await find_member(rt, arg)
    if isinstance(member, discord.Member):
        tag.text += to_json(member_json(member))
    return True


@function("json.memberoruser")
async def json_member_or_user(rt, arg, tag):
    user = rt.user
    if arg:
        count_network(tag)
        user = await find_member(rt, arg)
    if user:
        tag.text += to_json(member_or_user_json(user))
    return True


@function("json.user", "userjson")
async def json_user(rt, arg, tag):
    user = rt.user
    if arg:
        count_network(tag)
        user = await find_member(rt, arg)
    if user:
        tag.text += to_json(user_json(user))
    return True


def check_history(rt: Runtime, channel):
    if isinstance(rt.user, discord.Member):
        permissions = channel.permissions_for(rt.user)
        if not (permissions.view_channel and permissions.read_message_history):
            raise TagError("You cannot view the history of this channel")
    permissions = channel.permissions_for(rt.guild.me)
    if not (permissions.view_channel and permissions.read_message_history):
        raise TagError("Bot cannot view the history of this channel")


async def message_channel(rt: Runtime, tag: TagResult, query: str | None):
    if query:
        count_network(tag)
        channel = find_channel(rt, query)
        if not channel or not hasattr(channel, "fetch_message"):
            return None
    else:
        channel = rt.channel
    check_history(rt, channel)
    return channel


async def fetch_message(rt: Runtime, tag: TagResult, arg: str):
    message_id, *rest = split(arg, 2)
    message_id = message_id.strip()
    if not message_id.isdigit():
        return None
    channel = await message_channel(rt, tag, rest[0] if rest else None)
    if channel is None:
        return None
    count_network(tag)
    try:
        return await channel.fetch_message(int(message_id))
    except discord.HTTPException:
        return None


@function("json.message")
async def json_message(rt, arg, tag):
    if arg:
        message = await fetch_message(rt, tag, arg)
        if message:
            tag.text += to_json(message_json(message))
    return True


@function("messagecontent")
async def message_content(rt, arg, tag):
    if arg:
        message = await fetch_message(rt, tag, arg)
        if message:
            tag.text += message.content
    return True


@function("messageuserid")
async def message_user_id(rt, arg, tag):
    if arg:
        message = await fetch_message(rt, tag, arg)
        if message:
            tag.text += str(message.author.id)
    return True


async def recent_messages(rt: Runtime, tag: TagResult, channel, limit: int) -> list:
    count_network(tag)
    try:
        return [message async for message in channel.history(limit=limit)]
    except discord.HTTPException as error:
        raise TagError("Bot cannot view the history of this channel") from error


@function("messagelastid")
async def message_last_id(rt, arg, tag):
    index_query, *rest = split(arg, 2)
    index = 0
    if index_query == "random":
        index = -1
    elif js_number(index_query) is not None:
        index = js_parse_int(index_query) or 0
    if 100 <= index:
        raise TagError("Message index cannot be over 99")
    channel = await message_channel(rt, tag, rest[0] if rest else None)
    if channel is None:
        return True
    messages = await recent_messages(rt, tag, channel, 100 if index == -1 else min(100, index + 1))
    if index == -1 and messages:
        tag.text += str(random.choice(messages).id)
    elif 0 <= index < len(messages):
        tag.text += str(messages[index].id)
    return True


@function("randmessageid")
async def random_message_id(rt, arg, tag):
    channel = await message_channel(rt, tag, arg.strip() or None)
    if channel is None:
        return True
    messages = await recent_messages(rt, tag, channel, 100)
    if messages:
        tag.text += str(random.choice(messages).id)
    return True


@function("json.messagereply", "replycontent", "replyuserid")
async def reply_function(rt, arg, tag):
    return True


@function("prefix")
async def prefix(rt, arg, tag):
    tag.text += "/"
    return True


@function("nsfw")
async def nsfw(rt, arg, tag):
    is_nsfw = getattr(rt.channel, "is_nsfw", None)
    if not (is_nsfw and is_nsfw()):
        raise TagError("Cannot use a NSFW tag here!")
    return True


def media_kind(filename: str, content_type: str | None) -> str | None:
    if content_type:
        kind = content_type.split("/")[0]
        if kind in ("image", "video", "audio"):
            return kind
    extension = filename.rpartition(".")[2].lower().split("?")[0]
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    return None


async def media_from_value(rt: Runtime, value: str, kinds: set[str]) -> str | None:
    value = value.strip().removeprefix("<").removesuffix(">")
    if value.startswith(("http://", "https://")):
        return value
    emoji = CUSTOM_EMOJI.fullmatch(f"<{value}>") or CUSTOM_EMOJI.fullmatch(value)
    if emoji:
        if "image" not in kinds:
            return None
        return f"https://cdn.discordapp.com/emojis/{emoji[2]}.{'gif' if emoji[1] else 'png'}"
    if "image" in kinds:
        user = await find_member(rt, value)
        if user:
            return avatar_url(user)
    return None


async def last_media(rt: Runtime, kinds: set[str]) -> str | None:
    permissions = rt.channel.permissions_for(rt.guild.me) if rt.guild else None
    if permissions and not permissions.read_message_history:
        return None
    try:
        async for message in rt.channel.history(limit=50):
            for attachment in message.attachments:
                if media_kind(attachment.filename, attachment.content_type) in kinds:
                    return attachment.url
            for embed in message.embeds:
                for kind, item in (
                    ("video", embed.video),
                    ("image", embed.image),
                    ("image", embed.thumbnail),
                ):
                    if kind in kinds and item and item.url:
                        return item.url
    except discord.HTTPException:
        return None
    return None


def media_function(kinds: set[str], *names: str):
    async def handler(rt, arg, tag):
        count_network(tag)
        if rt.attachment_url:
            url = rt.attachment_url
        elif arg.strip():
            url = await media_from_value(rt, arg, kinds)
        else:
            url = await last_media(rt, kinds)
        if url:
            tag.text += url
        return True

    function(*names)(handler)


media_function({"image", "video", "audio"}, "media")
media_function({"audio"}, "audio")
media_function({"audio", "video"}, "av")
media_function({"image"}, "image", "last_attachment", "lastattachment", "lattachment", "lattach")
media_function({"image", "video"}, "iv")
media_function({"video"}, "video")


def codeblock_text(value: str) -> str:
    match = CODEBLOCK.search(value)
    return match[2] if match else value


@function("componentjson", lazy=True)
async def component_json(rt, arg, tag):
    arg = arg.strip()
    if arg.startswith("```") and arg.endswith("```"):
        arg = codeblock_text(arg)
    tag.variables[IS_FROM_CHILD_PARSING] = 1
    try:
        text = await parse_value(rt, tag, arg)
    finally:
        tag.variables[IS_FROM_CHILD_PARSING] = 0
    try:
        data = json.loads(text) if text else None
    except ValueError as error:
        raise TagError("Invalid Component Given") from error
    if not isinstance(data, dict | list):
        raise TagError("Invalid Component Given")
    components = tag.components or {"components": [], "on_timeout": None}
    try:
        components["components"].append(component_tools.parse_component(data))
    except component_tools.ComponentError as error:
        raise TagError(str(error)) from error
    check_components(components)
    tag.components = components
    return True


@function("componentsontimeout", lazy=True)
async def components_on_timeout(rt, arg, tag):
    arg = arg.strip()
    if not arg:
        return False
    tag.components = tag.components or {"components": [], "on_timeout": None}
    tag.components["on_timeout"] = arg
    return True


def embed_from_data(data) -> discord.Embed | None:
    if not isinstance(data, dict):
        return None
    try:
        embed = discord.Embed.from_dict(data)
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
    if not len(embed) and not any(
        getattr(embed, name).url for name in ("image", "thumbnail", "video")
    ):
        return None
    return embed


@function("embedjson")
async def embed_json(rt, arg, tag):
    try:
        embed = embed_from_data(json.loads(arg))
    except ValueError:
        return False
    if embed is None:
        return False
    tag.embeds.append(embed)
    if tag.limits.embeds < len(tag.embeds):
        raise TagError(f"Embeds surpassed max embeds length of {tag.limits.embeds}")
    return True


@function("pagejson")
async def page_json(rt, arg, tag):
    try:
        data = json.loads(arg)
    except ValueError as error:
        raise TagError("Invalid Page Given") from error
    for page in data if isinstance(data, list) else [data]:
        if not isinstance(page, dict):
            raise TagError("Invalid Page Given")
        content = page["content"] if isinstance(page.get("content"), str) else None
        embeds = None
        if isinstance(page.get("embed"), dict):
            embed = embed_from_data(page["embed"])
            if embed is None:
                raise TagError("Invalid Page Given")
            embeds = [embed]
        filenames = None
        if isinstance(page.get("files"), list):
            filenames = [
                name
                for name in page["files"]
                if isinstance(name, str) and name.startswith("attachment://")
            ] or None
        if not content and not embeds and not filenames:
            raise TagError("Invalid Page Given")
        tag.pages.append({"content": content, "embeds": embeds, "filenames": filenames})
        if tag.limits.pages < len(tag.pages):
            raise TagError(f"Pages surpassed max pages length of {tag.limits.pages}")
    return True


@function("eval")
async def eval_function(rt, arg, tag):
    tag.text += await parse_value(rt, tag, arg)
    return True


@function("evalsilent", lazy=True)
async def eval_silent(rt, arg, tag):
    try:
        tag.text += await parse_value(rt, tag, arg)
    except TagExit:
        raise
    except Exception:
        pass
    return True


@function("exit")
async def exit_function(rt, arg, tag):
    raise TagExit(arg)


@function("exitsilent")
async def exit_silent(rt, arg, tag):
    raise TagExit(arg, True)


@function("and", lazy=True)
async def logical_and(rt, arg, tag):
    if SPLITTER_ARGUMENT not in arg:
        return False
    conditionals = split(arg)
    for index, conditional in enumerate(conditionals):
        text = await parse_value(rt, tag, conditional)
        if not text:
            break
        if index == len(conditionals) - 1:
            tag.text += text
    return True


@function("or", lazy=True)
async def logical_or(rt, arg, tag):
    for conditional in split(arg):
        text = await parse_value(rt, tag, conditional)
        if text:
            tag.text += text
            break
    return True


@function("iferror", lazy=True)
async def logical_if_error(rt, arg, tag):
    for conditional in split(arg):
        try:
            tag.text += await parse_value(rt, tag, conditional)
        except TagExit:
            raise
        except Exception:
            continue
        break
    return True


@function("includes", lazy=True)
async def logical_includes(rt, arg, tag):
    if SPLITTER_ARGUMENT not in arg:
        return False
    text, *values = split(arg)
    text = await parse_value(rt, tag, text)
    for value in values:
        if text == await parse_value(rt, tag, value):
            tag.text += "true"
            return True
    tag.text += "false"
    return True


def strict_decimal(value: str) -> Decimal:
    text = value.strip()
    if not text:
        return Decimal(0)
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", text):
        return Decimal(int(text, 16))
    if not re.fullmatch(r"[+-]?(?:\d+\.?\d*|\.\d+)", text):
        raise InvalidOperation
    return Decimal(text)


def compare(first: str, operator: str, second: str) -> bool:
    if operator == "=":
        return first == second
    if operator == "!=":
        return first != second
    try:
        left, right = strict_decimal(first), strict_decimal(second)
    except InvalidOperation:
        return first.lower() == second.lower() if operator == "~" else False
    return {
        ">": left > right,
        ">=": left >= right,
        "<": left < right,
        "<=": left <= right,
        "~": left == right,
    }[operator]


@function("if", lazy=True)
async def logical_if(rt, arg, tag):
    if SPLITTER_ARGUMENT not in arg:
        return False
    parts = split(arg, 6)
    if len(parts) < 4:
        return False
    first, operator, second, *conditionals = parts
    branches = {}
    for conditional in conditionals:
        if not conditional:
            continue
        for prefix in ("then:", "else:", "finally:"):
            if conditional.startswith(prefix):
                if prefix in branches:
                    raise TagError(f"IF statement must have only one `{prefix}`")
                branches[prefix] = conditional[len(prefix) :]
                break
        else:
            return False
    if operator not in ("=", "!=", ">", ">=", "<", "<=", "~"):
        return False
    first = await parse_value(rt, tag, first)
    second = await parse_value(rt, tag, second)
    branch = branches.get("then:" if compare(first, operator, second) else "else:", "")
    tag.text += await parse_value(rt, tag, branch)
    if "finally:" in branches:
        tag.text += await parse_value(rt, tag, branches["finally:"])
    return True


@function("foreach", lazy=True)
async def logical_for_each(rt, arg, tag):
    if SPLITTER_ARGUMENT not in arg:
        return False
    parts = split(arg, 2)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return True
    text, code = await parse_value(rt, tag, parts[0]), parts[1]
    count = js_parse_int(text)
    if count is None:
        try:
            data = json.loads(text)
        except ValueError:
            return True
        if isinstance(data, str):
            data = string_arguments(data)
        if isinstance(data, list):
            values = [(str(index), value) for index, value in enumerate(data)]
        elif isinstance(data, dict):
            values = [(str(key), value) for key, value in data.items()]
        else:
            return True
    else:
        if count > 200:
            raise TagError("Cannot have a for loop that is less than 1 or bigger than 200.")
        values = [(str(index), index) for index in range(max(count, 0))]
    if len(values) > 200:
        raise TagError("Cannot have a for loop that is less than 1 or bigger than 200.")
    saved = {key: tag.variables[key] for key in (ID_X, VALUE_X) if key in tag.variables}
    for key, value in values:
        if BRACKET_LEFT in code:
            tag.variables[ID_X] = key
            tag.variables[VALUE_X] = js_str(value)
            tag.text += await parse_value(rt, tag, code)
        else:
            tag.text += code
    for key in (ID_X, VALUE_X):
        if key in saved:
            tag.variables[key] = saved[key]
        else:
            tag.variables.pop(key, None)
    return True


def check_variable_key(key: str, tag: TagResult, action: str):
    if key.startswith(PRIVATE_PREFIX):
        raise TagError(
            f"Tried to {action} a private variable, cannot start with '{PRIVATE_PREFIX}'."
        )
    if tag.limits.variable_key_length < len(key):
        raise TagError(f"Variable cannot be more than {tag.limits.variable_key_length} characters")


@function("get", lazy=True)
async def logical_get(rt, arg, tag):
    key, *default = split(arg, 2)
    key = (await parse_value(rt, tag, key)).strip()
    if tag.limits.variable_key_length < len(key):
        raise TagError(f"Variable cannot be more than {tag.limits.variable_key_length} characters")
    if key == FILES:
        files = [{"description": file.description, "filename": file.filename} for file in tag.files]
        tag.text += to_json(files)
    elif key in tag.variables:
        tag.text += js_str(tag.variables[key])
    elif default and default[0]:
        tag.text += await parse_value(rt, tag, default[0])
    return True


@function("set", lazy=True)
async def logical_set(rt, arg, tag):
    if SPLITTER_ARGUMENT not in arg:
        return False
    key, value = split(arg, 2)
    key = await parse_value(rt, tag, key)
    check_variable_key(key, tag, "set")
    user_keys = [name for name in tag.variables if not name.startswith(PRIVATE_PREFIX)]
    if key not in tag.variables and tag.limits.variables <= len(user_keys):
        raise TagError(f"Reached max variable amount (Max {tag.limits.variables:,} Variables)")
    value = await parse_value(rt, tag, value)
    tag.variables[key] = value[: tag.limits.variable_length].strip()
    return True


@function("delete")
async def logical_delete(rt, arg, tag):
    key = arg.strip()
    check_variable_key(key, tag, "delete")
    tag.variables.pop(key, None)
    return True


def storage_id(rt: Runtime, scope: str) -> int:
    return {"channel": rt.channel.id, "server": rt.guild.id, "user": rt.user.id}[scope]


def check_storage_key(key: str, tag: TagResult):
    if tag.limits.storage_key_length < len(key):
        raise TagError(
            f"Storage Variable cannot be more than {tag.limits.storage_key_length} characters"
        )


def storage_get(scope: str):
    async def handler(rt, arg, tag):
        key, *default = split(arg, 2)
        key = (await parse_value(rt, tag, key)).strip()
        check_storage_key(key, tag)
        tag_id = storage_tag_id(rt)
        if tag_id:
            value = await rt.db.get_variable(tag_id, scope, storage_id(rt, scope), key)
            if value is not None:
                tag.text += value
            elif default and default[0]:
                tag.text += await parse_value(rt, tag, default[0])
        return True

    return handler


def storage_set(scope: str):
    async def handler(rt, arg, tag):
        if SPLITTER_ARGUMENT not in arg:
            return False
        key, value = split(arg, 2)
        key = await parse_value(rt, tag, key)
        check_storage_key(key, tag)
        value = await parse_value(rt, tag, value)
        if tag.limits.storage_value_length < len(value):
            raise TagError(
                "Storage Variable Value cannot be more than "
                f"{tag.limits.storage_value_length} characters"
            )
        tag_id = storage_tag_id(rt)
        if tag_id:
            try:
                await rt.db.set_variable(tag_id, scope, storage_id(rt, scope), key, value)
            except StorageLimitError as error:
                raise TagError(
                    f"Reached max {scope} storage variable amount (Max 5 Variables)"
                ) from error
        return True

    return handler


def storage_delete(scope: str):
    async def handler(rt, arg, tag):
        key = arg.strip()
        check_storage_key(key, tag)
        tag_id = storage_tag_id(rt)
        if tag_id:
            await rt.db.delete_variable(tag_id, scope, storage_id(rt, scope), key)
        return True

    return handler


def storage_list(scope: str):
    async def handler(rt, arg, tag):
        tag_id = storage_tag_id(rt)
        values = await rt.db.variables(tag_id, scope, storage_id(rt, scope)) if tag_id else {}
        tag.text += to_json(values)
        return True

    return handler


for _scope in ("channel", "server", "user"):
    function(f"get{_scope}", lazy=True)(storage_get(_scope))
    function(f"set{_scope}", lazy=True)(storage_set(_scope))
    function(f"delete{_scope}")(storage_delete(_scope))
    function(f"variables{_scope}")(storage_list(_scope))


@function("variables")
async def variables(rt, arg, tag):
    tag.text += to_json(tag.variables)
    return True


@function("isfromai")
async def is_from_ai(rt, arg, tag):
    tag.text += "false"
    return True


@function("isfromcomponent")
async def is_from_component(rt, arg, tag):
    tag.text += "true" if tag.variables[COMPONENT_EXECUTIONS] > 0 else "false"
    return True


@function("ismaintag")
async def is_main_tag(rt, arg, tag):
    tag.text += "false" if tag.variables[PARENT_TAG_ID] else "true"
    return True


@function("math")
async def math_function(rt, arg, tag):
    try:
        tag.text += evaluate(arg.strip())
    except (MathError, ArithmeticError, ValueError) as error:
        raise TagError(f"Math equation errored out ({error})") from error
    return True


def unary_math(operation, *names):
    async def handler(rt, arg, tag):
        value = js_parse_int(arg)
        if value is None:
            return False
        tag.text += js_str(operation(value))
        return True

    function(*names)(handler)


unary_math(abs, "abs")
unary_math(math.cos, "cos")
unary_math(math.sin, "sin")
unary_math(math.tan, "tan")


@function("e")
async def math_e(rt, arg, tag):
    tag.text += js_str(math.e)
    return True


@function("pi")
async def math_pi(rt, arg, tag):
    tag.text += js_str(math.pi)
    return True


def big_integers(values: list[str]) -> list[int] | None:
    numbers = [value.split(".")[0] for value in values]
    if any(js_number(number) is None for number in numbers):
        return None
    try:
        return [int(number.strip() or "0", 0) for number in numbers]
    except ValueError:
        try:
            return [int(number.strip() or "0") for number in numbers]
        except ValueError:
            return None


@function("max")
async def math_max(rt, arg, tag):
    numbers = big_integers(split(arg)) if arg else None
    if not numbers:
        return False
    tag.text += str(max(numbers))
    return True


@function("min")
async def math_min(rt, arg, tag):
    numbers = big_integers(split(arg)) if arg else None
    if not numbers:
        return False
    tag.text += str(min(numbers))
    return True


@function("choose", lazy=True)
async def choose(rt, arg, tag):
    if not arg:
        return False
    value = random.choice(split(arg)) if SPLITTER_ARGUMENT in arg else arg
    tag.text += await parse_value(rt, tag, value)
    return True


@function("range", "random", "rnd")
async def rng_range(rt, arg, tag):
    if not arg:
        return False
    first, splitter, second = arg.partition(SPLITTER_ARGUMENT)
    values = big_integers([first, second] if splitter else ["0", first])
    if values is None:
        return False
    tag.text += str(random.randint(min(values), max(values)))
    return True


def uuid7() -> uuid.UUID:
    value = (int(time.time() * 1000) & ((1 << 48) - 1)) << 80
    value |= secrets.randbits(80)
    value = (value & ~(0xF << 76)) | (0x7 << 76)
    value = (value & ~(0x3 << 62)) | (0x2 << 62)
    return uuid.UUID(int=value)


@function("uuid")
async def rng_uuid(rt, arg, tag):
    if arg in ("", "4"):
        tag.text += str(uuid.uuid4())
    elif arg == "7":
        tag.text += str(uuid7())
    else:
        return False
    return True


@function("settings")
async def settings(rt, arg, tag):
    parts = split(arg)
    if len(parts) != 2:
        return False
    setting, value = parts[0].strip().upper(), parts[1].strip()
    if setting in IGNORED_SETTINGS:
        return True
    if setting not in (SETTING_ALLOW_EVERYONE, SETTING_NATIVE_MEDIA):
        return False
    if value.lower() == "true":
        tag.variables[SETTINGS][setting] = True
    else:
        tag.variables[SETTINGS].pop(setting, None)
    return True


@function("indexof", lazy=True)
async def index_of(rt, arg, tag):
    if SPLITTER_ARGUMENT not in arg:
        return False
    search, text = split(arg, 2)
    search = await parse_value(rt, tag, search)
    text = await parse_value(rt, tag, text)
    tag.text += str(text.find(search))
    return True


@function("jsonify")
async def jsonify(rt, arg, tag):
    tag.text += json.dumps(arg, ensure_ascii=False)
    return True


@function("oneof")
async def one_of(rt, arg, tag):
    if not arg:
        return False
    value, *choices = split(arg)
    if not value or not choices:
        return True
    lowered = value.lower()
    for choice in choices:
        if lowered in choice.lower():
            tag.text += choice
            return True
    by_lower = {choice.lower(): choice for choice in reversed(choices)}
    matches = difflib.get_close_matches(lowered, list(by_lower), n=1, cutoff=0.5)
    if matches:
        tag.text += by_lower[matches[0]]
    return True


@function("len", "length")
async def length(rt, arg, tag):
    tag.text += str(len(arg.encode("utf-16-le")) // 2)
    return True


def simple_text(operation, *names):
    async def handler(rt, arg, tag):
        tag.text += operation(arg)
        return True

    function(*names)(handler)


def codestring(text: str) -> str:
    return "`" + text.replace("`", "\u02cb") + "`"


def codeblock(text: str) -> str:
    return "```\n" + text.replace("```", "`\u200b`\u200b`") + "\n```"


def escape_all(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))


def quote_text(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.split("\n"))


simple_text(str.lower, "lower")
simple_text(str.upper, "upper")
simple_text(lambda text: text[::-1], "reverse")
simple_text(lambda text: quote(text, safe="-_.!~*'()"), "url", "urlencode")
simple_text(lambda text: f"**{text}**", "markupbold")
simple_text(codeblock, "code", "markupcodeblock")
simple_text(codestring, "markupcodestring")
simple_text(escape_all, "markupescape")
simple_text(lambda text: f"# {text}", "markupheaderbig")
simple_text(lambda text: f"## {text}", "markupheadermedium")
simple_text(lambda text: f"### {text}", "markupheadersmall")
simple_text(lambda text: f"*{text}*", "markupitalics")
simple_text(quote_text, "markupquote")
simple_text(lambda text: f"||{text}||", "markupspoiler")
simple_text(lambda text: f"~~{text}~~", "markupstrike")
simple_text(lambda text: f"-# {text}", "markupsubtext")
simple_text(lambda text: f"__{text}__", "markupunderline")
simple_text(lambda text: "\n".join(f"- {item}" for item in split(text)), "markuplistdotted")
simple_text(
    lambda text: "\n".join(f"{index}. {item}" for index, item in enumerate(split(text), 1)),
    "markuplistnumbered",
)


@function("markupurl")
async def markup_url(rt, arg, tag):
    text, *rest = split(arg, 3)
    url = rest[0] if rest else ""
    comment = rest[1] if len(rest) > 1 else ""
    tag.text += f'[{text}]({url} "{comment}")' if comment else f"[{text}]({url})"
    return True


@function("markuptime")
async def markup_time(rt, arg, tag):
    style = None
    timestamp = arg
    if not arg:
        timestamp = str(int(time.time() * 1000))
    elif arg in TIMESTAMP_STYLES or arg in TIMESTAMP_STYLES.values():
        style, timestamp = arg, str(int(time.time() * 1000))
    elif SPLITTER_ARGUMENT in arg:
        parts = split(arg)
        if parts[-1] in TIMESTAMP_STYLES or parts[-1] in TIMESTAMP_STYLES.values():
            style = parts.pop()
        timestamp = SPLITTER_ARGUMENT.join(parts).strip()
    number = js_number(timestamp)
    if number is None or math.isinf(number):
        return False
    style = TIMESTAMP_STYLES.get(style, style)
    tag.text += f"<t:{round(number / 1000)}{f':{style}' if style else ''}>"
    return True


@function("repeat")
async def string_repeat(rt, arg, tag):
    if SPLITTER_ARGUMENT not in arg:
        return False
    amount_text, *value = split(arg)
    amount = js_parse_int(amount_text.strip())
    if amount is None:
        return False
    if amount < 0:
        raise TagError("Invalid count value")
    text = SPLITTER_ARGUMENT.join(value).strip()
    if rt.max_file_size < len(text) * amount + len(tag.text):
        raise TagError(f"Text exceeded {rt.max_file_size} bytes")
    tag.text += text * amount
    return True


def js_replacement(template: str):
    def replace(match):
        output, index = [], 0
        while index < len(template):
            character = template[index]
            following = template[index + 1 : index + 2]
            if character == "$" and following:
                if following == "$":
                    output.append("$")
                    index += 2
                    continue
                if following == "&":
                    output.append(match[0])
                    index += 2
                    continue
                if following == "`":
                    output.append(match.string[: match.start()])
                    index += 2
                    continue
                if following == "'":
                    output.append(match.string[match.end() :])
                    index += 2
                    continue
                if following.isdigit():
                    groups = len(match.groups())
                    two = template[index + 1 : index + 3]
                    if len(two) == 2 and two.isdigit() and 0 < int(two) <= groups:
                        output.append(match[int(two)] or "")
                        index += 3
                        continue
                    if 0 < int(following) <= groups:
                        output.append(match[int(following)] or "")
                        index += 2
                        continue
                if following == "<" and match.re.groupindex:
                    end = template.find(">", index)
                    if end != -1:
                        name = template[index + 2 : end]
                        output.append(match.groupdict().get(name) or "")
                        index = end + 1
                        continue
            output.append(character)
            index += 1
        return "".join(output)

    return replace


@function("replace", "replaceregex")
async def string_replace(rt, arg, tag):
    if SPLITTER_ARGUMENT not in arg:
        return False
    parts = split(arg)
    if len(parts) < 3:
        return False
    pattern, first, *rest = parts
    second = SPLITTER_ARGUMENT.join(rest)
    if first.startswith("with:"):
        replacement, source = first[5:], second
    elif second.startswith("with:"):
        replacement, source = second[5:], first
    else:
        replacement, source = first, second
    source = source.removeprefix("in:")
    try:
        compiled = regex.compile(pattern.strip(), regex.IGNORECASE)
        replace = js_replacement(replacement)
        budget = rt.max_file_size - len(tag.text)
        pieces, position, size = [], 0, 0
        for match in compiled.finditer(source, timeout=tag.limits.regex_seconds):
            for piece in (source[position : match.start()], replace(match)):
                size += len(piece)
                if size > budget:
                    raise TagError(f"Text exceeded {rt.max_file_size} bytes")
                pieces.append(piece)
            position = match.end()
        pieces.append(source[position:])
        if size + len(pieces[-1]) > budget:
            raise TagError(f"Text exceeded {rt.max_file_size} bytes")
        tag.text += "".join(pieces)
    except (regex.error, TimeoutError, IndexError) as error:
        raise TagError("text replacing errored or timed out") from error
    return True


@function("substring", lazy=True)
async def substring(rt, arg, tag):
    if SPLITTER_ARGUMENT not in arg:
        return False
    parts = split(arg, 3)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return False
    start = js_parse_int(await parse_value(rt, tag, parts[1]))
    if start is None:
        return False
    end = None
    if len(parts) > 2:
        end = js_parse_int(await parse_value(rt, tag, parts[2]))
        if end is None:
            return False
    text = await parse_value(rt, tag, parts[0])
    tag.text += js_substring(text, start, end)
    return True


@function("tag")
async def tag_function(rt, arg, tag):
    tag_id, *rest = split(arg, 2)
    tag_id = tag_id.strip()
    arguments = rest[0] if rest else ""
    if not SNOWFLAKE.fullmatch(tag_id):
        raise TagError("Invalid Tag Id")
    if tag.variables[IS_FROM_CHILD_PARSING]:
        return False
    count_tag_execution(tag)
    count_network(tag)
    fetched = await rt.db.get_tag_by_id(int(tag_id))
    if not fetched or not rt.guild or fetched["guild_id"] != rt.guild.id:
        raise TagError("Cannot call this tag")
    await rt.db.use(fetched["id"])
    if is_group(fetched):
        fetched = await rt.db.random_member(target_id(fetched))
        if fetched is None:
            raise TagError("This group has no tags")
        await rt.db.use(fetched["id"])
    old_tag = rt.tag
    old_variables = dict(tag.variables)
    same = bool(old_tag) and old_tag["id"] == fetched["id"]
    child_variables = (
        dict(old_variables)
        if same
        else {key: tag.variables[key] for key in PRIVATE_VARIABLES if key in tag.variables}
    )
    child_variables[ARGS_STRING] = arguments
    child_variables[ARGS] = string_arguments(arguments)
    if old_tag:
        child_variables[PARENT_TAG_ID] = str(old_tag["id"])
    rt.tag = fetched
    try:
        content = fetched["reference_content"] or fetched["content"]
        result = await parse(rt, content, arguments, child_variables, tag.limits, tag.files)
        normalize(tag, result)
        if same:
            tag.variables.update(result.variables)
        else:
            for key in PRIVATE_VARIABLES:
                if key in result.variables:
                    tag.variables[key] = result.variables[key]
        for key in (ARGS_STRING, ARGS, PARENT_TAG_ID):
            tag.variables[key] = old_variables[key]
    finally:
        rt.tag = old_tag
    return True


@function("tagcontent")
async def tag_content(rt, arg, tag):
    if rt.tag:
        tag.text += rt.tag.get("reference_content") or rt.tag["content"]
    return True


@function("tagid")
async def tag_id_function(rt, arg, tag):
    if rt.tag:
        tag.text += str(rt.tag["id"])
    return True


@function("tagname")
async def tag_name(rt, arg, tag):
    if rt.tag:
        tag.text += rt.tag["name"]
    return True


@function("tagownerid")
async def tag_owner_id(rt, arg, tag):
    if rt.tag:
        tag.text += str(rt.tag["owner_id"])
    return True


TIME_UNITS = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "h": 3600,
    "hr": 3600,
    "hour": 3600,
    "d": 86400,
    "day": 86400,
    "w": 604800,
    "week": 604800,
    "month": 2592000,
    "y": 31536000,
    "year": 31536000,
}
RELATIVE_TIME = re.compile(r"(?:in\s+)?(\d+(?:\.\d+)?|an?)\s*([a-z]+?)s?(\s+ago)?", re.IGNORECASE)
NAMED_DAYS = {"now": 0, "today": 0, "tomorrow": 1, "yesterday": -1}


def parse_time(value: str, now: float | None = None) -> int | None:
    now = time.time() if now is None else now
    text = value.strip()
    number = js_number(text)
    if number is not None and not math.isinf(number):
        return int(number)
    lowered = text.lower()
    if lowered in NAMED_DAYS:
        return int((now + NAMED_DAYS[lowered] * 86400) * 1000)
    match = RELATIVE_TIME.fullmatch(lowered)
    if match and match[2] in TIME_UNITS and (lowered.startswith("in ") or match[3]):
        amount = 1.0 if match[1] in ("a", "an") else float(match[1])
        seconds = amount * TIME_UNITS[match[2]]
        return int((now + (-seconds if match[3] else seconds)) * 1000)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * 1000)


@function("unix")
async def unix(rt, arg, tag):
    milliseconds = parse_time(arg) if arg else int(time.time() * 1000)
    if milliseconds is not None:
        tag.text += str(milliseconds)
    return True


@function("unixs")
async def unix_seconds(rt, arg, tag):
    milliseconds = parse_time(arg) if arg else int(time.time() * 1000)
    if milliseconds is not None:
        tag.text += str(milliseconds // 1000)
    return True


@function("unixsnowflake")
async def unix_snowflake(rt, arg, tag):
    if arg.strip().isdigit():
        tag.text += str((int(arg.strip()) >> 22) + DISCORD_EPOCH)
    return True


def json_path(path: str) -> list[str]:
    segments, current, index = [], "", 0
    while index < len(path):
        character = path[index]
        if character == ".":
            if current:
                segments.append(current)
                current = ""
            index += 1
        elif character == "[":
            if current:
                segments.append(current)
                current = ""
            index += 1
            content = ""
            if index < len(path) and path[index] in "\"'":
                quote_character = path[index]
                index += 1
                while index < len(path) and path[index] != quote_character:
                    content += path[index]
                    index += 1
                index += 1
            while index < len(path) and path[index] != "]":
                content += path[index]
                index += 1
            index += 1
            segments.append(content)
        else:
            current += character
            index += 1
    if current:
        segments.append(current)
    return segments


_MISSING = object()


def traverse(data, path: str):
    for segment in json_path(path):
        if isinstance(data, dict):
            data = data.get(segment, _MISSING)
        elif isinstance(data, list | str):
            index = segment.strip()
            if index == "length":
                data = len(data)
            elif index.isdigit() and int(index) < len(data):
                data = data[int(index)]
            else:
                return _MISSING
        else:
            return _MISSING
        if data is _MISSING:
            return _MISSING
    return data


@function("traversejson")
async def traverse_json(rt, arg, tag):
    if SPLITTER_ARGUMENT not in arg:
        return False
    path, *rest = split(arg, 2)
    if not path or not rest or not rest[0]:
        return False
    try:
        data = json.loads(rest[0])
    except ValueError:
        return False
    result = traverse(data, path)
    if result is not _MISSING:
        tag.text += js_str(result)
    return True


@function("type")
async def type_function(rt, arg, tag):
    if SPLITTER_ARGUMENT not in arg:
        return False
    kind, *rest = split(arg, 3)
    text = rest[0] if rest else ""
    default = rest[1] if len(rest) > 1 else ""
    parser = {"float": js_parse_float, "number": js_parse_int}.get(kind.lower())
    if parser is None:
        return False
    value = parser(text)
    if value is None or (isinstance(value, float) and math.isnan(value)):
        if not default:
            return True
        value = parser(default)
    if value is None:
        return False
    tag.text += js_str(value)
    return True
