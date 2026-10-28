"""The analyst desk: a decision layer on top of the quantitative model.

* analyst.advisor      — consensus direction across timeframe trios, a market-
                         state-driven choice of expiration, reasons and warnings
                         (deterministic, instant, fully tested);
* analyst.claude_desk  — an optional Claude "senior analyst" that explains and
                         challenges the decision in words. It cannot change the
                         direction or the expiration.

Nothing here modifies analysis/ — the manual mode (user picks the expiration)
is untouched and keeps working exactly as before.
"""
