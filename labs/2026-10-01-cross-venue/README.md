# Lab 3 — Cross-venue: the same question on Kalshi and Polymarket

**Status: results for 19 September – 15 October 2026 (26 days).** Prices of matched Kalshi ↔ Polymarket
pairs were polled every 5 minutes; this page has the pipeline, the matching rules and the analysis:
how far the two venues disagree, whether the gap was ever tradeable after fees, how long it lives,
which venue moves first, and whose price was closer to the outcome. Russian version: [README.ru.md](README.ru.md).

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

## Results (19 September – 15 October 2026)

Data: 26.1 days, 4,443 five-minute cycles, 4.88M samples on 2,447 pairs (1,859 game markets, 356 season futures and awards, 9 Fed buckets, 194 text-matched politics and events); 2,036 of the Kalshi markets settled inside the window. A sample counts only when both venues quote with a spread of 10¢ or less: 49% of all samples (65% of game samples before kick-off, 3% in play, 48% on futures, 63% on politics). Kalshi has no usable quote in 49% of samples, Polymarket in 32%; thin Kalshi books, not the matching, are the main loss of data. All numbers below are from `results.json`; the full tables are in `tables.md`.

### 1. The venues agree to within a cent, except on politics

| Category | Pairs | Samples | Median \|gap\| | p90 | > 5¢ | > 10¢ |
|---|---|---|---|---|---|---|
| Games, before kick-off | 1,823 | 1.22M | 0.5¢ | 1.5¢ | 0.3% | 0.1% |
| Games, in play | 1,088 | 24k | 0.5¢ | 2.0¢ | 3.5% | 1.5% |
| Season futures & awards | 220 | 695k | 0.55¢ | 2.0¢ | 1.2% | 0.2% |
| Fed decisions | 8 | 30k | 1.0¢ | 3.0¢ | 0.8% | 0.0% |
| Politics & events (text match) | 170 | 426k | 1.05¢ | 5.0¢ | 10.2% | 2.8% |

![gap_cdf](figures/gap_cdf.png)

The signed mean (Kalshi minus Polymarket) is −0.02¢ on games, +0.15¢ on futures and +0.23¢ on politics: neither venue carries a systematic premium. Only politics shows real disagreement, and there 11% of the pairs sit more than 5¢ apart on average.

### 2. After fees the arbitrage is real, small, slow, and not takeable by one account

Route: buy YES on the cheaper venue and NO on the other, hold to resolution; Kalshi's taker fee is 7% × p(1−p) per contract, Polymarket's is the market's own schedule from Gamma (4–5% × p(1−p) where fees are on, zero where they are off). Makers pay nothing on either venue, so a patient order could do better than these taker numbers.

| Category | Books crossed | Net > 0 after fees | Net > 1¢ | Median net when > 0 | p90 | Pairs ever net > 0 | Round-trip fees, median |
|---|---|---|---|---|---|---|---|
| Games, before kick-off | 2.3% | 0.39% | 0.12% | 0.33¢ | 6.1¢ | 9% | 1.9¢ |
| Games, in play | 9.3% | 5.5% | 3.6% | 1.8¢ | 17.9¢ | 40% | 1.4¢ |
| Season futures & awards | 50.6% | 36.0% | 6.7% | 0.28¢ | 1.5¢ | 83% | 0.31¢ |
| Fed decisions | 28.9% | 7.9% | 0.6% | 0.6¢ | 0.8¢ | 100% (8) | 2.3¢ |
| Politics & events | 45.8% | 27.2% | 13.5% | 0.99¢ | 6.5¢ | 92% | 1.15¢ |

On games the books cross in 2% of samples and clear both fees in 0.4%. On futures half the samples are crossed, but these are 2–10¢ contracts where fees are a third of a cent and the edge is a quarter of a cent per share locked until the end of a season. Politics is the only place with a cent or more in 13.5% of samples. None of it is a trade for a single account: Kalshi is US-only with KYC and Polymarket blocks US users, so the two legs live on two people's accounts, and the capital on both is frozen until resolution. The gap is a forecast signal, not an income.

### 3. How long a gap lives

| Category | Minutes per pair-day with net > 0 | Episode median | p75 | ≥ 15 min | ≥ 60 min | Half-life of the mid gap |
|---|---|---|---|---|---|---|
| Games, before kick-off | 2.6 | 15 min | 65 min | 50% | 27% | 106 min |
| Games, in play | 5.5 | 5 min | 5 min | 8% | 2% | 13 min |
| Season futures & awards | 261 | 171 min | 360 min | 83% | 69% | 17 h |
| Fed decisions | 59 | 58 min | 194 min | 73% | 50% | 4 h |
| Politics & events | 192 | 80 min | 295 min | 76% | 57% | 34 h |

![gap_survival](figures/gap_survival.png)

This is not a latency race. A gap that clears fees on a politics market lasts 80 minutes at the median and is still open an hour later in 57% of cases; the mid-to-mid disagreement itself decays with a half-life of a day or more outside games. Nobody is closing these gaps because nobody can (see above), not because they vanish. Durations are cut short by the collector's own pauses (the 6-hourly re-match and laptop sleep, 5 pauses a day on average), so the long tail is censored near 6 hours; the minutes-per-day column is not affected.

### 4. Who moves first

Cross-correlation of 5-minute mid changes, and an error-correction split: when the venues disagree at *t*, how much of the next 5-minute move toward the other venue each side makes. Intervals are 95% pair-level bootstraps.

