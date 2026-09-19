# PredictionLab

An open-source research and paper-trading engine for prediction markets (Polymarket first, Kalshi and sportsbook odds as external anchors), with an honest, public lab notebook of what works and what does not.

> **Status: alpha, paper-trading only.** Live trading is hard-blocked in code. Nothing here is financial advice. Check the terms of service and the law of your own jurisdiction before you connect any exchange account.

## Why this exists

Most prediction-market tooling sells alerts and signals. Almost none of it shows you the loss column. PredictionLab is built the other way round: every strategy carries explicit validation gates, every result is logged, and the lab reports publish the negative findings alongside the positive ones. The engine was built over several months while testing more than a dozen strategy ideas on Polymarket; most of them were falsified, and that record is part of the product.

## What is in the box

- **Market data.** Polymarket Gamma (market metadata), CLOB (order books, price history) and Data API (fills) clients, a WebSocket subscriber, a normalizer that turns raw markets into one internal model, and a market cache.
- **Pricing.** A fair-price engine (VWAP, order-book imbalance, mean reversion, time decay, momentum filter), a signal engine with confidence gating, and optional LLM fair-price estimation and LLM veto (any OpenAI-compatible endpoint).
- **External anchors.** The Odds API client (no-vig probabilities from sharp and main sportsbooks), a sports matchup mapper with team aliases, and a Kalshi seed map for cross-venue comparison.
- **Strategies as plugins.** A registry that is the single source of truth for each strategy: execution status (live / paper / shadow / frozen / disabled), hold horizon, risk unit, capital lock-up class and the validation gates it must pass to graduate.
- **Risk manager.** Position, notional, daily-loss, cooldown and per-market limits; rejects DCA into losing positions; every rejection is logged to a funnel.
- **Paper execution and PnL.** Paper fills against live books, position manager with realized and unrealized PnL, take-profit, stop-loss and stale-exit rules.
- **Backtest runner** over stored market snapshots.
- **Dashboard.** FastAPI app with a live dashboard and analytics pages, plus admin endpoints (status, pause, resume, set mode, positions, trades, reset).
- **Integrations.** Outbound webhook events to n8n or any HTTP endpoint, inbound Telegram command endpoints, ready-made n8n workflow (see `docs/integrations/`).
- **Labs.** ~50 research scripts under `scripts/` (calibration, fade replays, maker forensics, longshot audits, cross-market radar, negative-risk arbitrage, weather and tennis canaries, and more). See `labs/README.md` for the index and `labs/` for published reports.

## Architecture

```
  Polymarket Gamma / CLOB / Data API / WebSocket      The Odds API      Kalshi (seed map)
                    │                                       │                 │
                    ▼                                       ▼                 ▼
             market_data/ ──► normalizer ──► market_cache ◄── external anchors ─┘
                    │
                    ▼
             pricing/  fair_price_engine ─► signal_engine ◄── ai/ (fair price, veto, classifier)
                    │
                    ▼
             strategies/  registry (status, horizon, risk unit, gates) ─► plugins
                    │
                    ▼
             execution/  risk_manager ─► execution_router ─► paper_execution ─► position_manager
                    │                                                              │
                    ▼                                                              ▼
             tasks/  scanner · trader · reporter                              SQLite / Postgres
                    │
                    ▼
             api/  dashboard · admin · webhooks ──► n8n / Telegram
```

## Quickstart

Requirements: Python 3.11+ (3.13 works), pip.

```bash
git clone https://github.com/1DanWave2/predictionlab.git
cd predictionlab
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Run one scan tick on mock data (no network, no keys):

```bash
USE_MOCK_DATA=true python3 -m app.main --once
```

Run the bot and the API together (paper mode, live Polymarket data, no keys required for reading):

```bash
python3 -m app.main
```

Open the dashboard at `http://127.0.0.1:8000/dashboard` and the API docs at `http://127.0.0.1:8000/docs`. API only:

```bash
python3 -m app.main --api-only
```

Docker:

```bash
docker compose up --build
```

Tests:

```bash
pytest -q
```

Lab dependencies (pandas, matplotlib, pyarrow, tabulate):

```bash
pip install -r requirements-labs.txt
```

## Configuration

Everything is configured through environment variables (see `.env.example`, which lists every setting with its default). The ones you will touch first:

| Variable | Default | Meaning |
|----------|---------|---------|
| `APP_MODE` | `paper_auto` | `paper_auto` fills paper orders; `shadow` logs signals without fills |
| `ENABLE_LIVE_TRADING` | `false` | Live trading is blocked regardless; kept as an explicit guard |
| `USE_MOCK_DATA` | `true` | Mock order books for offline runs; set `false` for live Polymarket data |
| `DATABASE_URL` | `sqlite:///./paper_bot.db` | SQLite by default; Postgres works via the compose profile |
| `INITIAL_PAPER_BALANCE` | `100` | Paper account size |
| `MAX_OPEN_POSITIONS`, `MAX_ORDER_NOTIONAL`, `DAILY_LOSS_LIMIT` | see file | Risk limits |
| `TAKE_PROFIT_PCT`, `STOP_LOSS_PCT`, `MIN_SL_AGE_MINUTES` | see file | Exit rules |
| `ODDS_API_KEY` | empty | Enables sportsbook anchors (free tier: 500 requests/month) |
| `AI_FAIR_PRICE_ENABLED`, `AI_VETO_ENABLED`, `GROQ_API_KEY` | `false` | Optional LLM fair price and veto through any OpenAI-compatible endpoint |
| `N8N_WEBHOOK_URL`, `N8N_WEBHOOK_SECRET` | empty | Outbound events |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | empty | Telegram notifications through the n8n workflow |

