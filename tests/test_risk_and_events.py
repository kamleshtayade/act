from datetime import datetime

from mcx_agent import risk
from mcx_agent.integrations.events import IST, check_event_risk

from .conftest import TUESDAY_10AM


def test_risk_limits():
    assert risk.evaluate(0, 0, 1.0) == risk.RiskVerdict(True, 1, None)
    assert not risk.evaluate(risk.MAX_DAILY_LOSS_INR, 0, 1.0).approved
    assert not risk.evaluate(0, risk.MAX_TRADES_PER_DAY, 1.0).approved
    # Claude can't scale above the cap
    assert risk.evaluate(0, 0, 5.0).lots == risk.MAX_LOTS_PER_TRADE
    assert not risk.evaluate(0, 0, 0.0).approved


def test_clear_tuesday_morning(settings):
    assert check_event_risk(settings, TUESDAY_10AM, "live").block is None


def test_weekend_blocked(settings):
    sat = datetime(2026, 10, 10, 10, 0, tzinfo=IST)
    assert "weekend" in check_event_risk(settings, sat, "live").block


def test_outside_live_window_blocks_live_only(settings):
    t = datetime(2026, 10, 6, 12, 30, tzinfo=IST)  # between windows
    assert "live trading windows" in check_event_risk(settings, t, "live").block
    assert check_event_risk(settings, t, "backtest").block is None


def test_eia_wednesday_blackout_and_context(settings):
    # 2026-10-07 is a Wednesday; US on DST -> 10:30 ET == 20:00 IST
    during = datetime(2026, 10, 7, 20, 10, tzinfo=IST)
    assert "EIA" in check_event_risk(settings, during, "live").block
    before = datetime(2026, 10, 7, 18, 45, tzinfo=IST)
    res = check_event_risk(settings, before, "live")
    assert res.block is None and any("EIA" in c for c in res.context)


def test_eod_flatten_window(settings):
    late = datetime(2026, 10, 6, 23, 20, tzinfo=IST)  # close 23:30 during US DST
    assert "flatten" in check_event_risk(settings, late, "backtest").block
