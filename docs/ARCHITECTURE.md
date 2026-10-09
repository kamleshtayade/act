# Agentic MCX Crude Oil Order-Flow System: Architecture Summary

Working summary of *mcx-crude-agentic-paper-trading-architecture.pdf*, kept next to the code. Update the **Implementation tracker** as phases land.

**Scope:** daily **paper** trading of MCX CRUDEOIL futures: **3 live + 2 backtest runs per day**. The plan stops at the DhanHQ **Sandbox**. No live capital is used.

---

## 1. Component roles

| Component | Role | Where in code |
|---|---|---|
| **GoCharting (Lipi)** | Computes order-flow metrics natively (footprint, delta, CVD, bid/ask imbalance). Fires a webhook when the signal condition hits. | `lipi/mcx_crude_delta_imbalance_signal.lipi` |
| **Webhook receiver** | FastAPI endpoint on a static-IP VPS. Receives GoCharting alerts. | `src/mcx_agent/webhook.py` |
| **DhanHQ Data API** | Depth/tick feed (20-level depth), historical OHLC, option chain. Paid tier, about Rs 399-499/mo. Used to **independently confirm** the webhook signal. | `integrations/dhan.py::fetch_depth` |
| **DhanHQ Sandbox** | Simulated broker for paper orders, fills and positions. | `integrations/dhan.py::place_paper_order` |
| **Dhan MCP Server / DhanHQ Claude Skill** | Exposes Dhan orders and data as MCP tools, with guardrails such as lot-size validation and limit-order defaults. | Optional alternative behind `place_paper_order` |
| **Claude API** | Runtime reasoning layer. Returns **confirm / veto / adjust-size** plus a rationale. Never computes signal math or risk limits. | `integrations/claude.py` |
| **LangGraph** | Orchestrates a small checkpointed graph with 5 nodes. | `src/mcx_agent/graph.py` |
| **Event guard sources** | EIA inventory schedule, economic calendar (OPEC/Fed), MCX holiday list. | `integrations/events.py`, `config/*.json` |
| **Claude Code** | Dev-time tool for building and iterating. Not part of the runtime. | n/a |

## 2. System flow

```
Scheduler (APScheduler, IST)
 ├─ pre-market check ──────────────► event guard / instrument sanity
 ├─ 2x backtest/day ───────────────► Backtest runner ─┐
 └─ EOD flatten ───────────────────► Dhan Sandbox     │
                                                      │ same graph
GoCharting Lipi ──alert webhook──► Webhook receiver ──► LangGraph ──► Dhan Sandbox (paper)
DhanHQ Data API ──confirm depth──────────────────────► (ingest)          │
Macro/Event guard ──event flags──────────────────────► (event_guard)     ▼
                                                                 Decision & outcome log (SQLite)
```

* Treat the GoCharting webhook as **a trigger to verify, not a trusted order**.
* Orders only ever go to the **Sandbox**. `config.py` refuses to start if the order host isn't the sandbox, and there is no `live` mode.
* The backtest runner reuses the same signal rule and `claude_decide` logic and writes to the same log store.

## 3. LangGraph design: 5 nodes, no cycles, one shared exit

```
ingest → event_guard → claude_decide → risk_check → execute_log → END
             │               │              │
             └───────────────┴──────────────┴──► log_no_trade → END
```

| # | Node | Purpose | Why it's a separate node |
|---|---|---|---|
| 1 | `ingest` | Takes the webhook payload, confirms it against Dhan depth, assembles state. | Keeps signal detection and verification together as one traceable step. |
| 2 | `event_guard` | EIA window, economic calendar, MCX holidays/session. Can short-circuit. | A hard gate before Claude sees anything. Cheaper and safer than relying on the LLM. |
| 3 | `claude_decide` | Claude: confirm / veto / adjust + rationale. | The only judgement step, so its contribution can be evaluated on its own. |
| 4 | `risk_check` | Hard-coded sizing, max daily loss, max trades/day. Can short-circuit. | Risk must never depend on an LLM call succeeding. |
| 5 | `execute_log` | Paper order (live) or `simulate_fill` (backtest), then log. | Deterministic once risk has passed. |

Backtesting reuses nodes 1-3 against historical data and swaps node 5 for a simulated fill. It uses the same graph, not a new one.

## 4. Pre-requisites checklist

- [ ] DhanHQ developer account, then Sandbox client ID + access token → `.env`
- [ ] DhanHQ Data API subscription (depth, historical, option chain) → `.env`
- [ ] Instrument master CSV: CRUDEOIL current-month security ID and roll schedule → `.env` / `config/mcx_crude_contract.json`
- [ ] Static-IP VPS (Dhan order endpoints, webhook host)
- [ ] GoCharting India plan with **real-time** order flow (the free tier is EOD/delayed)
- [ ] Lipi script backtested in GoCharting, with an alert pointed at the webhook URL
- [ ] Dhan MCP server and DhanHQ Claude Skill (optional; `npx -y skills add dhan-oss/dhanhq-skills --skill dhanhq --agent claude-code`). Review the wrapper scripts before use.
- [ ] Anthropic API key (runtime) → `.env`
- [ ] EIA API key (free) → `.env`
- [ ] Economic calendar source (FMP / Trading Economics free tier) → `.env` (optional)
- [ ] MCX holiday and contract-spec JSON → `config/mcx_crude_contract.json`
- [ ] Python 3.10+, langgraph, fastapi/uvicorn, SQLite → `pyproject.toml`

## 5. Implementation plan and tracker

