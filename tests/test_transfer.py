import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from uglytags import transfer

URL = "https://cdn.discordapp.com/attachments/1/2/cat.png?ex=ffffffff&is=1&hm=ab"


def tag_row(**overrides):
    row = {
        "id": 111111111111111111,
        "name": "hello",
        "content": "hi",
        "owner_id": 222222222222222222,
        "reference_id": None,
        "reference_name": None,
        "reference_owner_id": None,
        "uses": 3,
        "created_at": datetime(2024, 1, 1, tzinfo=UTC),
        "edited_at": None,
        "last_used_at": None,
    }
    row.update(overrides)
    return row


def test_export_is_notsobot_compatible():
    alias = tag_row(
        id=333333333333333333,
        name="hey",
        content="",
        reference_id=111111111111111111,
        reference_name="hello",
        reference_owner_id=222222222222222222,
    )
    filename, data = transfer.export_payload(
        9, [tag_row(), alias], {111111111111111111: {"a.txt": b"x"}}, {"name": "h"}
    )
    payload = json.loads(data)
    assert filename.startswith("tags-exported-9-2-filtered-")
    assert payload["serverId"] == "9"
    assert payload["options"] == {"name": "h"}
    first, second = payload["tags"]
    assert first["user"]["id"] == "222222222222222222"
    assert first["created"] == "2024-01-01T00:00:00+00:00"
    assert first[transfer.FILES_KEY] == [{"filename": "a.txt", "data": "eA=="}]
    assert second["reference_tag"]["id"] == "111111111111111111"


def test_parse_entries_validates_notsobot_tags():
    report = transfer.ImportReport()
    data = json.dumps(
        {
            "tags": [
                {
                    "id": "111111111111111111",
                    "name": "a\nb",
                    "content": "x",
                    "user": {"id": "222222222222222222"},
                    "created": "2020-01-01T00:00:00Z",
                    "uses": 5,
                },
                {
                    "id": "333333333333333333",
                    "name": "alias",
                    "content": "",
                    "user": {"id": "222222222222222222"},
                    "reference_tag": {"id": "111111111111111111", "content": ""},
                },
                {"name": "no owner", "content": "x"},
                {"name": "empty", "content": " ", "user": {"id": "222222222222222222"}},
                {
                    "name": "files",
                    "content": "{attach:a.txt}",
                    "user": {"id": "222222222222222222"},
                    transfer.FILES_KEY: [
                        {"filename": "../a.txt", "data": base64.b64encode(b"hi").decode()}
                    ],
                },
            ]
        }
    ).encode()
    entries = transfer.parse_entries(data, report)
    assert [entry["name"] for entry in entries] == ["ab", "alias", "files"]
    assert entries[0]["uses"] == 5
    assert entries[0]["created_at"].year == 2020
    assert entries[1]["reference_id"] == 111111111111111111
    assert entries[2]["files"] == {"a.txt": b"hi"}
    assert report.invalid == 2
    with pytest.raises(transfer.TransferError):
        transfer.parse_entries(b"not json", report)


async def test_attachment_links_become_stored_files(monkeypatch):
    entries = [
        {"content": f"{{attach:{URL}}} {{attach:{URL}|x.png}}", "files": {}},
        {"content": "{attach:https://cdn.discordapp.com/attachments/1/2/gone.png}", "files": {}},
    ]

    async def fake_download(session, url, max_bytes):
        if "gone" in url:
            raise transfer.MediaError("missing")
        return b"png"

    monkeypatch.setattr(transfer, "download", fake_download)
    monkeypatch.setattr(transfer, "refresh_urls", AsyncMock(return_value={}))
    report = transfer.ImportReport()
    await transfer.store_attachments(entries, None, None, 10, report)
    assert entries[0]["content"] == "{attach:cat.png} {attach:cat-2.png|x.png}"
    assert entries[0]["files"] == {"cat.png": b"png", "cat-2.png": b"png"}
    assert entries[1]["content"].startswith("{attach:https://")
    assert report.failed_files == 1


async def test_import_download_budget(monkeypatch):
    monkeypatch.setattr(transfer, "MAX_IMPORT_DOWNLOAD_BYTES", 3)
    calls = []

    async def fake_download(session, url, max_bytes):
        calls.append(max_bytes)
        return b"abc"

    monkeypatch.setattr(transfer, "download", fake_download)
    monkeypatch.setattr(transfer, "refresh_urls", AsyncMock(return_value={}))
    base = "https://cdn.discordapp.com/attachments/1/2/"
    entries = [{"content": f"{{attach:{base}a.png}}{{attach:{base}b.png}}", "files": {}}]
    report = transfer.ImportReport()
    await transfer.store_attachments(entries, None, None, 10, report)
    assert calls == [3]
    assert report.failed_files == 1


def test_parse_entries_rejects_schema_violations():
    report = transfer.ImportReport()
    owner = {"id": "222222222222222222"}
    data = json.dumps(
        [
            {"name": "big", "content": "x", "user": {"id": "999999999999999999999"}},
            {
                "name": "empty file",
                "content": "x",
                "user": owner,
                transfer.FILES_KEY: [{"filename": "a", "data": ""}],
            },
            {"name": "bool", "content": "x", "user": owner, "uses": True},
        ]
    ).encode()
    entries = transfer.parse_entries(data, report)
    assert [entry["name"] for entry in entries] == ["bool"]
    assert entries[0]["uses"] == 0
    assert report.invalid == 2


async def test_import_remaps_taken_ids(monkeypatch):
    data = json.dumps(
        [
            {
                "id": "111111111111111111",
                "name": "a",
                "content": "x",
                "user": {"id": "222222222222222222"},
            },
            {
                "id": "333333333333333333",
                "name": "b",
                "user": {"id": "222222222222222222"},
                "reference_tag": {"id": "111111111111111111"},
            },
        ]
    ).encode()
    db = SimpleNamespace(
        existing_ids=AsyncMock(return_value={111111111111111111}),
        import_tags=AsyncMock(return_value=["a", "b"]),
    )
    bot = SimpleNamespace(db=db, http=None, session=None, max_media_bytes=10)
    report = await transfer.import_tags(bot, 9, data)
    entries = db.import_tags.await_args.args[1]
    assert entries[0]["id"] != 111111111111111111
    assert entries[1]["reference_id"] == entries[0]["id"]
    assert (report.imported, report.skipped, report.invalid) == (2, 0, 0)
