from mcx_agent.state import DepthSnapshot, Signal

from .conftest import TUESDAY_10AM

LONG = Signal(side="LONG", delta=42_000, cvd=10)


def test_happy_path_executes(make_pipeline, store):
    out = make_pipeline().run(LONG, "r1", "live", TUESDAY_10AM)
    assert out["final_status"] == "executed"
    assert out["approved_lots"] == 1
    assert store.get_run("r1")["final_status"] == "executed"


def test_depth_disagreement_blocks(make_pipeline):
    def bearish_book(settings, sec_id, side_hint=None):
        return DepthSnapshot(security_id=sec_id, ltp=1, bid_qty=500, ask_qty=1500, source="mock")

    out = make_pipeline(fetch_depth=bearish_book).run(LONG, "r2", "live", TUESDAY_10AM)
    assert out["final_status"] == "no_trade"
    assert out["no_trade_reason"].startswith("ingest")
    assert "claude_decision" not in out  # Claude never called


def test_claude_veto_blocks(make_pipeline):
    calls = []

    def veto(settings, ctx):
        calls.append(ctx)
        return {"action": "veto", "size_multiplier": 0.0, "rationale": "EIA soon", "risk_flags": []}

    out = make_pipeline(decide=veto).run(LONG, "r3", "live", TUESDAY_10AM)
    assert out["final_status"] == "no_trade" and "claude veto" in out["no_trade_reason"]
    assert calls[0]["signal"]["side"] == "LONG" and "today" in calls[0]


def test_event_block_skips_claude(make_pipeline):
    def boom(settings, ctx):
        raise AssertionError("Claude must not be called when event_guard blocks")

    sat = TUESDAY_10AM.replace(day=10)
    out = make_pipeline(decide=boom).run(LONG, "r4", "live", sat)
    assert out["no_trade_reason"].startswith("event_guard")


def test_max_trades_per_day(make_pipeline):
    pipe = make_pipeline()
    statuses = [pipe.run(LONG, f"t{i}", "live", TUESDAY_10AM.replace(minute=i))["final_status"] for i in range(4)]
    assert statuses == ["executed"] * 3 + ["no_trade"]


def test_run_id_is_idempotent(make_pipeline, store):
    pipe = make_pipeline()
    pipe.run(LONG, "dup", "live", TUESDAY_10AM)
    pipe.run(LONG, "dup", "live", TUESDAY_10AM)
    assert store.trade_count("2026-10-06", "live") == 1


def test_backtest_counts_are_separate_from_live(make_pipeline, store):
    pipe = make_pipeline()
    for i in range(3):
        pipe.run(LONG, f"live{i}", "live", TUESDAY_10AM.replace(minute=i))
    bt = Signal(side="LONG", delta=42_000, cvd=10, price=6400, source="backtest")
    out = pipe.run(bt, "bt1", "backtest", TUESDAY_10AM.replace(minute=30), "passthrough")
    assert out["final_status"] == "executed"
    assert out["order_result"]["fill_price"] == 6401  # 1 tick slippage
