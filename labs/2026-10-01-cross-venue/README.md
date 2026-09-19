# Lab 3 — Cross-venue: the same question on Kalshi and Polymarket

**Status: collecting.** Prices of matched Kalshi ↔ Polymarket pairs are polled every 5 minutes;
the analysis (gaps, who moves first, whether a gap was ever tradeable after fees) is written once
a few weeks of ticks exist. This page documents the pipeline and the matching rules so the
result can be reproduced or challenged.

## Question

Two venues list the same outcome — a game, a season champion, a Fed decision, a Senate race.
How often do their prices disagree, by how much, for how long, and which side moves first?
A persistent gap is either an arbitrage nobody can take (Kalshi needs KYC and is US-only,
Polymarket is blocked for US users) or a free forecast signal for whoever can trade one side.

## Pipeline

| Step | Script | What it does |
|---|---|---|
| 1 | `fetch.py` | Snapshot of open markets. Kalshi: `/markets?status=open&min_close_ts=now&mve_filter=exclude` by cursor (without the parlay filter the listing is 200k+ parlay legs and the daily game markets never come), plus the daily game series listed explicitly, event titles from `/events`. Polymarket: Gamma active markets by 24h volume (offset cap ~2000) plus the sports moneyline listing, with outcomes and CLOB token ids. |
| 2 | `match.py` | Three matchers, in order (a market taken by an earlier one is not offered to the next): **games** (same league, game date within a day, both teams agree — US leagues via the code/nickname table in `teams.py`, soccer/college via club-name tokens; each Kalshi team/tie market is tied to one Polymarket outcome), **groups** (templated series: season champions, Fed decision buckets, nominees, MVPs, Oscars — entities extracted on both sides, exact for teams/Fed buckets, name tokens with a mutual-best rule otherwise), **text** (token Jaccard on titles with dates removed, crude stemming and synonyms; numbers and discriminator words such as *vice*, *emergency*, *ticket* must agree; deadlines parsed from the wording must agree; mutual-best; score ≥ 0.6). |
| 3 | `collect.py` | Every 5 min: Kalshi `/markets?tickers=…` (300 per call) and Polymarket CLOB `POST /prices` (150 tokens per call) for every active pair → SQLite `ticks`. Every 6 h it re-runs steps 1–2 and refreshes the pair list (new games appear, closed markets are retired). Runs as a small container (`deploy/`). |

Safety rails in the matcher: a pair whose two mid prices differ by more than 35¢ at match time
is flagged `suspect` and not collected (a wrong pair looks exactly like a huge arbitrage);
`pairs_manual.csv` accepts or rejects specific pairs by hand and wins over everything;
`--verify` re-checks text pairs with Claude when an API key is present.

## Coverage on the first run (19 Sep 2026)

Snapshot: 137,631 open Kalshi markets (parlays excluded) × 3,800 active Polymarket markets.
676 candidate pairs, 673 kept (2 flagged suspect, 3 rejected by hand: an in-play soccer game
with a 39¢ gap, "visit Venezuela" vs "lead Venezuela", "impeached" vs "out").

| Branch | Pairs | What is in there |
|---|---|---|
| Daily games | 488 | College football 198, MLB 58, MLS 43, NFL 36, NHL 24, EPL 24, Serie A 24, Ligue 1 24, La Liga 21, Bundesliga 18, WNBA 18 (US leagues as moneyline outcomes, soccer as per-team + draw markets) |
| Templated series | 125 | NFL champion 23, World Series 14, Champions League 14, 2028 US president 9, Ballon d'Or 9, EPL champion 8, Democratic nominee 8, Brazil president 7, NBA champion 7, Fed decisions (Oct/Dec) 6, F1 5, Republican nominee 4, NHL 3, Bundesliga 3, WNBA 3, NFC champion 1, La Liga 1 |
| Free text | 60 | 2026 Senate races by party, 2027 French presidential candidates, Berlin and Mecklenburg-Vorpommern state elections, next Israeli PM, Brazil first-round placings, Trump / Xi / Merz deadline markets, Nobel Peace Prize |

Prices at match time already agree to within a cent on most game and futures pairs; the
larger gaps sit in the free-text politics pairs (Senate races 2–5¢, Republican nominee 5¢).
Whether those gaps persist, close, or were ever tradeable after fees is what the collector
is there to answer.

**Where it runs.** Kalshi's API answers 403 (CloudFront) to the project's server, which sits in
a region Kalshi geo-blocks; Polymarket answers fine. The container in `deploy/` is therefore
built but stopped, and the collector runs on a laptop for now — expect gaps while it sleeps. A
host in an unblocked region fixes this; nothing in the code changes.

## What will be measured

- Gap distribution per pair group (games / season futures / Fed / politics / other): mid vs mid,
  and the executable gap `Kalshi ask − Polymarket bid` (and the reverse) after each venue's fees.
- Time in a tradeable gap: how many minutes per day a gap above fees exists, and its half-life.
- Lead-lag: cross-correlation of 5-minute mid changes; which venue's move predicts the other's.
- Resolution check for pairs that settle inside the window: did both venues settle the same way
  (a mismatch means the pair was wrong, not that arbitrage existed).

## Files

- `teams.py` — team alias tables (NFL, MLB, NBA, NHL) and club-name normalisation.
- `fetch.py`, `match.py`, `collect.py` — pipeline; `pairs_manual.csv` — hand overrides.
- `deploy/Dockerfile`, `deploy/docker-compose.yml` — the collector container.
- Data: `data/labs/xvenue/{kalshi,polymarket,pairs}.parquet` locally; the collector's
  `xvenue.db` lives on the server and is added to the data release when the analysis is written.
