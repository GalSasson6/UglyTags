import pytest

from uglytags.media import (
    MediaError,
    attachment_expired,
    download,
    is_discord_url,
    parse_media_limit_mb,
    referenced_files,
    safe_filename,
    unique_filename,
)


def test_media_limit():
    assert parse_media_limit_mb(None) == 20
    assert parse_media_limit_mb("5") == 5
    for value in ("0", "1.5", "big"):
        with pytest.raises(ValueError, match="MAX_MEDIA_MB"):
            parse_media_limit_mb(value)


def test_filenames_are_sanitized_and_unique():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("C:\\temp\\my cat!.PNG") == "my_cat_.PNG"
    assert safe_filename("...") == "file"
    assert len(safe_filename("a" * 300 + ".png")) == 100
    assert unique_filename("cat.png", {"CAT.png"}) == "cat-2.png"
    assert unique_filename("cat", {"cat", "cat-2"}) == "cat-3"


def test_referenced_files_ignore_urls():
    content = "{attach:cat.png}{ATTACH : dog.gif|x}{file:https://cdn.discordapp.com/a}{attach:{x}}"
    assert referenced_files(content) == {"cat.png", "dog.gif"}


def test_discord_url_rules():
    assert is_discord_url("https://cdn.discordapp.com/attachments/1/2/a.png")
    assert is_discord_url("https://media.discordapp.net/attachments/1/2/a.png")
    assert not is_discord_url("http://cdn.discordapp.com/attachments/1/2/a.png")
    assert not is_discord_url("https://cdn.discordapp.com.evil.test/a.png")
    base = "https://cdn.discordapp.com/attachments/1/2/a.png"
    assert attachment_expired(base)
    assert attachment_expired(f"{base}?ex=10&is=1&hm=ab", now=100)
    assert not attachment_expired(f"{base}?ex=ffffffff&is=1&hm=ab", now=100)
    assert not attachment_expired("https://cdn.discordapp.com/avatars/1/a.png")


async def test_download_refuses_other_hosts():
    with pytest.raises(MediaError):
        await download(None, "https://example.com/a.png", 10)
