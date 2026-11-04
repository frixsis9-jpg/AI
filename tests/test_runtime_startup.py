"""Start and stop the real entry point with fake external services in isolation."""

import subprocess
import sys


def test_main_lifecycle_initializes_storage_and_never_auto_starts_search():
    script = """
import asyncio, tempfile, sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import main
with tempfile.TemporaryDirectory() as directory:
    path=str(Path(directory)/"signals.db")
    settings=SimpleNamespace(log_level="CRITICAL",telegram_proxy_url=None,telegram_bot_token="dummy",
        database_path=path,pocket_option_ssid="dummy",search_max_feeds=2,
        search_interval_seconds=5,search_lead_seconds=20,search_timezone="Asia/Bishkek",
        telegram_admin_chat_id=None,resolver_interval_seconds=1,
        pocket_option_health_check_failure_threshold=3,pocket_option_health_check_interval_seconds=300)
    provider=Mock()
    provider.verify_connection=AsyncMock()
    provider.aclose=AsyncMock()
    bot=Mock();bot.session.close=AsyncMock();bot.send_message=AsyncMock()
    polling=AsyncMock()
    with patch.object(main,"_load_settings",return_value=settings), patch.object(main,"Bot",return_value=bot), patch.object(main,"PocketOptionProvider",return_value=provider), patch.object(main.Dispatcher,"start_polling",polling):
        asyncio.run(main.main())
    provider.verify_connection.assert_awaited_once_with(require_quotes=True)
    provider.aclose.assert_awaited_once()
    bot.session.close.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    assert not polling.call_args.kwargs["search"].tasks
    with sqlite3.connect(path) as conn:
        tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"signals","menu_state","search_events"}<=tables
print("Startup/shutdown OK; no network, orders or automatic notifications")
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    assert "Startup/shutdown OK" in result.stdout
