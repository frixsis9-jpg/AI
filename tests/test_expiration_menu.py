from analysis.multi_timeframe import timeframe_trio_for
from analyst.advisor import LADDER
from bot.keyboards import (
    expiration_keyboard,
    main_menu_keyboard,
    settings_keyboard,
    workspace_keyboard,
)
from bot.rendering import parse_selection
from domain import Expiration, Timeframe


def test_requested_seconds_and_all_existing_fixed_options():
    rows = expiration_keyboard().inline_keyboard
    assert [b.callback_data for b in rows[0]] == [
        "exp:SEC_5",
        "exp:SEC_10",
        "exp:SEC_15",
        "exp:SEC_30",
    ]
    assert [e.value for e in Expiration.selectable()] == [
        5,
        10,
        15,
        30,
        60,
        120,
        180,
        300,
        600,
        900,
        1800,
        3600,
    ]
    assert "exp:AUTO" not in [b.callback_data for row in rows for b in row]
    for exp in Expiration.selectable():
        assert parse_selection({"expiration": exp.name})[1] == exp
    assert parse_selection({"expiration": "SEC_20"})[1] is None
    assert parse_selection({"expiration": "garbage"})[1] is None
    assert all(not e.is_short for e in LADDER)


def test_second_profile_and_existing_minute_profiles_are_separate():
    assert timeframe_trio_for(Expiration.SEC_5) == (
        Timeframe.S15,
        Timeframe.M1,
        Timeframe.M5,
    )
    assert timeframe_trio_for(Expiration.MIN_3) == (
        Timeframe.M1,
        Timeframe.M5,
        Timeframe.M15,
    )
    assert timeframe_trio_for(Expiration.HOUR_1) == (
        Timeframe.M15,
        Timeframe.M30,
        Timeframe.H1,
    )
    actions = [
        b.callback_data for row in main_menu_keyboard().inline_keyboard for b in row
    ]
    assert actions == ["menu:manual", "mode:auto"]
    manual = [
        b.callback_data for row in workspace_keyboard({}).inline_keyboard for b in row
    ]
    assert manual == ["menu:pair", "menu:expiration", "menu:signal", "menu:home"]
    auto = [
        b.callback_data
        for row in workspace_keyboard({"pair_mode": "auto"}).inline_keyboard
        for b in row
    ]
    assert auto == ["menu:expiration", "search:start", "menu:settings", "menu:home"]
    assert "menu:watchlist" in [
        b.callback_data for row in settings_keyboard().inline_keyboard for b in row
    ]