| Phase | Days | Deliverable | Status |
|---|---|---|---|
| **0** Accounts & access | 1-2 | Dhan sandbox + Data API, GoCharting plan, VPS, EIA key | ⬜ user action (`.env.example` lists every key) |
| **1** Signal layer | 3-6 | Lipi rule backtested in GoCharting; alert → webhook; FastAPI receiver logging every payload | 🟨 receiver + payload log + parser done; Lipi needs in-editor verification |
| **2** Dhan integration | 7-10 | Sandbox order place/modify/cancel verified; ingest confirm-check from Data API | 🟨 REST quote confirm + sandbox order written; **verify endpoints against live sandbox**; 20-level WebSocket depth TODO |
| **3** LangGraph orchestrator | 11-15 | 5-node graph, checkpointing, hard-coded risk | ✅ done in mock mode, with tests |
| **4** Daily automation | 16-18 | Pre-market check, live windows, EOD flatten, 2 backtests/day, P&L circuit breaker | 🟨 scheduler jobs written; unrealized P&L for the circuit breaker TODO |
| **5** Evaluation loop | 19-21+ | Log every decision, tag win/loss/no-trade, compare Claude vs. deterministic, 4-6 weeks paper | 🟨 log schema + `report` metrics + Claude-vs-passthrough backtests |

## 6. Monthly cost estimate (planning only; confirm with each vendor)

| Item | Rs / month |
|---|---|
| DhanHQ Data API | 400-500 |
| DhanHQ Sandbox | free |
| GoCharting order-flow tier | 500-650 |
| Static-IP VPS | 400-800 |
| Claude API (runtime) | 300-500 |
| EIA / calendar / MCX JSON | free |
| **Total** | **about 1,600-2,450 (~$19-29)**, excluding an optional Claude Code subscription |

## 7. Key caveats

* Order flow needs **tick / full-depth** data, not OHLC. Confirm that both the Dhan WebSocket mode and the GoCharting plan deliver it.
* The webhook is a trigger to verify. The ingest confirm-check exists for this reason.
* Claude only judges ambiguous context. Code owns the numeric thresholds and risk limits.
* MCX crude is thin outside its active windows and reacts strongly to news (EIA, geopolitics), so `event_guard` matters more here than it would for equities.
* Run at least **4-6 weeks** of paper trading before any discussion of live capital.

---

## 8. Mapping and review notes (Lipi script + LangGraph skeleton)

### What changed versus `docs/reference_langgraph_skeleton.py`

| Skeleton behaviour | Problem | Implementation |
|---|---|---|
| `raw_signal["side"]` read directly | The Lipi alerts send `LONG\|delta=…\|cvd=…` text, not JSON with `side` | `signals.parse_gocharting_alert` handles both Lipi message shapes and JSON |
| `build_graph()` + new SQLite connection on every run | Leaks connections; recompiles each call | `Pipeline` compiles once per process |
| Re-invoking a crashed `thread_id` with fresh input | Restarts from scratch instead of resuming | `Pipeline.run` resumes from the checkpoint if `snapshot.next` is non-empty; a completed `run_id` is never re-run |
| `datetime.now(utc)` in event_guard / risk_check | Wrong in backtests (should be bar time); wrong trading day (MCX is IST) | State carries `as_of`; trading day is the IST date |
| Risk counts mix live and backtest runs | A backtest could exhaust live's 3 trades/day | Counts and P&L are filtered by `run_type` |
| `event_context` passed to Claude was always `None` (blocked runs never reach Claude) | Claude got no event context | Non-blocking upcoming events and the EIA snapshot are passed in `event_context` / `macro_context` |
| `size_multiplier` unbounded | Claude could scale size *up* | Clamped to [0, 1]; `MAX_LOTS_PER_TRADE` is the cap |
| Claude failure handling unspecified | | Any API error, refusal or incomplete output becomes a **veto** (fail closed) |
| Nodes mutate and return the whole state | Works, but hides what each node writes | Nodes return partial updates |
| Webhook duplicates | `alertcondition` and inline `alert()` can both fire; real-time bars can re-fire | Per-side cooldown (`SIGNAL_COOLDOWN_SECONDS`), in memory and in the store |

### Lipi script: verify in the GoCharting editor

1. `input()` signature and `cvd[N]` history indexing are **not verified** (flagged in the script).
2. Both `alertcondition()` and the inline `alert()` can fire for the same bar. Configure **one** in the Alert Widget, preferably the inline `alert()`, because it carries delta/CVD values. The receiver dedupes either way.
3. Intrabar delta is not final until the bar closes. If Lipi supports alert-on-bar-close, use it to avoid repainting signals.
4. `deltaThresholdInput = 40000` must be calibrated to CRUDEOIL's actual per-bar volume and timeframe. Keep `signals.LIPI_DELTA_THRESHOLD` in sync, because backtests use the Python port.
5. The webhook URL is `https://<vps>:<port>/webhook/gocharting/<GOCHARTING_WEBHOOK_SECRET>`.

### Open decisions

* **Sizing granularity:** with `MAX_LOTS_PER_TRADE = 1` CRUDEOIL lot, any Claude "adjust" below 1.0 rounds down to 0 lots, which means no trade. Trade CRUDEOILM (10 bbl) with a larger lot cap if partial sizing should matter.
* **Order routing:** direct DhanHQ v2 REST (current) or the Dhan MCP server / Claude Skill. Either way, `place_paper_order` stays the only place orders are sent from.
* **Backtest data:** Dhan OHLC history has no delta/CVD. Bars need to come from a GoCharting footprint export or from recorded ticks.
