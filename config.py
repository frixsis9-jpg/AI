"""Application configuration, loaded from environment variables / .env file."""
from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from market_data.ssid import validate_ssid_format


class Settings(BaseSettings):
    """All runtime configuration lives here — nothing is hardcoded elsewhere."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str

    # Pocket Option session id — the bot's default (and only, for now) market
    # data source. One shared account for every bot user, see README for how
    # to obtain this value and what its risk trade-offs are.
    #
    # Validated eagerly (see market_data.ssid) so an obviously wrong value —
    # the .env placeholder left in place, stray whitespace, a value that
    # isn't shaped like Pocket Option's actual session cookie — fails loudly
    # right at startup with a specific, actionable message, instead of
    # surfacing as a confusing connection error later. This only catches
    # *format* problems; an expired-but-well-formed SSID can only be caught
    # by an actual connection attempt — see
    # PocketOptionProvider.verify_connection, called once at startup in
    # main.py.
    pocket_option_ssid: str

    @field_validator("pocket_option_ssid")
    @classmethod
    def _validate_pocket_option_ssid(cls, value: str) -> str:
        return validate_ssid_format(value)

    # Twelve Data is no longer the default source (see README) but the
    # provider code is kept around as a fallback, so its key stays optional.
    twelve_data_api_key: str | None = None

    # Optional proxy the bot uses ONLY for its own connection to Telegram's
    # servers (api.telegram.org) — e.g. socks5://127.0.0.1:40000 for
    # Cloudflare WARP running in Proxy Mode as a background Windows service.
    # See README, "Работа без ручного впн" for why this exists: in some
    # regions (this project's user is in Kyrgyzstan) Telegram is blocked at
    # the network level, so *some* tunnel is unavoidable for reaching it —
    # but with this set, that tunnel runs invisibly as a Windows service and
    # the bot connects through it every time on its own. Nothing else
    # (Pocket Option, TwelveData) is routed through this proxy — only
    # aiogram's own Bot session. Leave unset if Telegram is reachable
    # directly from this machine.
    telegram_proxy_url: str | None = None

    @field_validator("telegram_proxy_url", mode="before")
    @classmethod
    def _blank_proxy_url_means_unset(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    # Optional: when unset, the bot still runs but every signal skips the
    # Claude second-opinion review (see ai/review.py) and is shown/stored as
    # a rule-only signal instead of failing outright.
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"

    # The Claude "senior analyst" note in the 🧠 Авто mode (analyst/claude_desk.py).
    # It only comments — it never changes the direction or the expiration — and
    # is skipped silently when there is no real API key. Set to false to turn
    # the note off even with a key.
    analyst_enabled: bool = True

    # Where the SQLite signal log lives (see storage/).
    database_path: str = "signals.db"
    # How often the background task checks for expired signals to resolve.
    resolver_interval_seconds: float = 15.0

    # Background Pocket Option connection monitor (market_data/session_monitor.py):
    # periodically re-checks the connection and — if telegram_admin_chat_id is
    # set — messages that chat when it looks like the SSID has actually died
    # (not just a one-off blip), and again once it recovers. See README,
    # "24/7 без присмотра: что делать, когда SSID протухнет".
    telegram_admin_chat_id: int | None = None
    pocket_option_health_check_interval_seconds: float = 300.0
    pocket_option_health_check_failure_threshold: int = 3

    @field_validator("telegram_admin_chat_id", mode="before")
    @classmethod
    def _blank_admin_chat_id_means_unset(cls, value: object) -> object:
        # Unlike the str|None fields above (e.g. twelve_data_api_key), an
        # empty .env value here would otherwise fail int parsing outright —
        # TELEGRAM_ADMIN_CHAT_ID= (left blank, as in .env.example) must mean
        # "not configured", not a validation error.
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    log_level: str = "INFO"


# Imported once and reused everywhere (avoids re-reading .env on every access).
settings = Settings()  # type: ignore[call-arg]  # values come from the environment / .env
