"""Hard-coded risk limits.

These are constants on purpose - NEVER move them into claude_decide, an LLM
prompt, or `.env`. Changing one should be a reviewed code change.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

MAX_DAILY_LOSS_INR = -5_000.0  # paper rupees; circuit breaker for the session
MAX_TRADES_PER_DAY = 3  # matches the 3 live executions/day cadence
MAX_LOTS_PER_TRADE = 1  # CRUDEOIL lot = 100 bbl. NOTE: with 1 lot, any
# Claude "adjust" below 1.0 floors to 0 lots (= no trade). To make partial
# sizing meaningful, trade CRUDEOILM (10 bbl) with a larger lot cap.

# ingest confirm-check: visible book must lean the signal's way by this ratio
MIN_DEPTH_CONFIRM_RATIO = 1.10


@dataclass(frozen=True)
class RiskVerdict:
    approved: bool
    lots: int
    reason: Optional[str]


def evaluate(daily_pnl: float, trade_count: int, size_multiplier: float) -> RiskVerdict:
    if daily_pnl <= MAX_DAILY_LOSS_INR:
        return RiskVerdict(False, 0, f"daily loss limit breached ({daily_pnl:.0f} <= {MAX_DAILY_LOSS_INR:.0f})")
    if trade_count >= MAX_TRADES_PER_DAY:
        return RiskVerdict(False, 0, f"max trades/day reached ({trade_count})")
    mult = min(max(size_multiplier, 0.0), 1.0)  # Claude can only shrink size
    lots = math.floor(MAX_LOTS_PER_TRADE * mult + 1e-9)
    if lots <= 0:
        return RiskVerdict(False, 0, f"size multiplier {mult:.2f} rounds to 0 lots")
    return RiskVerdict(True, lots, None)
