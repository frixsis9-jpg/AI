"""SQLite schema for the signals table."""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    pair TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    expiration_seconds INTEGER NOT NULL,
    rule_direction TEXT NOT NULL,
    final_direction TEXT NOT NULL,
    confidence REAL,
    claude_consulted INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    indicators_json TEXT NOT NULL,
    reason TEXT,
    expires_at TEXT NOT NULL,
    outcome TEXT,
    exit_price REAL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_pair_tf_exp
    ON signals (pair, timeframe, expiration_seconds);

CREATE INDEX IF NOT EXISTS idx_signals_pending
    ON signals (outcome, expires_at);
"""

# Columns added after the first release — applied by SignalRepository.init()
# with ALTER TABLE when an older signals.db doesn't have them yet.
MIGRATION_COLUMNS: dict[str, str] = {
    # which analysis model produced the signal (calibration only uses
    # outcomes of the current model — old ones measured a different thing)
    "model_version": "TEXT",
    # every factor reading per timeframe, JSON — training data for
    # backtest/fit_weights.py once real outcomes accumulate
    "features_json": "TEXT",
}
