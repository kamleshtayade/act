"""SQLite decision & outcome log - the evaluation trail for Phase 5.

One row per graph run (executed or not), keyed by run_id so a re-delivered
webhook or a resumed run can't double-log or double-fire.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    run_type        TEXT NOT NULL,          -- live | backtest
    trade_date      TEXT NOT NULL,          -- IST date the run belongs to
    as_of           TEXT NOT NULL,
    logged_at       TEXT NOT NULL,
    side            TEXT,
    signal          TEXT,                   -- json
    depth           TEXT,                   -- json
    signal_confirmed INTEGER,
    event_block     TEXT,
    event_context   TEXT,                   -- json
    decision_mode   TEXT,
    claude_decision TEXT,                   -- json
    risk_reason     TEXT,
    approved_lots   INTEGER,
    final_status    TEXT NOT NULL,          -- executed | no_trade
    no_trade_reason TEXT,
    order_result    TEXT,                   -- json
    entry_price     REAL,
    exit_price      REAL,
    realized_pnl    REAL,                   -- INR, filled when the trade is closed
    outcome         TEXT                    -- win | loss | flat | no_trade
);
CREATE INDEX IF NOT EXISTS idx_runs_date ON runs(trade_date, run_type);
"""


def _j(v: Any) -> Optional[str]:
    return None if v is None else json.dumps(v, default=str)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    # --- reads used by risk_check ------------------------------------------
    def trade_count(self, trade_date: str, run_type: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM runs WHERE trade_date=? AND run_type=? AND final_status='executed'",
            (trade_date, run_type),
        ).fetchone()
        return int(row[0])

    def daily_pnl(self, trade_date: str, run_type: str) -> float:
        """Realized P&L only. TODO(phase 4): add sandbox mark-to-market for
        open positions so the circuit breaker sees unrealized losses too."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM runs WHERE trade_date=? AND run_type=?",
            (trade_date, run_type),
        ).fetchone()
        return float(row[0])

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def last_signal_time(self, side: str, run_type: str = "live") -> Optional[datetime]:
        row = self._conn.execute(
            "SELECT MAX(as_of) FROM runs WHERE side=? AND run_type=?", (side, run_type)
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def has_runs_with_prefix(self, prefix: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM runs WHERE run_id LIKE ? LIMIT 1", (prefix + "%",)).fetchone()
        return row is not None

    def runs(self, run_type: Optional[str] = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM runs", ()
        if run_type:
            q, args = q + " WHERE run_type=?", (run_type,)
        return [dict(r) for r in self._conn.execute(q + " ORDER BY as_of", args)]

    # --- writes --------------------------------------------------------------
    def log_run(self, state: dict[str, Any], trade_date: str) -> None:
        signal = state.get("raw_signal") or {}
        order = state.get("order_result") or {}
        row = {
            "run_id": state["run_id"],
            "run_type": state["run_type"],
            "trade_date": trade_date,
            "as_of": state["as_of"],
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "side": signal.get("side"),
            "signal": _j(signal),
            "depth": _j(state.get("confirmed_depth")),
            "signal_confirmed": int(bool(state.get("signal_confirmed"))),
            "event_block": state.get("event_block"),
            "event_context": _j(state.get("event_context")),
            "decision_mode": state.get("decision_mode"),
            "claude_decision": _j(state.get("claude_decision")),
            "risk_reason": state.get("risk_reason"),
            "approved_lots": state.get("approved_lots"),
            "final_status": state["final_status"],
            "no_trade_reason": state.get("no_trade_reason"),
            "order_result": _j(state.get("order_result")),
            "entry_price": order.get("fill_price"),
            "outcome": "no_trade" if state["final_status"] == "no_trade" else None,
        }
        cols = ", ".join(row)
        ph = ", ".join(f":{k}" for k in row)
        with self._lock, self._conn:
            self._conn.execute(f"INSERT OR IGNORE INTO runs ({cols}) VALUES ({ph})", row)

    def close_trade(self, run_id: str, exit_price: float, realized_pnl: float) -> None:
        outcome = "win" if realized_pnl > 0 else "loss" if realized_pnl < 0 else "flat"
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET exit_price=?, realized_pnl=?, outcome=? WHERE run_id=?",
                (exit_price, realized_pnl, outcome, run_id),
            )
