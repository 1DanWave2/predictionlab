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
**Snapshot 2026-10-18** · leaderboard window: 7 days · wallets pulled in full: top 25 by weekly PnL

**#1 BreakTheBank** — $3,116,943 on the weekly leaderboard, $18,626,733 volume.

- The week in trades: 4932 fills in 91 market(s) over 162.0 hours; 68 fills of $10k+ carry 91% of the money; largest single fill $569,500; median fill $4.
- The position that made the week: **Broncos** on “Seahawks vs. Broncos” — $1,741,499 at 44.0¢ → $974,457 (41.8% of the week's closed PnL).
- Biggest loss in the window: **No** on “Will FK Bodø/Glimt win on 2026-10-14?” — $604,755 at 63.5¢ → −$604,755.
- 28 days, everything counted: 11 wins ($6,937,043) and 133 resolved losers still sitting unredeemed (−$6,363,017) → net $574,026, hit rate 7.6%. The public closed-positions list shows only the wins.
- Maker or taker: maker (maker rebates $21,840 vs taker rebates $3,822 in the window).

**Top 25 together**: $17,762,682 weekly PnL, the #1 wallet is 17.5% of it. 10 of 25 made more than half their week on one position (median: the best position is 31.0% of the week). 10 of 25 earn more maker than taker rebates. 21 of 25 carry resolved-but-unredeemed losses (1997 positions, −$27,948,882 over 28 days) that the closed-positions list does not show. Categories by notional: sports & esports 17, other 6, unknown 1, crypto 1.
- Week-over-week churn: needs a second snapshot.
<!-- /auto:week -->

## Files

- `fetch.py` — leaderboard (week/month × PnL/volume), then `/activity`, `/closed-positions`, `/positions` for the top 25; appends the leaderboard to `leaderboard_history.parquet`.
- `analyze.py` — the numbers above → `results.json`, `tables.md`, this README.
- Data: `data/labs/whales/<date>/` locally; the weekly parquet snapshot goes to the `data-latest` release.
