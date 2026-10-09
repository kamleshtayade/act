"""claude_decide integration - Claude reviews a deterministic signal and
returns confirm / veto / adjust with a rationale.

Claude never computes signal math or risk limits. It only judges context.
Any failure (API error, refusal, bad output) fails CLOSED as a veto.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any, Optional

import anthropic

from ..config import Settings
from ..state import ClaudeDecision

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are the judgement layer of a paper-trading system for MCX Crude Oil futures.

A deterministic order-flow rule (bar delta imbalance confirmed by cumulative
volume delta trend) has already fired, and the broker's own order book has
independently agreed with its direction. Hard risk limits (position size,
daily loss, trades per day) are enforced by code after you; you cannot raise
size above the allowed maximum.

Your job: decide whether the surrounding context makes this specific signal
worth taking.
- confirm: context supports the trade, take full allowed size (size_multiplier 1.0).
- adjust: tradeable but with reservations, take reduced size (0 < size_multiplier < 1).
- veto: context makes the signal unreliable (size_multiplier 0.0).

Things that matter for MCX crude: proximity to the EIA weekly inventory release
and other scheduled macro events, thin liquidity outside active windows, a
weak or lopsided order book, a delta spike that disagrees with the broader CVD
trend, and recent losing trades today. When the evidence is mixed, prefer
adjust over confirm. Do not invent data you were not given.
"""


@lru_cache(maxsize=1)
def _client(api_key: str, timeout: float) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)


def _veto(reason: str) -> dict[str, Any]:
    return ClaudeDecision(
        action="veto", size_multiplier=0.0, rationale=reason, risk_flags=["claude_unavailable"]
    ).model_dump()


def call_claude_decide(settings: Settings, context: dict[str, Any]) -> dict[str, Any]:
    if settings.is_mock:
        return ClaudeDecision(
            action="confirm", size_multiplier=1.0, rationale="mock mode - no Claude call made"
        ).model_dump()

    client = _client(settings.anthropic_api_key.get_secret_value(), settings.claude_timeout_seconds)
    extra_headers: Optional[dict[str, str]] = None
    extra_body: Optional[dict[str, Any]] = None
    if settings.claude_enable_server_fallback:
        # Server-side refusal fallback: if the model declines, the API reroutes
        # the same request instead of returning an empty decision.
        extra_headers = {"anthropic-beta": "server-side-fallback-2026-07-01"}
        extra_body = {"fallbacks": "default"}

    try:
        response = client.messages.parse(
            model=settings.claude_model,
            max_tokens=settings.claude_max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(context, indent=2, default=str)}],
            output_format=ClaudeDecision,
            output_config={"effort": settings.claude_effort},
            extra_headers=extra_headers,
            extra_body=extra_body,
        )
    except anthropic.RateLimitError as e:
        log.warning("Claude rate limited: %s", e)
        return _veto("Claude rate limited - failing closed")
    except anthropic.APIStatusError as e:
        log.error("Claude API error %s: %s", e.status_code, e)
        return _veto(f"Claude API error {e.status_code} - failing closed")
    except anthropic.APIConnectionError as e:
        log.error("Claude connection error: %s", e)
        return _veto("Claude unreachable - failing closed")

    if response.stop_reason == "refusal":
        return _veto("Claude declined to evaluate - failing closed")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        return _veto("Claude output incomplete - failing closed")

    decision: ClaudeDecision = response.parsed_output
    # Normalise: the schema can't enforce numeric bounds, so clamp here.
    mult = min(max(decision.size_multiplier, 0.0), 1.0)
    if decision.action == "veto":
        mult = 0.0
    elif decision.action == "confirm":
        mult = 1.0
    out = decision.model_copy(update={"size_multiplier": mult}).model_dump()
    out["model"] = response.model
    out["usage"] = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
    return out
