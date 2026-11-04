"""Model outcomes, separated by pair/expiry/version; never actual broker fills."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from analysis.calibration import bucket_stats
from analysis.confirmation import MODEL_VERSION
from storage.repository import SignalRepository

router = Router(name="stats")


@router.message(Command("stats"))
async def cmd_stats(message: Message, repo: SignalRepository | None = None) -> None:
    if repo is None:
        await message.answer(
            "⚠️ Хранилище статистики не подключено (нет SignalRepository)."
        )
        return

    rows = await repo.get_stats()
    if not rows:
        await message.answer(
            "Пока нет сохранённых сигналов — статистика появится после первых запросов."
        )
        return

    wins = sum(r.wins for r in rows)
    losses = sum(r.losses for r in rows)
    lines = [
        "📈 Модельная статистика сигналов",
        "Цена вашей ручной сделки может отличаться.",
        "",
    ]
    if wins + losses:
        lines.append(
            f"Всего: ✅ {wins} / ❌ {losses} — винрейт {100 * wins / (wins + losses):.1f}%"
        )

    outcomes = await repo.resolved_outcomes(MODEL_VERSION)
    if outcomes:
        lines += ["", f"Базовая модель ({MODEL_VERSION}), по оценке вероятности:"]
        for b in bucket_stats(outcomes):
            if b.total:
                high = "+" if b.high > 100 else f"–{b.high:.0f}"
                lines.append(
                    f" • оценка {b.low:.0f}{high}%: реально {b.winrate:.0f}% (из {b.total})"
                )

    lines += ["", "По комбинациям (валюта + таймфрейм + экспирация):"]
    for row in rows:
        rate = f" — {row.winrate:.0f}%" if row.winrate is not None else ""
        lines.append(
            f"{row.label}: ✅ {row.wins} / ❌ {row.losses} / ❔ {row.unknown} / ⏳ {row.pending}{rate}"
        )

    lines += [
        "",
        "❔ UNKNOWN — не удалось получить цену точно в момент экспирации; "
        "⏳ — экспирация ещё не наступила.",
    ]

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    await message.answer(text)
