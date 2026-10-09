"""DhanHQ integration: Data API (depth confirmation) + Sandbox (paper orders).

Uses DhanHQ v2 REST directly over httpx so the order host is pinned to the
sandbox URL from settings (validated in config.py). Endpoint/field names follow
the DhanHQ v2 docs - VERIFY against https://dhanhq.co/docs/v2/ before Phase 2
sign-off:

* POST {data}/marketfeed/quote   -> LTP + 5-level depth + total buy/sell qty
* POST {sandbox}/orders          -> place order
* GET  {sandbox}/positions       -> open positions (EOD flatten)

20-level depth is only available over Dhan's depth WebSocket; the REST quote
snapshot is the Phase 2 starting point. Swap `fetch_depth` for a WebSocket
consumer once the basic confirm-check is proven.

Alternatively, route orders through the Dhan MCP server / DhanHQ Claude Skill
(dhan-oss/dhanhq-skills) as the PDF suggests - keep `place_paper_order` as the
single choke point either way.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx

from ..config import Settings, get_contract
from ..state import DepthSnapshot, Side

log = logging.getLogger(__name__)


class DhanError(RuntimeError):
    pass


def resolve_security_id(settings: Settings, today_iso: str) -> str:
    """Prefer the contract table (handles rolls), fall back to .env."""
    for c in get_contract().get("contracts", []):
        if c.get("security_id") and c.get("expiry") and today_iso <= c["expiry"]:
            return str(c["security_id"])
    if settings.dhan_crudeoil_security_id:
        return settings.dhan_crudeoil_security_id
    if settings.is_mock:
        return "MOCK_CRUDEOIL"
    raise DhanError("No CRUDEOIL security id - set DHAN_CRUDEOIL_SECURITY_ID or config/mcx_crude_contract.json")


def _data_headers(settings: Settings) -> dict[str, str]:
    return {
        "access-token": settings.dhan_data_access_token.get_secret_value(),
        "client-id": settings.dhan_data_client_id or "",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _sandbox_headers(settings: Settings) -> dict[str, str]:
    return {
        "access-token": settings.dhan_sandbox_access_token.get_secret_value(),
        "client-id": settings.dhan_sandbox_client_id or "",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def fetch_depth(settings: Settings, security_id: str, side_hint: Side | None = None) -> DepthSnapshot:
    if settings.is_mock:
        # Deterministic mock that agrees with the hinted side, so the smoke
        # test exercises the full happy path.
        heavy, light = 1200.0, 900.0
        bid, ask = (heavy, light) if side_hint != "SHORT" else (light, heavy)
        return DepthSnapshot(
            security_id=security_id, ltp=6423.0, bid_qty=bid, ask_qty=ask,
            best_bid=6422.0, best_ask=6423.0, levels=5, source="mock",
        )

    segment = settings.dhan_exchange_segment
    url = f"{settings.dhan_data_base_url.rstrip('/')}/marketfeed/quote"
    try:
        resp = httpx.post(url, headers=_data_headers(settings), json={segment: [int(security_id)]}, timeout=5.0)
        resp.raise_for_status()
        quote = resp.json()["data"][segment][str(security_id)]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise DhanError(f"depth fetch failed: {e}") from e

    depth = quote.get("depth") or {}
    buys, sells = depth.get("buy") or [], depth.get("sell") or []
    bid_qty = float(quote.get("buy_quantity") or sum(l.get("quantity", 0) for l in buys))
    ask_qty = float(quote.get("sell_quantity") or sum(l.get("quantity", 0) for l in sells))
    return DepthSnapshot(
        security_id=str(security_id),
        ltp=float(quote["last_price"]),
        bid_qty=bid_qty,
        ask_qty=ask_qty,
        best_bid=float(buys[0]["price"]) if buys else None,
        best_ask=float(sells[0]["price"]) if sells else None,
        levels=max(len(buys), len(sells)),
        source="dhan",
    )


def place_paper_order(
    settings: Settings, security_id: str, side: Side, lots: int, limit_price: float, run_id: str
) -> dict[str, Any]:
    """Marketable LIMIT order on the Dhan SANDBOX. Never a live account."""
    if lots <= 0:
        raise DhanError("refusing to place an order with lots <= 0")
    order = {
        "dhanClientId": settings.dhan_sandbox_client_id,
        "correlationId": run_id[:25],
        "transactionType": "BUY" if side == "LONG" else "SELL",
        "exchangeSegment": settings.dhan_exchange_segment,
        "productType": "INTRADAY",
        "orderType": "LIMIT",
        "validity": "DAY",
        "securityId": str(security_id),
        # VERIFY: for MCX_COMM confirm whether Dhan expects quantity in lots or units.
        "quantity": lots,
        "price": limit_price,
    }
    if settings.is_mock:
        return {"status": "mock_filled", "orderId": f"mock-{uuid.uuid4().hex[:8]}", "request": order}

    base = settings.dhan_sandbox_base_url.rstrip("/")
    assert "sandbox" in base.lower(), "order host must be the Dhan sandbox"
    try:
        resp = httpx.post(f"{base}/orders", headers=_sandbox_headers(settings), json=order, timeout=10.0)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise DhanError(f"sandbox order failed: {e}") from e
    return {"status": "submitted", "response": resp.json(), "request": order}


def get_positions(settings: Settings) -> list[dict[str, Any]]:
    if settings.is_mock:
        return []
    base = settings.dhan_sandbox_base_url.rstrip("/")
    resp = httpx.get(f"{base}/positions", headers=_sandbox_headers(settings), timeout=10.0)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else data.get("data", [])
