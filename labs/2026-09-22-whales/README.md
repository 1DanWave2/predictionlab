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
**Snapshot 2026-10-04** · leaderboard window: 7 days · wallets pulled in full: top 25 by weekly PnL

**#1 BreakTheBank** — $2,671,890 on the weekly leaderboard, $8,384,061 volume.

- The week in trades: 2452 fills in 77 market(s) over 167.8 hours; 54 fills of $10k+ carry 85% of the money; largest single fill $280,000; median fill $14.
- The position that made the week: **Bears** on “Eagles vs. Bears” — $1,310,036 at 39.1¢ → $798,400 (88.7% of the week's closed PnL).
- Biggest loss in the window: **Patriots** on “Patriots vs. Jaguars” — $140,000 at 40.0¢ → −$140,000.
- 28 days, everything counted: 4 wins ($2,089,927) and 98 resolved losers still sitting unredeemed (−$4,491,875) → net −$2,401,948, hit rate 3.9%. The public closed-positions list shows only the wins.
- Maker or taker: maker (maker rebates $5,702 vs taker rebates $12 in the window).

**Top 25 together**: $16,008,973 weekly PnL, the #1 wallet is 16.7% of it. 9 of 25 made more than half their week on one position (median: the best position is 36.5% of the week). 9 of 25 earn more maker than taker rebates. 22 of 25 carry resolved-but-unredeemed losses (2283 positions, −$36,252,839 over 28 days) that the closed-positions list does not show. Categories by notional: sports & esports 16, other 5, politics & macro 2, unknown 2.
- Week-over-week churn: needs a second snapshot.
<!-- /auto:week -->

## Files

- `fetch.py` — leaderboard (week/month × PnL/volume), then `/activity`, `/closed-positions`, `/positions` for the top 25; appends the leaderboard to `leaderboard_history.parquet`.
- `analyze.py` — the numbers above → `results.json`, `tables.md`, this README.
- Data: `data/labs/whales/<date>/` locally; the weekly parquet snapshot goes to the `data-latest` release.
