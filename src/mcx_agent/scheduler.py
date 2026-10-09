"""Daily automation (Phase 4): pre-market check, EOD flatten, 2 backtests/day.

Live runs are event-driven (GoCharting webhook) and gated to the configured
live windows by event_guard - the scheduler doesn't poll for them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .backtest import load_bars, run_backtest
from .config import PROJECT_ROOT, get_contract
from .graph import Pipeline
from .integrations import dhan, events

log = logging.getLogger(__name__)
BACKTEST_CSV = PROJECT_ROOT / "data" / "backtest" / "latest.csv"


def premarket_check(pipeline: Pipeline) -> None:
    s = pipeline.deps.settings
    now = datetime.now(events.IST)
    if name := events.holiday_name(now.date()):
        log.warning("pre-market: MCX holiday today (%s)", name)
        return
    try:
        sec = dhan.resolve_security_id(s, now.date().isoformat())
        log.info("pre-market: CRUDEOIL security id %s", sec)
    except dhan.DhanError as e:
        log.error("pre-market: %s", e)
    open_at = now.replace(hour=9, minute=15)
    check = events.check_event_risk(s, max(now, open_at), "live")
    log.info("pre-market: block=%s upcoming=%s", check.block, check.context)


def eod_flatten(pipeline: Pipeline) -> None:
    """Square off any open sandbox CRUDEOIL position before the close."""
    s = pipeline.deps.settings
    now = datetime.now(events.IST)
    close_t = events.session_close_ist(now.date())
    close_dt = now.replace(hour=close_t.hour, minute=close_t.minute, second=0, microsecond=0)
    if not (close_dt - timedelta(minutes=get_contract()["eod_flatten_minutes_before_close"] + 5) <= now < close_dt):
        return  # the other flatten slot handles today's close time
    sec = dhan.resolve_security_id(s, now.date().isoformat())
    for pos in dhan.get_positions(s):
        # VERIFY Dhan v2 positions field names (netQty / securityId).
        qty = int(pos.get("netQty", 0))
        if str(pos.get("securityId")) != sec or qty == 0:
            continue
        depth = dhan.fetch_depth(s, sec)
        side = "SHORT" if qty > 0 else "LONG"
        px = (depth.best_bid if side == "SHORT" else depth.best_ask) or depth.ltp
        res = dhan.place_paper_order(s, sec, side, abs(qty), px, f"eod-{now:%Y%m%d%H%M}")
        log.info("EOD flatten %s %s @ %s -> %s", side, abs(qty), px, res.get("status"))


def scheduled_backtest(pipeline: Pipeline, slot: str, csv_path: Path = BACKTEST_CSV) -> None:
    if not csv_path.exists():
        log.warning("backtest %s skipped - %s not found", slot, csv_path)
        return
    bars = load_bars(csv_path)
    tag = f"{datetime.now(events.IST):%Y%m%d}{slot}"
    # Run Claude and the no-LLM baseline side by side - Phase 5 asks whether
    # Claude's overrides add value or just noise.
    for mode in ("claude", "passthrough"):
        try:
            log.info("backtest %s/%s: %s", slot, mode, run_backtest(bars, pipeline, tag, mode))
        except ValueError as e:
            log.warning("backtest %s/%s skipped: %s", slot, mode, e)


def build_scheduler(pipeline: Pipeline) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="Asia/Kolkata")
    wk = "mon-fri"
    sched.add_job(premarket_check, CronTrigger(day_of_week=wk, hour=8, minute=40), args=[pipeline], id="premarket")
    # Two slots cover both the US-DST and US-standard close times.
    sched.add_job(eod_flatten, CronTrigger(day_of_week=wk, hour=23, minute=12), args=[pipeline], id="eod_dst")
    sched.add_job(eod_flatten, CronTrigger(day_of_week=wk, hour=23, minute=37), args=[pipeline], id="eod_std")
    sched.add_job(scheduled_backtest, CronTrigger(hour=7, minute=10), args=[pipeline, "am"], id="bt_am")
    sched.add_job(scheduled_backtest, CronTrigger(hour=12, minute=40), args=[pipeline, "pm"], id="bt_pm")
    return sched
