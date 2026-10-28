"""Validates POCKET_OPTION_SSID the same way the installed PocketOptionAsync
(binaryoptionstoolsv2==0.2.14) actually does — mirrored from its own source
(`BinaryOptionsToolsV2/pocketoption/asynchronous.py`,
`sanitize_and_validate_ssid()`), not guessed.

This does NOT confirm the SSID still works on Pocket Option's side — only a
real network round-trip can do that (see
PocketOptionProvider.verify_connection in market_data/pocket_option.py). It
only catches format problems: the .env placeholder left unchanged, stray
whitespace/newlines from a sloppy copy, and — the important part — a value
that is not the exact JSON shape PocketOptionAsync requires, including the
same two fields it treats as mandatory ("session" and "uid").

Where this value actually comes from (verified against the installed
package, not assumed): PocketOptionAsync expects the full raw Socket.IO
"auth" frame, e.g.

    42["auth",{"session":"<...>","isDemo":0,"uid":12345678,"platform":1}]

As of this Pocket Option site version there is no browser cookie literally
named "ssid" — the package's own bundled `pocketoption/tools/login.py`
reads the session token from the **`po_session`** cookie instead (its bundled
README still says to look for a cookie called "ssid"; that line is simply
stale relative to its own code, confirmed by reading both). Two ways to get
this value yourself, safely, without automating login:

1. (Recommended, easiest to find) DevTools -> Application -> Cookies ->
   pocketoption.com -> cookie named `po_session` -> its value goes into the
   "session" field of a string you build yourself, using "uid":0 — not a
   placeholder, but the exact value the library's own bundled login helper
   (`pocketoption/tools/login.py`, function `login()`) always sends
   regardless of the real account (verified by reading that function's
   source: it literally returns `...,"uid":0,...}]`). `uid` only affects a
   soft warning in sanitize_and_validate_ssid(), never a hard failure, so
   there is no need to separately look up your real numeric uid.
2. (Alternative, carries your real uid) DevTools -> Network -> filter "WS"
   -> the Pocket Option WebSocket connection -> Messages/Frames -> the
   outgoing frame starting with `42["auth",` sent right after the page
   connects. That is the literal message your own already-logged-in browser
   session sends to authenticate itself — nothing to reconstruct or guess.
"""
from __future__ import annotations

import json
import re

_PLACEHOLDER_VALUES = {"", "your_pocket_option_ssid_here"}
_MIN_LENGTH = 20
_REQUIRED_AUTH_FIELDS = ("session", "uid")

# Same fix the real library applies before validating: shell/copy-paste can
# strip the quotes around the "auth" event name (e.g. `42[auth,` instead of
# `42["auth",`) — normalize that rather than rejecting it outright.
_AUTH_KEY_QUOTE_FIX = re.compile(r"""42\[['"]?auth['"]?\s*,""")


class InvalidSsidFormatError(ValueError):
    """The configured POCKET_OPTION_SSID clearly isn't a real session value.

    Raised before any network connection is attempted — see the specific
    message for which check failed and what to do about it.
    """


def validate_ssid_format(raw: str) -> str:
    """Returns the SSID (sanitized the same way PocketOptionAsync would), or
    raises InvalidSsidFormatError with a specific, actionable reason (in
    Russian, matching the rest of this project's user-facing text).

    Beyond the placeholder/whitespace checks specific to this project, the
    JSON-shape checks below intentionally mirror
    `sanitize_and_validate_ssid()` in the installed
    BinaryOptionsToolsV2/pocketoption/asynchronous.py: same required fields
    ("session", "uid"), same quote-fix regex, same "42[" + JSON-array shape.
    This function is stricter than the real one on a few points it only
    warns about (invalid JSON, wrong array shape) — the whole point here is
    to fail before ever opening a connection, so treating those as hard
    errors is more useful than a warning nobody would see.
    """
    stripped = raw.strip()

    if stripped.lower() in _PLACEHOLDER_VALUES:
        raise InvalidSsidFormatError(
            "POCKET_OPTION_SSID пуст или оставлен как плейсхолдер "
            "'your_pocket_option_ssid_here'. Вставьте туда значение, "
            "полученное из браузера — см. README, раздел «Как получить SSID»."
        )

    if raw != stripped:
        raise InvalidSsidFormatError(
            "POCKET_OPTION_SSID содержит лишние пробелы или переносы строк "
            "по краям — похоже на артефакт копирования. Скопируйте значение "
            "заново, не прихватывая соседние символы."
        )

    if any(ch in stripped for ch in ("\n", "\r", "\t")):
        raise InvalidSsidFormatError(
            "POCKET_OPTION_SSID содержит перенос строки или таб внутри "
            "значения. Настоящее значение — одна строка без переносов; "
            "похоже, скопирован не только он, а что-то ещё вместе с ним."
        )

    if len(stripped) < _MIN_LENGTH:
        raise InvalidSsidFormatError(
            f"POCKET_OPTION_SSID подозрительно короткий ({len(stripped)} "
            "символов) для настоящего значения — похоже, скопирована не та "
            "строка."
        )

    sanitized = _AUTH_KEY_QUOTE_FIX.sub('42["auth",', stripped, count=1)

    if not sanitized.startswith("42["):
        raise InvalidSsidFormatError(
            "POCKET_OPTION_SSID должен начинаться с '42[\"auth\",' — это "
            "формат Socket.IO-сообщения авторизации, который ожидает "
            "PocketOptionAsync. Похоже, скопировано что-то другое (например, "
            "значение не той cookie). См. README, раздел «Как получить SSID»."
        )

    try:
        payload = json.loads(sanitized[2:])
    except json.JSONDecodeError as exc:
        raise InvalidSsidFormatError(
            "POCKET_OPTION_SSID начинается правильно ('42['), но дальше не "
            f"распарсился как JSON ({exc}). Скопируйте значение целиком, "
            "ничего не обрезая и не добавляя лишних символов."
        ) from exc

    if not isinstance(payload, list) or len(payload) < 2:
        raise InvalidSsidFormatError(
            "POCKET_OPTION_SSID распарсился, но это не тот формат — ожидается "
            'массив вида ["auth", {...}] минимум из двух элементов, как в '
            '42["auth",{"session":"...","uid":...}].'
        )

    event_name, auth_data = payload[0], payload[1]

    if event_name != "auth":
        raise InvalidSsidFormatError(
            f"POCKET_OPTION_SSID — это Socket.IO-сообщение типа {event_name!r}, "
            "а не 'auth'. Нужен именно кадр авторизации, начинающийся с "
            '42["auth",...] — см. README, раздел «Как получить SSID».'
        )

    if not isinstance(auth_data, dict):
        raise InvalidSsidFormatError(
            "POCKET_OPTION_SSID: данные после \"auth\" — не JSON-объект, как "
            "ожидает PocketOptionAsync."
        )

    missing = [field for field in _REQUIRED_AUTH_FIELDS if field not in auth_data]
    if missing:
        raise InvalidSsidFormatError(
            "POCKET_OPTION_SSID не содержит обязательных полей: "
            + ", ".join(missing)
            + ". Именно так же это проверяет сам PocketOptionAsync при "
            'подключении — нужна полная строка вида 42["auth",{"session":'
            '"...","uid":...,"isDemo":0,"platform":1}].'
        )

    return sanitized
