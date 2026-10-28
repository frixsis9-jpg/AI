"""Docker-only launcher; leaves application code and configuration untouched."""
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parent.parent
required = (
    "ai/errors.py",
    "analyst/advisor.py",
    "analyst/claude_desk.py",
    "bot/handlers/selection.py",
    "bot/handlers/signal.py",
    "bot/handlers/start.py",
    "bot/handlers/stats.py",
    "market_data/models.py",
    "market_data/pocket_option.py",
    "market_data/session_monitor.py",
    "market_data/ssid.py",
    "storage/repository.py",
    "storage/resolver.py",
)
missing = [name for name in required if not (root / name).is_file()]
if missing:
    print(
        "Не хватает файлов исходного бота:\n  " + "\n  ".join(missing)
        + "\nСкопируйте все папки из рабочей версии бота рядом с main.py "
        "и выполните docker compose up -d --build.",
        file=sys.stderr,
    )
    sys.exit(1)

os.chdir(root)
os.execv(sys.executable, [sys.executable, "main.py"])
