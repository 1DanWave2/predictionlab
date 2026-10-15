# Lab 3 tables (2026-09-19 to 2026-10-15, 26.1 days)

## Gap, mid vs mid (both venues quoted, spread <= 10c)

| Category | Pairs | Samples | Median abs gap | p90 | > 5c | > 10c | Mean Kalshi - Poly |
|---|---|---|---|---|---|---|---|
| games, before kick-off | 1,823 | 1,219,613 | 0.5c | 1.5c | 0.3% | 0.1% | -0.02c |
| games, in play | 1,088 | 24,076 | 0.5c | 2.0c | 3.5% | 1.5% | -0.08c |
| season futures & awards | 220 | 695,156 | 0.55c | 2.0c | 1.2% | 0.2% | +0.15c |
| Fed decisions | 8 | 29,913 | 1.0c | 3.0c | 0.8% | 0.0% | +0.14c |
| politics & events (text match) | 170 | 426,635 | 1.05c | 5.0c | 10.2% | 2.8% | +0.23c |

## Executable gap after fees

| Category | Books crossed | Net > 0 | Net > 1c | Median net when > 0 | p90 | Pairs ever net > 0 | Round-trip fees (median) |
|---|---|---|---|---|---|---|---|
| games, before kick-off | 2.28% | 0.39% | 0.12% | 0.33c | 6.09c | 9.2% | 1.94c |
| games, in play | 9.25% | 5.53% | 3.63% | 1.84c | 17.92c | 40.3% | 1.44c |
| season futures & awards | 50.6% | 36.04% | 6.69% | 0.28c | 1.48c | 83.2% | 0.31c |
| Fed decisions | 28.92% | 7.9% | 0.58% | 0.6c | 0.77c | 100.0% | 2.31c |
| politics & events (text match) | 45.79% | 27.22% | 13.54% | 0.99c | 6.54c | 92.4% | 1.15c |

## Episodes of a net-positive gap

| Category | Episodes | Minutes per pair-day | Median duration | p75 | >= 15 min | >= 60 min | Mid-gap half-life |
|---|---|---|---|---|---|---|---|
| games, before kick-off | 441 | 2.6 | 15.0 min | 65.0 min | 49.9% | 26.5% | 106.0 min |
| games, in play | 762 | 5.5 | 5.0 min | 5.0 min | 8.1% | 1.8% | 13.0 min |
| season futures & awards | 6,634 | 261.1 | 171.3 min | 360.3 min | 83.4% | 68.8% | 1048.0 min |
| Fed decisions | 102 | 59.2 | 58.1 min | 194.0 min | 72.5% | 50.0% | 242.0 min |
| politics & events (text match) | 3,979 | 191.8 | 80.0 min | 295.2 min | 76.4% | 57.0% | 2050.0 min |

## Lead-lag of 5-minute mid changes (95% pair bootstrap)

| Category | Samples | Same interval | Kalshi leads (lag 1) | Polymarket leads (lag 1) | Share of a disagreement closed by Kalshi |
|---|---|---|---|---|---|
| games, before kick-off | 1,190,662 | 0.4033 | 0.0061 [-0.0308; 0.0466] | 0.0096 [-0.0246; 0.0482] | 54.5% [40.7; 65.0] |
| games, in play | 21,993 | 0.9477 | -0.0532 [-0.0967; -0.0115] | -0.013 [-0.0593; 0.0248] | 62.9% [44.3; 80.2] |
| season futures & awards | 680,095 | 0.2197 | 0.0117 [-0.0387; 0.0539] | 0.0046 [-0.0695; 0.0735] | 128.6% [-742.0; 960.9] |
| Fed decisions | 29,250 | 0.5057 | 0.1419 [0.0064; 0.1886] | 0.2231 [0.0114; 0.3] | 91.6% [83.4; 100.1] |
| politics & events (text match) | 416,674 | 0.3306 | 0.0374 [0.005; 0.068] | 0.0856 [0.0475; 0.1232] | 70.2% [59.8; 81.2] |

## Settlement and calibration

Settled on both venues: 2,035; settled differently (wrong pair): 22 (1.08%).

| Category | Pairs | Brier Kalshi | Brier Polymarket | Difference (K - P) | Disagreements > 5c at the reference quote |
|---|---|---|---|---|---|
| all | 1,868 | 0.187 | 0.1878 | -0.0007 [-0.0014; -0.0002] | 8 |
| games | 1,800 | 0.1934 | 0.1938 | -0.0004 [-0.0009; 0.0001] | 2 |
| text | 48 | 0.023 | 0.0374 | -0.0145 [-0.0302; -0.0024] | 6 |
