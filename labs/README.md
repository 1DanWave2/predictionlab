# Labs

Published research reports live in this folder, one subfolder per report, each with the script that produced it, the data window, the filters, and the losing cases. The scripts themselves live in `scripts/`; this page is the index.

Convention for a report folder:

```
labs/YYYY-MM-DD-slug/
  README.md        the report (English), with a Russian version README.ru.md
  run.py           the exact script or command that produced the numbers
  figures/         charts
  data-window.txt  first and last snapshot timestamp, market count, filters
```

## Published

| # | Report | One-line result |
|---|--------|-----------------|
| 1 | [Longshot fade](2026-09-18-longshot-fade/README.md) ([RU](2026-09-18-longshot-fade/README.ru.md)) | On 183k closed markets, outcomes priced 5–20¢ within a week of the event win *more* often than priced; the textbook overpricing exists only under 2¢ and a month out, worth 0.4–1.7¢ per share. Selling longshots as a taker loses net. |

## Planned reports

| # | Report | Question | Scripts |
|---|--------|----------|---------|
| 2 | Maker mode and liquidity rewards | What a two-sided quoter earns net of adverse selection on small accounts | `paper_maker_forensic*.py`, `paper_tail_maker_sim.py`, `rewards_scanner.py` |
| 3 | Cross-venue gaps | Size and persistence of Kalshi vs Polymarket vs sportsbook gaps on matched events | `cross_market_radar.py`, `odds_api_audit.py`, `app/integrations/kalshi_seed_map.py` |

## Research scripts by theme

Descriptions are taken from each script's header; open the script for the full method.

**Calibration and resolution**
- `category_calibration.py`: calibration overlay per market category
- `external_calibration.py`: calibration against Gamma resolved markets
- `resolution_risk.py`: resolution-risk filter for markets with ambiguous rules
- `resolutions_watcher.py`: alert when the count of resolved weather markets crosses thresholds
- `weather_resolution_scorer.py`: first ground-truth Brier check on weather markets

**Fade research (buying dips, selling pumps)**
- `fade_shadow_scanner.py`: shadow scanner for the fade-any edge (median +3.61% at 30 minutes, 57% hit rate on the validation set)
- `fade_replay.py`: replay analyzer for fade-any trades
- `fade_any_funnel.py`: why the strategy produced zero trades after deploy
- `fade_any_correlation.py`: correlation tagging of fade-any trades
- `impact_fade.py`: impact-fade detector with walk-forward validation
- `longtail_moonshot_research.py`: long-tail (longshot) research
- `longtail_event_audit.py`: event-level audit of long-tail trades

**Maker and rewards**
- `paper_maker_forensic.py`, `_v2.py`, `_v3.py`: forensic and live-readiness audits of the paper maker
- `paper_tail_maker_sim.py`: tail-maker simulator
- `rewards_scanner.py`: Polymarket liquidity-rewards scanner

**Cross-venue and arbitrage**
- `cross_market_radar.py`: cross-platform radar (Kalshi seed map vs Polymarket)
- `neg_risk_arb_scanner.py`: negative-risk (sum-to-one) arbitrage scanner
- `odds_api_audit.py`: The Odds API usage audit
- `hedge_shadow.py`: hedge-manager shadow simulator

**Smart-money (wallet following and fading)**
- `smart_money_poc.py`, `smart_money_poc_v2.py`, `smart_money_v2_weather.py`: proofs of concept
- `sm_validator.py`: early validator
- `sm_canary_lab.py`: shadow scoring of wallet signals
- `sm_mirror_backtest.py`, `sm_dollar_backtest.py`: mirror and fade dollar-PnL backtests
- `sm_fade_canary_runner.py`: paper canary that fades tracked losing wallets

**Single-family canaries**
- `tennis_underdog_shadow.py`, `tennis_underdog_resolve.py`, `tennis_underdog_paper_runner.py`, `tennis_underdog_paper_resolver.py`: tennis underdog canary, shadow and paper phases
- `weather_bucket_shadow.py`: weather bucket shadow
- `wti_mr_canary_runner.py`: WTI mean-reversion canary
- `asset_target_dollar_backtest.py`, `asset_target_haircut_backtest.py`: asset-target backtests
- `event_diagnostic.py`: event-strategy diagnostic

**Reporting and operations**
- `analysis_dashboard_24h.py`: cross-strategy 24–48h analysis
- `mfe_mae_report.py`: MFE/MAE retrospective
- `morning_report.py`: daily Telegram summary
- `poll_fills.py`: production fills logger
- `bot_health_alerts.py`: operational health alerts
- `ai_veto_stats.sh`: LLM veto effectiveness from container logs
- `ai_debate_bridge.py`, `debate_heartbeat.py`, `tg_relay_worker.py`: tooling for the two-model review loop used during development
