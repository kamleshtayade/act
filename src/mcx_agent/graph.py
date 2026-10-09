"""The 5-node LangGraph pipeline (PDF section 3).

    ingest -> event_guard -> claude_decide -> risk_check -> execute_log
                  |                |               |
                  v                v               v
              log_no_trade   log_no_trade    log_no_trade

Live and backtest runs share this graph; only data sources differ:
* ingest: live confirms against Dhan depth; backtest has no historical depth.
* execute_log: live places a Sandbox order; backtest simulates the fill.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph

from . import risk
from .config import Settings, get_contract, get_settings
from .integrations import claude, dhan, events
from .state import ClaudeDecision, DepthSnapshot, Signal, TradingState
from .store import Store

log = logging.getLogger(__name__)

BACKTEST_SLIPPAGE_TICKS = 1


@dataclass
class Deps:
    """Everything a node touches outside the state. Swap pieces in tests."""

    settings: Settings
    store: Store
    fetch_depth: Callable[..., DepthSnapshot] = dhan.fetch_depth
    place_order: Callable[..., dict] = dhan.place_paper_order
    decide: Callable[[Settings, dict], dict] = claude.call_claude_decide
    event_check: Callable[..., events.EventCheck] = events.check_event_risk
    macro_snapshot: Callable[[Settings], dict] = events.fetch_eia_snapshot


def _as_of(state: TradingState) -> datetime:
    return datetime.fromisoformat(state["as_of"])


def trade_date_of(state: TradingState) -> str:
    return _as_of(state).astimezone(events.IST).date().isoformat()


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------

def make_nodes(deps: Deps) -> dict[str, Callable[[TradingState], dict]]:
    s = deps.settings

    def ingest(state: TradingState) -> dict:
        signal = Signal.model_validate(state["raw_signal"])
        if state["run_type"] == "backtest":
            price = signal.price or 0.0
            depth = DepthSnapshot(security_id="BACKTEST", ltp=price, bid_qty=0, ask_qty=0, source="backtest")
            return {
                "confirmed_depth": depth.model_dump(),
                "signal_confirmed": True,
                "confirm_reason": "backtest: no historical depth - confirm-check skipped",
            }
        try:
            sec_id = dhan.resolve_security_id(s, trade_date_of(state))
            depth = deps.fetch_depth(s, sec_id, signal.side)
        except dhan.DhanError as e:
            return {"confirmed_depth": None, "signal_confirmed": False, "confirm_reason": f"depth unavailable: {e}"}

        ratio = depth.imbalance_ratio if signal.side == "LONG" else (
            depth.ask_qty / depth.bid_qty if depth.bid_qty else float("inf")
        )
        ok = ratio >= risk.MIN_DEPTH_CONFIRM_RATIO
        reason = f"book ratio {ratio:.2f} {'>=' if ok else '<'} {risk.MIN_DEPTH_CONFIRM_RATIO} for {signal.side}"
        return {"confirmed_depth": depth.model_dump(), "signal_confirmed": ok, "confirm_reason": reason}

    def event_guard(state: TradingState) -> dict:
        check = deps.event_check(s, _as_of(state), state["run_type"])
        macro = deps.macro_snapshot(s) if state["run_type"] == "live" and not check.block else {}
        return {"event_block": check.block, "event_context": check.context, "macro_context": macro}

    def claude_decide(state: TradingState) -> dict:
        if state.get("decision_mode") == "passthrough":
            d = ClaudeDecision(action="confirm", size_multiplier=1.0, rationale="passthrough baseline (no LLM)")
            return {"claude_decision": d.model_dump()}
        trade_date = trade_date_of(state)
        context: dict[str, Any] = {
            "as_of_ist": _as_of(state).astimezone(events.IST).isoformat(),
            "run_type": state["run_type"],
            "signal": {k: v for k, v in state["raw_signal"].items() if k != "raw"},
            "order_book": state.get("confirmed_depth"),
            "order_book_check": state.get("confirm_reason"),
            "upcoming_events": state.get("event_context") or [],
            "macro": state.get("macro_context") or {},
            "today": {
                "trades_taken": deps.store.trade_count(trade_date, state["run_type"]),
                "realized_pnl_inr": deps.store.daily_pnl(trade_date, state["run_type"]),
                "max_trades_per_day": risk.MAX_TRADES_PER_DAY,
            },
            "contract": {k: get_contract()[k] for k in ("symbol", "lot_size_bbl", "tick_size_inr")},
        }
        return {"claude_decision": deps.decide(s, context)}

    def risk_check(state: TradingState) -> dict:
        trade_date = trade_date_of(state)
        verdict = risk.evaluate(
            daily_pnl=deps.store.daily_pnl(trade_date, state["run_type"]),
            trade_count=deps.store.trade_count(trade_date, state["run_type"]),
            size_multiplier=(state.get("claude_decision") or {}).get("size_multiplier", 0.0),
        )
        return {"risk_approved": verdict.approved, "risk_reason": verdict.reason, "approved_lots": verdict.lots}

    def execute_log(state: TradingState) -> dict:
        signal = Signal.model_validate(state["raw_signal"])
        depth = DepthSnapshot.model_validate(state["confirmed_depth"])
        lots = state["approved_lots"]
        tick = get_contract()["tick_size_inr"]
        if state["run_type"] == "live":
            px = (depth.best_ask if signal.side == "LONG" else depth.best_bid) or depth.ltp
            try:
                result = deps.place_order(s, depth.security_id, signal.side, lots, px, state["run_id"])
                result["fill_price"] = px  # assumed marketable-limit fill; reconcile from sandbox later
            except dhan.DhanError as e:
                update = {"order_result": {"status": "error", "error": str(e)}, "final_status": "no_trade",
                          "no_trade_reason": f"order failed: {e}"}
                deps.store.log_run({**state, **update}, trade_date_of(state))
                return update
        else:
            slip = BACKTEST_SLIPPAGE_TICKS * tick
            px = (signal.price or 0.0) + (slip if signal.side == "LONG" else -slip)
            result = {"status": "simulated_fill", "side": signal.side, "lots": lots, "fill_price": px}
        update = {"order_result": result, "final_status": "executed", "no_trade_reason": None}
        deps.store.log_run({**state, **update}, trade_date_of(state))
        return update

    def log_no_trade(state: TradingState) -> dict:
        if state.get("event_block"):
            reason = f"event_guard: {state['event_block']}"
        elif not state.get("signal_confirmed"):
            reason = f"ingest: signal not confirmed ({state.get('confirm_reason')})"
        elif (state.get("claude_decision") or {}).get("action") == "veto":
            reason = f"claude veto: {state['claude_decision'].get('rationale')}"
        else:
            reason = f"risk_check: {state.get('risk_reason')}"
        update = {"order_result": None, "final_status": "no_trade", "no_trade_reason": reason}
        deps.store.log_run({**state, **update}, trade_date_of(state))
        return update

    return {
        "ingest": ingest,
        "event_guard": event_guard,
        "claude_decide": claude_decide,
        "risk_check": risk_check,
        "execute_log": execute_log,
        "log_no_trade": log_no_trade,
    }


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

def route_after_event_guard(state: TradingState) -> str:
    if state.get("event_block") or not state.get("signal_confirmed", False):
        return "log_no_trade"
    return "claude_decide"


def route_after_claude(state: TradingState) -> str:
    decision = state.get("claude_decision") or {}
    return "log_no_trade" if decision.get("action", "veto") == "veto" else "risk_check"


def route_after_risk(state: TradingState) -> str:
    return "execute_log" if state.get("risk_approved") else "log_no_trade"


def build_graph(deps: Deps, checkpointer: Any = None):
    g = StateGraph(TradingState)
    for name, fn in make_nodes(deps).items():
        g.add_node(name, fn)

    g.set_entry_point("ingest")
    g.add_edge("ingest", "event_guard")
    g.add_conditional_edges("event_guard", route_after_event_guard,
                            {"claude_decide": "claude_decide", "log_no_trade": "log_no_trade"})
    g.add_conditional_edges("claude_decide", route_after_claude,
                            {"risk_check": "risk_check", "log_no_trade": "log_no_trade"})
    g.add_conditional_edges("risk_check", route_after_risk,
                            {"execute_log": "execute_log", "log_no_trade": "log_no_trade"})
    g.add_edge("execute_log", END)
    g.add_edge("log_no_trade", END)
    return g.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

class Pipeline:
    """Holds one compiled graph + checkpointer + store for the process."""

    def __init__(self, deps: Optional[Deps] = None):
        settings = deps.settings if deps else get_settings()
        self.deps = deps or Deps(settings=settings, store=Store(settings.db_path))
        settings.checkpoint_db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(settings.checkpoint_db_path, check_same_thread=False)
        # Checkpointing: a crashed mid-day run resumes instead of silently
        # missing a signal or double-firing one.
        self.app = build_graph(self.deps, SqliteSaver(conn))

    def run(
        self,
        signal: Signal,
        run_id: str,
        run_type: str = "live",
        as_of: Optional[datetime] = None,
        decision_mode: str = "claude",
    ) -> dict:
        existing = self.deps.store.get_run(run_id)
        if existing:
            log.info("run %s already completed (%s) - not re-running", run_id, existing["final_status"])
            return existing
        initial: TradingState = {
            "run_type": run_type,  # type: ignore[typeddict-item]
            "run_id": run_id,
            "as_of": (as_of or datetime.now(timezone.utc)).isoformat(),
            "raw_signal": signal.model_dump(),
            "decision_mode": decision_mode,  # type: ignore[typeddict-item]
        }
        config = {"configurable": {"thread_id": run_id}}
        snapshot = self.app.get_state(config)
        if snapshot.next:
            log.warning("run %s was interrupted before %s - resuming from checkpoint", run_id, snapshot.next)
            return self.app.invoke(None, config=config)
        return self.app.invoke(initial, config=config)
