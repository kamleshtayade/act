"""
LangGraph skeleton - MCX Crude Oil order-flow paper trading pipeline.

5 nodes, no cycles, one conditional exit shared by three nodes:

    ingest -> event_guard -> claude_decide -> risk_check -> execute_log
                  |                |               |
                  v                v               v
              log_no_trade   log_no_trade    log_no_trade

This is a SCAFFOLD: every call to Dhan, GoCharting, or Claude is stubbed
with a clearly marked function you fill in. The graph wiring, state
shape, and conditional routing are complete and runnable as-is (the
stubs just return mock data so you can smoke-test the graph shape
before wiring real APIs).

Install:
    pip install langgraph langgraph-checkpoint-sqlite anthropic dhanhq --break-system-packages
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Literal, Optional, TypedDict

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver

# ---------------------------------------------------------------------------
# 1. STATE SCHEMA
# ---------------------------------------------------------------------------

class TradingState(TypedDict, total=False):
    run_type: Literal["live", "backtest"]
    run_id: str

    # populated by ingest_node
    raw_signal: dict           # payload from GoCharting webhook (live) or a
                                # historical bar (backtest)
    confirmed_depth: dict      # DhanHQ depth/tick snapshot used to corroborate
                                # the GoCharting signal before trusting it
    signal_confirmed: bool

    # populated by event_guard_node
    event_block: Optional[str] # reason string if blocked, else None

    # populated by claude_decide_node
    claude_decision: Optional[dict]  # {"action": "confirm"|"veto"|"adjust",
                                      #  "size_multiplier": float,
                                      #  "rationale": str}

    # populated by risk_check_node
    risk_approved: bool
    risk_reason: Optional[str]

    # populated by execute_log_node / log_no_trade_node
    order_result: Optional[dict]
    final_status: str          # "executed" | "no_trade"


# ---------------------------------------------------------------------------
# 2. STUBS - replace these with real integrations
# ---------------------------------------------------------------------------

def fetch_dhan_depth(security_id: str) -> dict:
    """STUB: pull current 20-level depth / tick snapshot from DhanHQ Data API
    to independently confirm the GoCharting webhook signal.
    Wire this to the dhanhq SDK or your Dhan MCP client.
    """
    return {"security_id": security_id, "bid_vol": 120, "ask_vol": 95, "ltp": 6423.0}


def check_event_risk(now: datetime) -> Optional[str]:
    """STUB: check EIA inventory release schedule, economic calendar
    (OPEC/Fed), and MCX holiday list. Return a block reason, or None if clear.
    """
    # e.g. query a cached EIA/calendar JSON you refresh daily
    return None


def call_claude_decide(signal: dict, depth: dict, event_context: Optional[str]) -> dict:
    """STUB: send the order-flow signal + macro context to Claude and parse
    a structured decision back. Replace with a real Anthropic API call.

    Example real call:
        from anthropic import Anthropic
        client = Anthropic()
        resp = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=500,
            system=CLAUDE_DECIDE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps({
                "signal": signal, "depth": depth, "event_context": event_context,
            })}],
        )
        # parse resp into the dict shape below - use tool_use / forced JSON
        # output in production rather than free-text parsing.
    """
    return {"action": "confirm", "size_multiplier": 1.0, "rationale": "stub decision"}


def get_daily_pnl(run_date: str) -> float:
    """STUB: read today's realized + unrealized paper P&L from the log store."""
    return 0.0


def get_trade_count(run_date: str) -> int:
    """STUB: count trades already placed today, from the log store."""
    return 0


def place_paper_order(signal: dict, size_multiplier: float) -> dict:
    """STUB: place the order via the Dhan MCP server / DhanHQ Claude Skill,
    pointed at the Sandbox endpoint. Never point this at a live account
    during the paper-trading phase.
    """
    return {"status": "filled", "side": signal.get("side"), "qty": 1 * size_multiplier}


def simulate_fill(signal: dict, size_multiplier: float) -> dict:
    """STUB: for backtest runs - simulate a fill against historical price data
    instead of calling the live Sandbox API."""
    return {"status": "simulated_fill", "side": signal.get("side"), "qty": 1 * size_multiplier}


def persist_log(state: TradingState) -> None:
    """STUB: write decision + rationale + outcome to SQLite/Parquet for the
    evaluation loop (Phase 5 of the implementation plan)."""
    print(f"[LOG] run={state.get('run_id')} status={state.get('final_status')} "
          f"decision={state.get('claude_decision')} order={state.get('order_result')}")


# ---------------------------------------------------------------------------
# 3. NODES
# ---------------------------------------------------------------------------

# Hard-coded risk limits - NEVER move these into claude_decide_node.
MAX_DAILY_LOSS = -5000.0     # paper rupees
MAX_TRADES_PER_DAY = 3       # matches the "3 live executions/day" cadence


