import json
from pathlib import Path

_locale = Path(__file__).with_name("locales") / "en-US.json"
if not _locale.exists():
    _locale = Path(__file__).resolve().parents[2] / "static" / "locales" / "en-US.json"
_messages = json.loads(_locale.read_text(encoding="utf-8"))


def t(key: str, **values: object) -> str:
    return _messages[key].format(**values)
