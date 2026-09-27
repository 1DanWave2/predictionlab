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
**Snapshot 2026-09-27** · leaderboard window: 7 days · wallets pulled in full: top 25 by weekly PnL

**#1 0x361b16e3ddfe1d415d41008daac2631d94ab74fe** — $2,223,397 on the weekly leaderboard, $2,238,016 volume.

- The week in trades: 157 fills in 38 market(s) over 164.8 hours; 40 fills of $10k+ carry 87% of the money; largest single fill $72,513; median fill $367.
- The position that made the week: **Yes** on “Will Spain win on 2026-09-26?” — $1,055,431 at 50.0¢ → $527,973 (51.0% of the week's closed PnL).
- Biggest loss in the window: **No** on “Will Frosinone Calcio win on 2026-09-20?” — $5 at 86.0¢ → −$5.
- 28 days, everything counted: 6 wins ($1,035,121) and 2 resolved losers still sitting unredeemed (−$7) → net $1,035,114, hit rate 75.0%. The public closed-positions list shows only the wins.
- Maker or taker: taker (maker rebates $0 vs taker rebates $4,185 in the window).

**Top 25 together**: $17,944,367 weekly PnL, the #1 wallet is 12.4% of it. 5 of 25 made more than half their week on one position (median: the best position is 24.5% of the week). 6 of 25 earn more maker than taker rebates. 21 of 25 carry resolved-but-unredeemed losses (3158 positions, −$24,756,934 over 28 days) that the closed-positions list does not show. Categories by notional: sports & esports 20, other 4, unknown 1.
- Week-over-week churn: needs a second snapshot.
<!-- /auto:week -->

## Files

- `fetch.py` — leaderboard (week/month × PnL/volume), then `/activity`, `/closed-positions`, `/positions` for the top 25; appends the leaderboard to `leaderboard_history.parquet`.
- `analyze.py` — the numbers above → `results.json`, `tables.md`, this README.
- Data: `data/labs/whales/<date>/` locally; the weekly parquet snapshot goes to the `data-latest` release.
