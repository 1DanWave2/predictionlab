#!/bin/bash
# AI veto effectiveness — count GO/SKIP from last 24h of docker logs
docker logs --since 24h polymarket-bot 2>&1 | python3 -c "
import sys, re
go_total = 0
skip_total = 0
batches = 0
skip_reasons = []
for line in sys.stdin:
    m = re.search(r'scanner\.ai_veto \| input=(\d+) kept=(\d+)', line)
    if m:
        inp, kept = int(m.group(1)), int(m.group(2))
        batches += 1
        go_total += kept
        skip_total += inp - kept
        continue
    m = re.search(r'scanner\.ai_veto_skip \| market_id=(\S+) reason=(.*?) conf=', line)
    if m:
        skip_reasons.append((m.group(1), m.group(2).strip()))
print(f'=== AI Veto stats (last 24h) ===')
print(f'Batches: {batches}')
print(f'Total GO: {go_total}')
print(f'Total SKIP: {skip_total}')
total = go_total + skip_total
if total:
    print(f'SKIP rate: {skip_total/total*100:.1f}%')
print()
print(f'=== Last 20 SKIP reasons ===')
for mid, reason in skip_reasons[-20:]:
    print(f'  {mid}: {reason}')
"
