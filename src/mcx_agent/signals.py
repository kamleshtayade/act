"""Signal layer helpers.

* `parse_gocharting_alert` - turns whatever GoCharting POSTs into a `Signal`.
* `lipi_delta_imbalance_rule` - a Python port of
  `lipi/mcx_crude_delta_imbalance_signal.lipi`, used by the backtest runner so
  live and backtest runs share one rule definition.

The Lipi script emits two message shapes (see its alertcondition()/alert()):

    LONG|delta_imbalance|crudeoil
    LONG|delta=42000|cvd=123456
"""

from __future__ import annotations

import json
from typing import Any, Optional, Sequence

from .state import Signal

# Keep in sync with the Lipi script's input() defaults.
LIPI_DELTA_THRESHOLD = 40_000
LIPI_CVD_LOOKBACK = 20


class SignalParseError(ValueError):
    pass


def _parse_pipe_message(text: str) -> dict[str, Any]:
    parts = [p.strip() for p in text.strip().split("|") if p.strip()]
    if not parts:
        raise SignalParseError("empty alert message")
    side = parts[0].upper()
    if side not in ("LONG", "SHORT"):
        raise SignalParseError(f"unknown side {parts[0]!r}")
    out: dict[str, Any] = {"side": side}
    for part in parts[1:]:
        if "=" in part:
            key, _, value = part.partition("=")
            key = key.strip().lower()
            if key in ("delta", "cvd", "price"):
                try:
                    out[key] = float(value)
                except ValueError as e:
                    raise SignalParseError(f"bad number for {key}: {value!r}") from e
        elif part.lower() == "crudeoil":
            out["symbol"] = "CRUDEOIL"
    return out


def parse_gocharting_alert(body: bytes | str) -> Signal:
    """Accepts a raw pipe-delimited message, or JSON that either carries the
    fields directly ({"side": ...}) or wraps the message ({"message": ...}).
    GoCharting's exact webhook body format should be confirmed by logging a few
    real payloads (Phase 1) - this parser is intentionally lenient."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    text = text.strip()
    fields: dict[str, Any]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, dict):
        if "side" in payload:
            fields = {k: payload[k] for k in ("side", "delta", "cvd", "price", "symbol") if k in payload}
            fields["side"] = str(fields["side"]).upper()
        else:
            msg = payload.get("message") or payload.get("text") or payload.get("alert")
            if not isinstance(msg, str):
                raise SignalParseError("JSON payload has no side/message field")
            fields = _parse_pipe_message(msg)
    elif isinstance(payload, str):
        fields = _parse_pipe_message(payload)
    else:
        fields = _parse_pipe_message(text)

    return Signal(source="gocharting", raw=text, **fields)


def lipi_delta_imbalance_rule(
    deltas: Sequence[float],
    cvds: Sequence[float],
    i: int,
    threshold: float = LIPI_DELTA_THRESHOLD,
    lookback: int = LIPI_CVD_LOOKBACK,
) -> Optional[str]:
    """Return "LONG", "SHORT" or None for bar `i`, mirroring the Lipi script:

        longSignal  = delta >= threshold  and cvd >  cvd[lookback]
        shortSignal = delta <= -threshold and not (cvd > cvd[lookback])
    """
    if i < lookback:
        return None  # cvd[lookback] undefined - Lipi would yield na
    delta, cvd, cvd_ref = deltas[i], cvds[i], cvds[i - lookback]
    cvd_rising = cvd > cvd_ref
    if delta >= threshold and cvd_rising:
        return "LONG"
    if delta <= -threshold and not cvd_rising:
        return "SHORT"
    return None