def ingest_node(state: TradingState) -> TradingState:
    security_id = state["raw_signal"].get("security_id", "MCX_CRUDEOIL")
    depth = fetch_dhan_depth(security_id)
    state["confirmed_depth"] = depth
    # minimal confirmation check: webhook signal direction should agree with
    # the sign of current bid/ask imbalance - replace with your real rule.
    imbalance = depth["bid_vol"] - depth["ask_vol"]
    side = state["raw_signal"].get("side")
    state["signal_confirmed"] = (imbalance > 0 and side == "LONG") or (imbalance < 0 and side == "SHORT")
    return state


def event_guard_node(state: TradingState) -> TradingState:
    state["event_block"] = check_event_risk(datetime.now(timezone.utc))
    return state


def route_after_event_guard(state: TradingState) -> str:
    if state.get("event_block") or not state.get("signal_confirmed", False):
        return "log_no_trade"
    return "claude_decide"


def claude_decide_node(state: TradingState) -> TradingState:
    decision = call_claude_decide(
        state["raw_signal"], state["confirmed_depth"], state.get("event_block")
    )
    state["claude_decision"] = decision
    return state


def route_after_claude(state: TradingState) -> str:
    decision = state.get("claude_decision") or {}
    if decision.get("action") == "veto":
        return "log_no_trade"
    return "risk_check"


def risk_check_node(state: TradingState) -> TradingState:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_pnl = get_daily_pnl(today)
    trade_count = get_trade_count(today)

    if daily_pnl <= MAX_DAILY_LOSS:
        state["risk_approved"] = False
        state["risk_reason"] = f"daily loss limit breached ({daily_pnl})"
    elif trade_count >= MAX_TRADES_PER_DAY:
        state["risk_approved"] = False
        state["risk_reason"] = f"max trades/day reached ({trade_count})"
    else:
        state["risk_approved"] = True
        state["risk_reason"] = None
    return state


def route_after_risk(state: TradingState) -> str:
    return "execute_log" if state.get("risk_approved") else "log_no_trade"


def execute_log_node(state: TradingState) -> TradingState:
    decision = state["claude_decision"]
    size_multiplier = decision.get("size_multiplier", 1.0)

    if state["run_type"] == "live":
        result = place_paper_order(state["raw_signal"], size_multiplier)
    else:
        result = simulate_fill(state["raw_signal"], size_multiplier)

    state["order_result"] = result
    state["final_status"] = "executed"
    persist_log(state)
    return state


def log_no_trade_node(state: TradingState) -> TradingState:
    state["order_result"] = None
    state["final_status"] = "no_trade"
    persist_log(state)
    return state


# ---------------------------------------------------------------------------
# 4. GRAPH WIRING
# ---------------------------------------------------------------------------

def build_graph():
    graph = StateGraph(TradingState)

    graph.add_node("ingest", ingest_node)
    graph.add_node("event_guard", event_guard_node)
    graph.add_node("claude_decide", claude_decide_node)
    graph.add_node("risk_check", risk_check_node)
    graph.add_node("execute_log", execute_log_node)
    graph.add_node("log_no_trade", log_no_trade_node)

    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "event_guard")

    graph.add_conditional_edges(
        "event_guard", route_after_event_guard,
        {"claude_decide": "claude_decide", "log_no_trade": "log_no_trade"},
    )
    graph.add_conditional_edges(
        "claude_decide", route_after_claude,
        {"risk_check": "risk_check", "log_no_trade": "log_no_trade"},
    )
    graph.add_conditional_edges(
        "risk_check", route_after_risk,
        {"execute_log": "execute_log", "log_no_trade": "log_no_trade"},
    )

    graph.add_edge("execute_log", END)
    graph.add_edge("log_no_trade", END)

    # Checkpointing: a crashed mid-day run can resume instead of silently
    # missing a signal or double-firing one.
    conn = sqlite3.connect("checkpoints.db", check_same_thread=False)
    checkpointer = SqliteSaver(conn)

    return graph.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# 5. ENTRY POINTS - one per execution type
# ---------------------------------------------------------------------------

def run_live(webhook_payload: dict, run_id: str) -> TradingState:
    """Call this from your FastAPI webhook receiver when GoCharting's alert
    fires. One call per live execution (you're running 3/day)."""
    app = build_graph()
    initial_state: TradingState = {"run_type": "live", "run_id": run_id, "raw_signal": webhook_payload}
    config = {"configurable": {"thread_id": run_id}}
    return app.invoke(initial_state, config=config)


def run_backtest(historical_bar: dict, run_id: str) -> TradingState:
    """Call this from your scheduler for the 2 backtest runs/day, feeding in
    historical bars instead of a live webhook payload."""
    app = build_graph()
    initial_state: TradingState = {"run_type": "backtest", "run_id": run_id, "raw_signal": historical_bar}
    config = {"configurable": {"thread_id": run_id}}
    return app.invoke(initial_state, config=config)


if __name__ == "__main__":
    # Smoke test with mock data - verifies the graph shape before you wire
    # real Dhan/GoCharting/Claude calls into the stubs above.
    mock_signal = {"security_id": "MCX_CRUDEOIL", "side": "LONG", "delta": 42000}
    result = run_live(mock_signal, run_id="smoke-test-001")
    print("Final state:", result)
