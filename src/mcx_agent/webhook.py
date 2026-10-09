"""FastAPI receiver for GoCharting alert webhooks (Phase 1 + live runs).

GoCharting's Alert Widget can't add auth headers, so the shared secret lives
in the URL path:  POST /webhook/gocharting/{secret}

Every payload is appended to data/webhook_payloads.jsonl before parsing, so
Phase 1 can validate delivery reliability/latency and the real body format.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from .config import PROJECT_ROOT, get_settings
from .graph import Pipeline
from .signals import SignalParseError, parse_gocharting_alert

log = logging.getLogger(__name__)
PAYLOAD_LOG = PROJECT_ROOT / "data" / "webhook_payloads.jsonl"


def create_app(pipeline: Pipeline | None = None) -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="MCX Crude webhook receiver")
    state: dict = {"pipeline": pipeline, "last_accepted": {}}

    def get_pipeline() -> Pipeline:
        if state["pipeline"] is None:
            state["pipeline"] = Pipeline()
        return state["pipeline"]

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "mode": settings.app_mode}

    @app.post("/webhook/gocharting/{secret}")
    async def gocharting(secret: str, request: Request, background: BackgroundTasks) -> dict:
        expected = settings.gocharting_webhook_secret
        if expected is None and not settings.is_mock:
            raise HTTPException(503, "webhook secret not configured")
        if expected is not None and not secrets.compare_digest(secret, expected.get_secret_value()):
            raise HTTPException(404)

        body = await request.body()
        received_at = datetime.now(timezone.utc)
        PAYLOAD_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(PAYLOAD_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"received_at": received_at.isoformat(), "body": body.decode("utf-8", "replace")}) + "\n")

        try:
            signal = parse_gocharting_alert(body)
        except SignalParseError as e:
            log.warning("unparseable alert: %s", e)
            return {"accepted": False, "reason": str(e)}

        pipe = get_pipeline()
        # In-memory check catches back-to-back duplicates before the first run
        # has been logged; the store check survives process restarts.
        cooldown = timedelta(seconds=settings.signal_cooldown_seconds)
        last = max(filter(None, [state["last_accepted"].get(signal.side),
                                 pipe.deps.store.last_signal_time(signal.side)]), default=None)
        if last and received_at - last < cooldown:
            return {"accepted": False, "reason": f"duplicate {signal.side} within cooldown"}
        state["last_accepted"][signal.side] = received_at

        run_id = f"live-{received_at:%Y%m%dT%H%M%S}-{signal.side}-{uuid.uuid4().hex[:6]}"
        # Return fast - GoCharting shouldn't wait on Dhan + Claude round trips.
        background.add_task(pipe.run, signal, run_id, "live", received_at)
        return {"accepted": True, "run_id": run_id}

    return app
