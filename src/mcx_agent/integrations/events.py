"""event_guard data sources: MCX session/holidays, EIA release window,
economic calendar. Returns a hard block reason, plus non-blocking context.

Deterministic on purpose - cheaper and safer than relying on the LLM to notice
event risk (PDF, section 3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

from ..config import Settings, get_contract, load_json

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

CONTEXT_LOOKAHEAD = timedelta(hours=4)


@dataclass
class Event:
    name: str
    at: datetime  # tz-aware
    blackout: timedelta


@dataclass
class EventCheck:
    block: Optional[str] = None
    context: list[str] = field(default_factory=list)


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def session_close_ist(day: date) -> time:
    """MCX evening close moves with US daylight saving."""
    sess = get_contract()["session"]
    noon_ny = datetime.combine(day, time(12), tzinfo=NY)
    return _hhmm(sess["close_us_dst"] if noon_ny.dst() else sess["close_us_standard"])


def holiday_name(day: date) -> Optional[str]:
    for h in get_contract().get("holidays", []):
        if h.get("date") == day.isoformat() and h.get("type", "full") == "full":
            return h.get("name", "MCX holiday")
    return None


def check_session(now_ist: datetime, run_type: str) -> Optional[str]:
    day = now_ist.date()
    if now_ist.weekday() >= 5:
        return "weekend - MCX closed"
    if name := holiday_name(day):
        return f"MCX holiday: {name}"
    contract = get_contract()
    open_t, close_t = _hhmm(contract["session"]["open"]), session_close_ist(day)
    t = now_ist.time()
    if not (open_t <= t < close_t):
        return f"outside MCX session ({open_t:%H:%M}-{close_t:%H:%M} IST)"
    flatten = (datetime.combine(day, close_t) - timedelta(minutes=contract["eod_flatten_minutes_before_close"])).time()
    if t >= flatten:
        return "inside end-of-day flatten window - no new entries"
    if run_type == "live":
        windows = contract.get("live_windows", [])
        if windows and not any(_hhmm(w["start"]) <= t < _hhmm(w["end"]) for w in windows):
            return "outside configured live trading windows"
    return None


def eia_release(day: date, blackout: timedelta) -> Optional[Event]:
    """EIA Weekly Petroleum Status Report: Wednesdays 10:30 ET. Holiday weeks
    shift it - add those manually to config/event_calendar.json."""
    if day.weekday() != 2:
        return None
    at = datetime.combine(day, time(10, 30), tzinfo=NY)
    return Event("EIA Weekly Petroleum Status Report", at, blackout)


def static_events(settings: Settings) -> list[Event]:
    cal = load_json(settings.event_calendar_file)
    default = timedelta(minutes=cal.get("default_blackout_minutes", 30))
    out = []
    for e in cal.get("events", []):
        if e.get("_example") or "time" not in e:
            continue
        mins = e.get("blackout_minutes")
        out.append(Event(e["name"], datetime.fromisoformat(e["time"]), timedelta(minutes=mins) if mins else default))
    return out


@lru_cache(maxsize=8)
def _fmp_events_for(day_iso: str, api_key: str) -> tuple[Event, ...]:
    """Financial Modeling Prep economic calendar (free tier). VERIFY the URL
    and field names against FMP's current docs before relying on it."""
    url = "https://financialmodelingprep.com/stable/economic-calendar"
    try:
        resp = httpx.get(url, params={"from": day_iso, "to": day_iso, "apikey": api_key}, timeout=10.0)
        resp.raise_for_status()
        rows = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("FMP calendar fetch failed (%s) - using static calendar only", e)
        return ()
    events = []
    for r in rows if isinstance(rows, list) else []:
        name = str(r.get("event", ""))
        high_impact_us = r.get("country") == "US" and str(r.get("impact", "")).lower() == "high"
        if high_impact_us or "opec" in name.lower():
            try:
                at = datetime.fromisoformat(str(r["date"])).replace(tzinfo=UTC)
            except (KeyError, ValueError):
                continue
            events.append(Event(f"{name} ({r.get('country')})", at, timedelta(minutes=30)))
    return tuple(events)


def check_event_risk(settings: Settings, now: datetime, run_type: str) -> EventCheck:
    now_ist = now.astimezone(IST)
    result = EventCheck(block=check_session(now_ist, run_type))
    if result.block:
        return result

    cal_default = timedelta(minutes=load_json(settings.event_calendar_file).get("default_blackout_minutes", 30))
    events = static_events(settings)
    if eia := eia_release(now.astimezone(NY).date(), cal_default):
        events.append(eia)
    if run_type == "live" and settings.fmp_api_key and not settings.is_mock:
        events.extend(_fmp_events_for(now_ist.date().isoformat(), settings.fmp_api_key.get_secret_value()))

    for ev in sorted(events, key=lambda e: e.at):
        if ev.at - ev.blackout <= now <= ev.at + ev.blackout:
            return EventCheck(block=f"event blackout: {ev.name} at {ev.at.astimezone(IST):%H:%M IST}")
        if now < ev.at <= now + CONTEXT_LOOKAHEAD:
            mins = int((ev.at - now).total_seconds() // 60)
            result.context.append(f"{ev.name} in {mins} min ({ev.at.astimezone(IST):%H:%M IST})")
    return result


def fetch_eia_snapshot(settings: Settings) -> dict[str, Any]:
    """Latest two weekly US commercial crude stock prints (series WCESTUS1),
    as macro context for Claude. VERIFY the route/facet against EIA API v2 docs."""
    if settings.is_mock or not settings.eia_api_key:
        return {}
    url = "https://api.eia.gov/v2/petroleum/stoc/wstk/data/"
    params = {
        "api_key": settings.eia_api_key.get_secret_value(),
        "frequency": "weekly",
        "data[0]": "value",
        "facets[series][]": "WCESTUS1",
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": 2,
    }
    try:
        resp = httpx.get(url, params=params, timeout=10.0)
        resp.raise_for_status()
        rows = resp.json()["response"]["data"]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("EIA fetch failed: %s", e)
        return {}
    if len(rows) < 2:
        return {}
    latest, prev = float(rows[0]["value"]), float(rows[1]["value"])
    return {
        "eia_week": rows[0]["period"],
        "us_crude_stocks_kbbl": latest,
        "weekly_change_kbbl": latest - prev,
    }