| Category | Same interval | Kalshi leads (lag 1) | Polymarket leads (lag 1) | Share of a disagreement closed by Kalshi |
|---|---|---|---|---|
| Games, before kick-off | 0.40 | 0.006 [−0.03; 0.05] | 0.010 [−0.02; 0.05] | 55% [41; 65] |
| Season futures & awards | 0.22 | 0.012 [−0.04; 0.05] | 0.005 [−0.07; 0.07] | not measurable: Polymarket does not move toward Kalshi at all (β = −0.0004 [−0.0016; 0.0017]), Kalshi drifts toward Polymarket (+0.0017 [0.0008; 0.0053]) |
| Fed decisions | 0.51 | 0.14 [0.01; 0.19] | 0.22 [0.01; 0.30] | 92% [83; 100] (8 pairs) |
| Politics & events | 0.33 | 0.037 [0.005; 0.068] | 0.086 [0.048; 0.123] | 70% [60; 81] |

![lead_lag](figures/lead_lag.png)

Games move together inside the 5-minute window and neither side leads. On politics Polymarket moves first: its last move predicts Kalshi's next one twice as strongly as the reverse, and when the two disagree Kalshi makes 70% of the closing move. The Fed buckets point the same way on 8 pairs. On futures the gap closes only from the Kalshi side, slowly. In-play games cannot be read at 5-minute resolution (bid-ask bounce gives negative lag correlations) and are left out.

### 5. Who was right

**Settlement check.** 2,035 pairs settled on both venues; 22 (1.1%) settled differently, which means the pair was wrong, not that an arbitrage paid out: 12 of 1,883 games (six MLB games paired to the neighbouring day's game of the same series, both outcomes mirrored), 10 of 119 text pairs (eight "highest temperature" markets paired to the previous day, one "at least 8%" matched to "less than 8%", one "win" matched to "runner-up"), 0 of 33 futures. These 22 pairs contributed 0.03% of the politics samples, so the gap numbers above do not come from them; the matcher fixes are listed below. Among unresolved text pairs a similar share is probably wrong too.

**Calibration.** For the 1,868 correctly paired markets with a valid quote at least one hour before kick-off (games) or the Kalshi close (others):

| | Pairs | Brier Kalshi | Brier Polymarket | Difference, Kalshi − Polymarket |
|---|---|---|---|---|
| All | 1,868 | 0.1870 | 0.1878 | −0.0007 [−0.0014; −0.0002] |
| Games | 1,800 | 0.1934 | 0.1938 | −0.0004 [−0.0009; +0.0001] |
| Politics & events | 48 | 0.023 | 0.037 | −0.0145 [−0.030; −0.002] |

![brier](figures/brier.png)

On games the two venues are equally good, to the fourth decimal. On the 48 settled politics markets Kalshi's price an hour before the close was closer to the outcome: Polymarket kept a few cents on outcomes that were already decided, which is the same cheap-outcome premium as in Lab 1, seen from the other side. Only 8 pairs disagreed by more than 5¢ at the reference time, too few to say who is right when the venues really disagree; that question needs months, not weeks.

## What this means

- The "Kalshi–Polymarket arbitrage" of the forum posts is, on games, a crossed book in 2% of samples that clears fees in 0.4% and lasts minutes. On politics it is about a cent, it lasts hours, and it exists because no single account can trade both venues.
- Read the daily gap post in the channel accordingly: 1–2¢ on a game is noise; a few cents on a politics market is a slow disagreement in which Polymarket has usually moved first and Kalshi is usually the side that will move.
- For the engine: the cross-venue gap is a feature for a Kalshi-side strategy (Polymarket's move is a one-hour-ahead signal on politics), not for an arbitrage strategy.

## Caveats

- Top-of-book only, size not recorded: an executable gap at one contract may not exist at a hundred.
- 5-minute sampling. Anything faster, including most in-play action, is invisible; the in-play rows are included for completeness only.
- Fees: Kalshi's 7% schedule applies to most series (a few index markets are lower); cent rounding is ignored; Polymarket's schedule is per market from Gamma, 4% assumed where missing.
- Pairs are chosen by our matcher: 8% of the settled text pairs were wrong, and pairs further than 35¢ apart at match time are excluded as suspect, so the widest disagreements are out of the sample by construction.
- 26 days on one laptop: three days have under 100k samples because the machine slept; episode durations are censored at the collector's pauses.

## Matcher fixes queued

- Text pairs: when both titles carry a calendar date, require the same day (the temperature markets); add direction words (*less / under / below* vs *at least / more / over*) and *runner-up* to the discriminators that must agree.
- Games: pair MLB games by start time within two hours, not within a day, so back-to-back series and double-headers cannot cross.

## Files

- `teams.py` — team alias tables (NFL, MLB, NBA, NHL) and club-name normalisation.
- `fetch.py`, `match.py`, `collect.py` — pipeline; `pairs_manual.csv` — hand overrides.
- `settle.py` — settlement and fee metadata for every pair (Kalshi result, Polymarket outcome prices and fee schedule) → `data/labs/xvenue/meta.json`.
- `analyze.py` — the analysis above → `results.json`, `tables.md`, `figures/`.
- `deploy/Dockerfile`, `deploy/docker-compose.yml` — the collector container.
- Data: `xvenue.db.gz` (the collector's SQLite, 26 days of ticks) and `meta.json` are on the `data-latest` release of this repo; `pairs.parquet` is the current pair list.

## Reproduce

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt -r requirements-labs.txt
gh release download data-latest -p xvenue.db.gz -p meta.json -D data/labs/xvenue && gunzip data/labs/xvenue/xvenue.db.gz
python3 labs/2026-10-01-cross-venue/settle.py --db data/labs/xvenue/xvenue.db     # refreshes meta.json for pairs that settled since
python3 labs/2026-10-01-cross-venue/analyze.py --db data/labs/xvenue/xvenue.db    # ~4 min: results.json, tables.md, figures/
```
