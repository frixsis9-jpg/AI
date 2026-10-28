"""Builds the prompt sent to Claude for the second-opinion review.

The multi-timeframe combiner (analysis.multi_timeframe) always scores a
signal as CALL or PUT now, with a 0-100 confidence — it no longer produces
WAIT on its own (see analysis/confirmation.py's docstring for why). Claude's
own review (ai/review.py) is still allowed to downgrade a signal to WAIT
when it disagrees or comes back under-confident — that's a separate,
optional safety layer on top, not something the rule engine does itself any
more. The prompt hands over every analyzed timeframe's own verdict and vote
tally (not just one timeframe) plus the combined verdict, and is told in
plain terms what it is and isn't allowed to do with that verdict.
"""
from __future__ import annotations

from analysis.multi_timeframe import MultiTimeframeResult

SYSTEM_PROMPT = """\
Ты выступаешь вторым, независимым мнением поверх уже готового технического \
сигнала для бинарных опционов (Pocket Option). Правило-based движок \
проанализировал сразу НЕСКОЛЬКО таймфреймов и свёл их в предварительный \
вердикт CALL или PUT (совпадение засчитывается только если таймфреймы не \
противоречат друг другу) — он приложен ниже вместе со всеми исходными \
данными по каждому таймфрейму.

Твоя роль строго ограничена:
- Ты НЕ можешь превратить WAIT в CALL или PUT — тебе такой сигнал вообще не \
покажут.
- Ты НЕ можешь изменить направление сигнала (CALL <-> PUT). Ты можешь либо \
согласиться с предложенным направлением, либо сказать, что не согласен \
(тогда сигнал будет переведён в WAIT автоматически — это сделает вызывающий \
код, не ты).
- Ты НЕ можешь "усилить" слабый сигнал — твоя уверенность (confidence) \
отражает ТОЛЬКО то, насколько ты сам, независимо оценивая эти же данные, \
согласен с предложенным направлением. Не ориентируйся на то, какая \
уверенность "нужна", чтобы сигнал прошёл.
- Если видишь противоречия в данных (например, тренд старшего таймфрейма \
против направления сигнала, низкая волатильность, конфликтующий price \
action) — прямо укажи это и снижай confidence.
- Никогда не выдумывай данные, которых нет в предоставленном снимке.

Ответь СТРОГО в виде одного JSON-объекта без какого-либо текста до или \
после, в формате:
{"direction": "CALL"|"PUT"|"WAIT", "confidence": 0-100, "reason": "..."}

"direction" — твоя собственная оценка направления (можешь указать "WAIT", \
если считаешь, что сигнала быть не должно вообще — это будет означать \
несогласие с предложенным направлением). "confidence" — целое число 0-100, \
насколько ты уверен именно в этом направлении. "reason" — краткое (1-3 \
предложения) объяснение на русском.
"""


def build_prompt(result: MultiTimeframeResult) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt) for one review call.

    Only called when `result.final_direction` is CALL or PUT — see
    ai/review.py.
    """
    return SYSTEM_PROMPT, _build_user_prompt(result)


def _build_user_prompt(result: MultiTimeframeResult) -> str:
    primary = result.primary
    ind = primary.indicators

    per_timeframe_text = "\n".join(
        f"— {r.timeframe.value}: {r.confirmation.direction} "
        f"({r.confirmation.bullish_votes} бычьих / {r.confirmation.bearish_votes} медвежьих факторов, "
        f"тренд след. таймфрейма: {r.higher_timeframe_trend.value})"
        for r in result.per_timeframe
    )

    levels_text = ", ".join(
        f"{level.kind} {level.price:.5f} (касаний: {level.touches})" for level in primary.levels
    ) or "не обнаружены"
    price_action_text = ", ".join(
        f"{p.pattern} ({p.bias.value})" for p in primary.price_action
    ) or "не обнаружено"

    return f"""\
Пара: {result.pair.label}
Экспирация: {result.expiration.label} ({"КОРОТКАЯ, экспериментальный режим" if result.expiration.is_short else "обычная"})

Проанализированные таймфреймы и их собственные вердикты:
{per_timeframe_text}

Сведённый (итоговый) вердикт правило-based движка: {result.final_direction} \
(согласны {result.agreement} из {result.analyzed_count} проанализированных таймфреймов; \
уверенность движка: {result.confidence:.0f}%)

Данные по самому быстрому из проанализированных таймфреймов ({primary.timeframe.value}), \
на нём же считается цена входа:
Текущая цена: {primary.current_price:.5f}
EMA9/20/50: {ind.ema9:.5f} / {ind.ema20:.5f} / {ind.ema50:.5f}
RSI(14): {ind.rsi:.1f}
MACD: линия {ind.macd_line:.5f}, сигнал {ind.macd_signal:.5f}, гистограмма {ind.macd_histogram:.5f} \
(пред. значение {ind.macd_histogram_prev:.5f})
Stochastic %K/%D: {ind.stochastic_k:.1f} / {ind.stochastic_d:.1f}
ATR(14): {ind.atr:.5f} (к недавнему среднему: {ind.atr_ratio:.2f}x)
Price action (последние закрытые свечи): {price_action_text}
Уровни поддержки/сопротивления: {levels_text}

Оцени этот сведённый сигнал и верни JSON-ответ по описанному формату.
"""
