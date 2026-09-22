# Lab 4 — Whale of the week: what the Polymarket leaderboard shows, and what it hides

**Status: weekly.** Every Sunday the pipeline snapshots the public leaderboard, pulls the top 25
wallets in full and rewrites the block below. One wallet gets the video; the group gets the table.

## Question

The weekly leaderboard says who made the most money. It does not say how: one bet or a hundred,
size that moves the book or size you could match, a bettor or a market maker, and whether the
"100% hit rate" a wallet's public page shows is real. All of that is in the same public API,
so we read it.

## What is measured

- **The #1 wallet by weekly PnL**: fills, markets, active hours, how concentrated the money is
  (fills ≥ $10k as a share of notional), the position that made the week, the biggest loss,
  maker vs taker rebates.
- **Hidden losses.** A position that resolves to zero never shows up in `closed-positions`: it
  stays in `positions` as "redeemable" for nothing. The public profile therefore lists wins only.
  We fold those positions back in as losses, dated by the market's end date, and report the
  honest 28-day hit rate and net.
- **The top 25 as a group**: how many made over half their week on one position, how many are
  makers, how many carry hidden losses, which categories, and (from the second snapshot on)
  how many of last week's top 10 survived.

Nothing here is advice. Wallets are pseudonymous proxy addresses and the names are what their
owners chose to display. The point is the shape of the money, not the person.

## This week

<!-- auto:week -->
**Snapshot 2026-09-22** · leaderboard window: 7 days · wallets pulled in full: top 25 by weekly PnL

**#1 totoro3miyazaki** — $1,817,968 on the weekly leaderboard, $4,272,819 volume.

- The week in trades: 125 fills in 3 market(s) over 17.6 hours; 17 fills of $10k+ carry 98% of the money; largest single fill $678,081; median fill $18.
- The position that made the week: **No** on “Will Chelsea FC win on 2026-09-18?” — $2,713,712 at 64.9¢ → $951,471 (74.3% of the week's closed PnL).
- Biggest loss in the window: **Alex Michelsen** on “US Open ATP: Frances Tiafoe vs Alex Michelsen” — $495,591 at 41.9¢ → −$495,591.
- 28 days, everything counted: 14 wins ($4,877,877) and 6 resolved losers still sitting unredeemed (−$2,549,552) → net $2,328,325, hit rate 70.0%. The public closed-positions list shows only the wins.
- Maker or taker: taker (maker rebates $1,208 vs taker rebates $13,180 in the window).

**Top 25 together**: $15,440,445 weekly PnL, the #1 wallet is 11.8% of it. 11 of 25 made more than half their week on one position (median: the best position is 35.0% of the week). 9 of 25 earn more maker than taker rebates. 17 of 25 carry resolved-but-unredeemed losses (1088 positions, −$12,374,332 over 28 days) that the closed-positions list does not show. Categories by notional: sports & esports 18, unknown 3, politics & macro 2, other 1, crypto 1.
- Week-over-week churn: needs a second snapshot.
<!-- /auto:week -->

## Files

- `fetch.py` — leaderboard (week/month × PnL/volume), then `/activity`, `/closed-positions`, `/positions` for the top 25; appends the leaderboard to `leaderboard_history.parquet`.
- `analyze.py` — the numbers above → `results.json`, `tables.md`, this README.
- Data: `data/labs/whales/<date>/` locally; the weekly parquet snapshot goes to the `data-latest` release.