Strategy toggles (`SNIPER_ENABLED`, `CONSENSUS_DRIFT_ENABLED`, `MOMENTUM_ENABLED`, `ASSET_TARGET_ENABLED`, `FINANCIAL_STRATEGY_ENABLED`, and so on) are read at startup. If a strategy is enabled in `.env` but its registry entry is `frozen` or `disabled`, the bot refuses to start. That is deliberate.

## Strategies and where they stand

The registry (`app/strategies/registry.py`) is the truth; this table is a snapshot of it.

| Strategy | Status | Idea | What the data said |
|----------|--------|------|--------------------|
| `sniper_sports` | paper | Buy when sportsbook no-vig probability disagrees with the Polymarket price on an exactly matched game | Sparse but positive setups; depends on The Odds API |
| `event_strategy` | paper | Default fair-price-vs-market edge on event markets, TP/SL exits | Baseline strategy |
| `tennis_underdog_canary` | paper | Buy WTA/ATP underdogs priced 7.5–10 cents | 31.7% win rate vs 8.7% implied in audit, with a 23-loss streak; needs n ≥ 150 events |
| `fade_shadow_scanner` | shadow | Detect pumps and dumps for fade research | Telemetry only |
| `neg_risk_arb_scanner` | shadow | Find multi-outcome markets whose prices do not sum to 1 | Telemetry only |
| `hedge_shadow` | shadow | Simulated hedge round-trips | Telemetry only |
| `sm_fade_weather` | shadow | Fade a specific smart-money wallet on weather markets | Awaiting resolved-event count |
| `fade_any` | frozen | Buy 5-point drops in 5 minutes, expect a bounce | 31 trades, 58% win rate, net negative: fat left tail |
| `asset_target` | frozen | One-touch barrier probability for "asset above X by date" markets | 60-minute hypothesis falsified on 6,077 trades; resolution hypothesis untested |
| `wti_mr_canary` | frozen | Mean reversion on WTI price-target markets | Single correlated market family, no-go |
| `maker` | disabled | Two-sided quoting for liquidity rewards | Not viable on a $100 paper account; revisit at $1,000+ |

Frozen and disabled strategies stay in the repo on purpose: the code and the numbers are the lab notebook.

## Labs

`labs/` holds published reports; `scripts/` holds the research scripts they are built from. Weekly digests in Russian go to the Telegram channel [@predictionlab_ru](https://t.me/predictionlab_ru); the site at [1danwave2.github.io/predictionlab](https://1danwave2.github.io/predictionlab/) mirrors the reports.

Published:

1. **[Longshot fade](labs/2026-09-18-longshot-fade/README.md)** ([RU](labs/2026-09-18-longshot-fade/README.ru.md)). 182,503 closed markets, 765k observations at fixed horizons before the event. Outcomes priced 5–20¢ within a week of the event win *more* often than their price implies; the textbook overpricing exists only under 2¢ and a month or more out, and is worth 0.4–1.7¢ per share. Selling longshots as a taker loses after fees.

Planned:

2. **Maker mode and liquidity rewards.** What a two-sided quoter actually earns net of adverse selection, on 479k order-book snapshots with the rewards parameters attached.
3. **Cross-venue gaps.** How large and how persistent Kalshi vs Polymarket vs sportsbook gaps are on matched events.

Each report ships with the script, the data window, the exact filters, and the losing cases.

## Data

The engine stores market snapshots, paper orders and positions in SQLite by default. Databases are git-ignored; put yours under `data/`.

The lab data store (closed markets, daily YES-price histories, observations) is published as parquet files on the `data-latest` release and refreshed every night by the `nightly-data` workflow, which pulls only what closed since the last run from Polymarket's public APIs, rebuilds the observations and commits the updated results and figures. `gh release download data-latest --pattern '*.parquet'` gets you the current store (~200 MB).

## Integrations

- `docs/integrations/n8n_workflow.json`: an n8n workflow that receives bot events, formats them for Telegram, and forwards Telegram commands back to the API.
- `docs/integrations/n8n-telegram-setup.ru.md` and `n8n-workflow-design.ru.md`: setup notes (Russian; English version planned).
- `docs/integrations/examples/`: example webhook payloads.

## Roadmap

- One-click deploy (compose plus a VPS bootstrap script) with Telegram alerts out of the box
- Kalshi client beyond the seed map; generic cross-venue market matcher
- Weekly lab reports
- Hosted option for people who do not want to run infrastructure

## Contributing

Issues and pull requests are welcome, especially new strategy plugins with their validation gates filled in, and lab scripts with reproducible data windows. See `CONTRIBUTING.md`.

## Disclaimer

This software is for research and paper trading. It does not execute live orders. Prediction markets are restricted or prohibited in many jurisdictions; you are responsible for complying with the terms of any venue you connect to and with the law where you live. Past paper results do not predict anything.

## License

MIT. See `LICENSE`.
