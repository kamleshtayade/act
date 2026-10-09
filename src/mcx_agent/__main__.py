"""CLI entry point.

    python -m mcx_agent smoke                 # one mock signal through the graph
    python -m mcx_agent serve                 # webhook receiver + daily scheduler
    python -m mcx_agent backtest --csv bars.csv [--mode claude|passthrough]
    python -m mcx_agent backtest --synthetic  # random-walk bars, smoke only
    python -m mcx_agent report                # Phase 5 metrics from the log
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from .config import get_settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mcx_agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("smoke")
    sub.add_parser("serve")
    bt = sub.add_parser("backtest")
    bt.add_argument("--csv", type=Path)
    bt.add_argument("--synthetic", action="store_true")
    bt.add_argument("--mode", choices=["claude", "passthrough"], default="passthrough")
    bt.add_argument("--tag", default=None)
    rp = sub.add_parser("report")
    rp.add_argument("--run-type", choices=["live", "backtest"], default=None)
    args = parser.parse_args(argv)

    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from .graph import Pipeline

    if args.cmd == "smoke":
        from .integrations.events import IST
        from .state import Signal

        pipe = Pipeline()
        now = datetime.now(IST).replace(hour=10, minute=0, second=0, microsecond=0)
        while now.weekday() >= 5:  # land on a weekday inside the morning window
            now -= timedelta(days=1)
        sig = Signal(side="LONG", delta=42_000, cvd=150_000, source="manual")
        result = pipe.run(sig, f"smoke-{datetime.now():%Y%m%d%H%M%S}", "live", now)
        print(json.dumps({k: result.get(k) for k in (
            "run_id", "final_status", "no_trade_reason", "confirm_reason", "claude_decision",
            "approved_lots", "order_result")}, indent=2, default=str))

    elif args.cmd == "serve":
        import uvicorn

        from .scheduler import build_scheduler
        from .webhook import create_app

        pipe = Pipeline()
        build_scheduler(pipe).start()
        uvicorn.run(create_app(pipe), host=settings.webhook_host, port=settings.webhook_port)

    elif args.cmd == "backtest":
        from .backtest import load_bars, run_backtest, synthetic_bars

        if not (args.csv or args.synthetic):
            parser.error("backtest needs --csv or --synthetic")
        bars = synthetic_bars() if args.synthetic else load_bars(args.csv)
        tag = args.tag or datetime.now().strftime("%Y%m%d%H%M%S")
        print(json.dumps(run_backtest(bars, Pipeline(), tag, args.mode), indent=2))

    elif args.cmd == "report":
        from .backtest import evaluate
        from .store import Store

        rows = Store(settings.db_path).runs(args.run_type)
        print(json.dumps(evaluate(rows), indent=2))


if __name__ == "__main__":
    main()
