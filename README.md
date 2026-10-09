# MCX Crude Agentic Paper Trading

GoCharting order-flow signal → DhanHQ depth confirmation → event guard → Claude judgement → hard risk limits → DhanHQ **Sandbox** paper order, orchestrated as a 5-node LangGraph.

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the architecture summary, phase tracker and design notes.

## Layout

```
.env.example                 every API key / token the system uses (copy to .env)
config/
  mcx_crude_contract.json    lot/tick, session hours, live windows, rolls, holidays
  event_calendar.json        manual OPEC/Fed/etc. blackouts
lipi/                        GoCharting Lipi signal script
src/mcx_agent/
  config.py                  settings + secrets (pydantic-settings, reads .env)
  state.py                   graph state + Signal / DepthSnapshot / ClaudeDecision
  signals.py                 webhook payload parser + Python port of the Lipi rule
  graph.py                   the 5-node LangGraph + Pipeline runner (checkpointed)
  risk.py                    hard-coded risk limits (never in .env or the LLM)
  store.py                   SQLite decision/outcome log
  webhook.py                 FastAPI GoCharting receiver
  scheduler.py               pre-market, EOD flatten, 2 backtests/day
  backtest.py                bar replay, simulated fills, Phase 5 metrics
  integrations/              dhan.py, claude.py, events.py (EIA, calendar, MCX session)
tests/
```

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env            # APP_MODE=mock needs no keys

pytest                          # unit + graph tests
python -m mcx_agent smoke       # one mock signal end to end
python -m mcx_agent backtest --synthetic --mode passthrough
python -m mcx_agent report --run-type backtest
```

Going to paper trading: fill in `.env` (Dhan sandbox + Data API, Anthropic, GoCharting secret, EIA), set `DHAN_CRUDEOIL_SECURITY_ID`, set `APP_MODE=paper`, then on the static-IP VPS:

```bash
python -m mcx_agent serve       # webhook on WEBHOOK_PORT + daily scheduler
```

Point the GoCharting alert webhook at `https://<vps>:<port>/webhook/gocharting/<GOCHARTING_WEBHOOK_SECRET>`. Put TLS in front of it with a reverse proxy such as Caddy or nginx.

## Safety rails

* No live mode. Orders go only to the Dhan sandbox host, and startup fails if `DHAN_SANDBOX_BASE_URL` isn't a sandbox URL.
* Risk limits are code constants in `risk.py`. Claude can only shrink position size, never grow it.
* Any Claude error, refusal or malformed output is treated as a **veto**.
* Each `run_id` executes at most once. Interrupted runs resume from the LangGraph checkpoint.
