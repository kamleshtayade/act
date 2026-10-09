"""Backtest runner - same signal rule + same graph, simulated fills.

Input: a CSV of bars with at least
    timestamp, close, delta        (+ optional high, low, cvd)
`timestamp` is ISO-8601; naive timestamps are treated as IST. If `cvd` is
missing it's accumulated from `delta` (from the start of the file).

Order-flow fields (delta/CVD) need tick data - Dhan's OHLC history does not
carry them. Export bars from GoCharting's footprint chart, or aggregate your
own recorded tick feed, into this CSV shape.
"""

from __future__ import annotations

import csv
import logging
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from .config import get_contract
from .graph import Pipeline
from .integrations.events import IST
from .signals import LIPI_CVD_LOOKBACK, LIPI_DELTA_THRESHOLD, lipi_delta_imbalance_rule
from .state import Signal

log = logging.getLogger(__name__)

# Simulated exit rule - deterministic and deliberately simple.
HOLD_BARS = 6
STOP_TICKS = 25
TARGET_TICKS = 40


@dataclass
class Bar:
    ts: datetime
    close: float
    delta: float
    cvd: float
    high: Optional[float] = None
    low: Optional[float] = None


def load_bars(path: Path) -> list[Bar]:
    bars: list[Bar] = []
    cvd_acc = 0.0
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = datetime.fromisoformat(row["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            delta = float(row["delta"])
            cvd_acc += delta
            bars.append(Bar(
                ts=ts, close=float(row["close"]), delta=delta,
                cvd=float(row["cvd"]) if row.get("cvd") not in (None, "") else cvd_acc,
                high=float(row["high"]) if row.get("high") else None,
                low=float(row["low"]) if row.get("low") else None,
            ))
    return bars


def synthetic_bars(days: int = 3, seed: int = 7) -> list[Bar]:
    """Random-walk bars inside MCX hours - for smoke-testing the pipeline only."""
    rng = random.Random(seed)
    bars, px, cvd = [], 6400.0, 0.0
    start = datetime(2026, 9, 14, 9, 0, tzinfo=IST)  # a Monday
    offset, made = 0, 0
    while made < days:
        d0 = start + timedelta(days=offset)
        offset += 1
        if d0.weekday() >= 5:
            continue
        made += 1
        t = d0
        while t.hour < 23:
            delta = rng.gauss(0, 25_000)
            px += delta / 10_000 + rng.gauss(0, 3)
            cvd += delta
            bars.append(Bar(ts=t, close=round(px), delta=delta, cvd=cvd,
                            high=round(px + abs(rng.gauss(0, 4))), low=round(px - abs(rng.gauss(0, 4)))))
            t += timedelta(minutes=5)
    return bars


def _simulate_exit(bars: list[Bar], i: int, side: str, entry: float, tick: float) -> tuple[int, float]:
    sign = 1 if side == "LONG" else -1
    stop, target = entry - sign * STOP_TICKS * tick, entry + sign * TARGET_TICKS * tick
    day = bars[i].ts.astimezone(IST).date()
    j = i
    for j in range(i + 1, min(i + 1 + HOLD_BARS, len(bars))):
        b = bars[j]
        if b.ts.astimezone(IST).date() != day:  # flatten at end of day
            return j - 1, bars[j - 1].close
        lo, hi = b.low if b.low is not None else b.close, b.high if b.high is not None else b.close
        if (sign > 0 and lo <= stop) or (sign < 0 and hi >= stop):
            return j, stop  # stop first: pessimistic when both are touched
        if (sign > 0 and hi >= target) or (sign < 0 and lo <= target):
            return j, target
    return j, bars[j].close


def run_backtest(
    bars: list[Bar],
    pipeline: Pipeline,
    tag: str,
    decision_mode: str = "claude",
    threshold: float = LIPI_DELTA_THRESHOLD,
    lookback: int = LIPI_CVD_LOOKBACK,
) -> dict:
    if pipeline.deps.store.has_runs_with_prefix(f"bt-{tag}-{decision_mode}-"):
        raise ValueError(f"backtest tag {tag!r} already used for mode {decision_mode} - pick a new tag")
    contract = get_contract()
    tick, lot_bbl = contract["tick_size_inr"], contract["lot_size_bbl"]
    deltas, cvds = [b.delta for b in bars], [b.cvd for b in bars]
    busy_until = -1
    summary = {"signals": 0, "executed": 0, "no_trade": 0, "pnl_inr": 0.0, "wins": 0, "losses": 0}

    for i, bar in enumerate(bars):
        if i <= busy_until:
            continue  # one position at a time
        side = lipi_delta_imbalance_rule(deltas, cvds, i, threshold, lookback)
        if not side:
            continue
        summary["signals"] += 1
        signal = Signal(side=side, delta=bar.delta, cvd=bar.cvd, price=bar.close, source="backtest")
        run_id = f"bt-{tag}-{decision_mode}-{bar.ts:%Y%m%dT%H%M}"
        result = pipeline.run(signal, run_id, "backtest", bar.ts, decision_mode)
        if result.get("final_status") != "executed":
            summary["no_trade"] += 1
            continue

        order = result["order_result"]
        entry, lots = order["fill_price"], order["lots"]
        exit_i, exit_px = _simulate_exit(bars, i, side, entry, tick)
        sign = 1 if side == "LONG" else -1
        pnl = sign * (exit_px - entry) * lot_bbl * lots
        pipeline.deps.store.close_trade(run_id, exit_px, pnl)
        busy_until = exit_i
        summary["executed"] += 1
        summary["pnl_inr"] += pnl
        if pnl > 0:
            summary["wins"] += 1
        elif pnl < 0:
            summary["losses"] += 1

    summary["pnl_inr"] = round(summary["pnl_inr"], 2)
    return summary


def evaluate(rows: Iterable[dict]) -> dict:
    """Phase 5 metrics over closed trades in the log."""
    pnls = [r["realized_pnl"] for r in rows if r.get("realized_pnl") is not None]
    if not pnls:
        return {"trades": 0}
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    mean = sum(pnls) / len(pnls)
    sd = math.sqrt(sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)) if len(pnls) > 1 else 0.0
    return {
        "trades": len(pnls),
        "total_pnl_inr": round(sum(pnls), 2),
        "win_rate": round(sum(p > 0 for p in pnls) / len(pnls), 3),
        "max_drawdown_inr": round(max_dd, 2),
        "per_trade_sharpe": round(mean / sd, 3) if sd else None,
    }
