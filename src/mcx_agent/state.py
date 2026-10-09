"""Graph state and the typed payloads that flow through it."""

from __future__ import annotations

from typing import Any, Literal, Optional, TypedDict

from pydantic import BaseModel, Field

Side = Literal["LONG", "SHORT"]


class Signal(BaseModel):
    """A normalised order-flow signal - from a GoCharting webhook (live) or
    from the Python port of the Lipi rule run over historical bars (backtest)."""

    side: Side
    symbol: str = "CRUDEOIL"
    delta: Optional[float] = None
    cvd: Optional[float] = None
    price: Optional[float] = None  # bar close / LTP when known (backtest always)
    source: Literal["gocharting", "backtest", "manual"] = "gocharting"
    raw: Any = None


class DepthSnapshot(BaseModel):
    security_id: str
    ltp: float
    bid_qty: float  # total resting buy quantity across visible levels
    ask_qty: float  # total resting sell quantity across visible levels
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    levels: int = 0
    source: Literal["dhan", "mock", "backtest"] = "dhan"

    @property
    def imbalance_ratio(self) -> float:
        """bid/ask for LONG confirmation; >1 means buyers are heavier."""
        return self.bid_qty / self.ask_qty if self.ask_qty else float("inf")


class ClaudeDecision(BaseModel):
    """Structured output schema for the claude_decide node."""

    action: Literal["confirm", "veto", "adjust"] = Field(
        description="confirm = take the trade at full allowed size; adjust = take it "
        "at reduced size; veto = do not trade."
    )
    size_multiplier: float = Field(
        description="Fraction of the allowed position size, between 0.0 and 1.0. "
        "Use 1.0 for confirm, 0.0 for veto, and a value in between for adjust."
    )
    rationale: str = Field(description="Two to four sentences explaining the decision.")
    risk_flags: list[str] = Field(
        default_factory=list,
        description="Short labels for concerns noticed, e.g. 'eia_soon', 'thin_book', 'cvd_divergence'.",
    )


class TradingState(TypedDict, total=False):
    run_type: Literal["live", "backtest"]
    run_id: str
    as_of: str  # ISO timestamp the run is evaluated at (bar time in backtests)

    # ingest
    raw_signal: dict  # Signal.model_dump()
    confirmed_depth: Optional[dict]  # DepthSnapshot.model_dump()
    signal_confirmed: bool
    confirm_reason: str

    # event_guard
    event_block: Optional[str]  # set => hard block
    event_context: list[str]  # non-blocking upcoming events, shown to Claude
    macro_context: dict  # e.g. latest EIA inventory numbers

    # claude_decide
    decision_mode: Literal["claude", "passthrough"]
    claude_decision: Optional[dict]

    # risk_check
    risk_approved: bool
    risk_reason: Optional[str]
    approved_lots: int

    # execute_log / log_no_trade
    order_result: Optional[dict]
    final_status: Literal["executed", "no_trade"]
    no_trade_reason: Optional[str]
