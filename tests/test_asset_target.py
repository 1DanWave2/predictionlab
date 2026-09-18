"""Tests for asset_target parser + barrier probability."""

from __future__ import annotations

import math

from app.strategies.asset_target.barrier_probability import (
    BarrierInputs,
    annualize_volatility_from_log_returns,
    ewma_volatility_from_log_returns,
    first_passage_probability,
)
from app.strategies.asset_target.parser import (
    TargetDirection,
    parse_asset_target,
)


# ============== PARSER ==============

def test_parse_btc_simple():
    s = parse_asset_target("Will BTC hit $120K by May 31, 2026?")
    assert s.asset_symbol == "BTC"
    assert s.threshold_usd == 120_000
    assert s.direction == TargetDirection.UPPER
    assert s.deadline_iso == "2026-05-31"
    assert s.tradable
    assert s.cluster_key == "BTC:2026-05-31:upper"


def test_parse_eth_with_comma():
    s = parse_asset_target("Will Ethereum reach $5,000 by 2026-06-30?")
    assert s.asset_symbol == "ETH"
    assert s.threshold_usd == 5_000
    assert s.deadline_iso == "2026-06-30"
    assert s.direction == TargetDirection.UPPER
    assert s.tradable


def test_parse_wti_in_month_format():
    s = parse_asset_target(
        "Will WTI Crude Oil (WTI) hit (HIGH) $110 in May?",
        current_year=2026,
    )
    assert s.asset_symbol == "WTI"
    assert s.threshold_usd == 110.0
    assert s.deadline_iso == "2026-05-31"
    assert s.direction == TargetDirection.UPPER
    assert s.tradable


def test_parse_lower_direction():
    s = parse_asset_target("Will SOL drop below $100 by June 30, 2026?")
    assert s.asset_symbol == "SOL"
    assert s.direction == TargetDirection.LOWER
    assert s.threshold_usd == 100.0
    assert s.cluster_key == "SOL:2026-06-30:lower"


def test_parse_unknown_asset():
    s = parse_asset_target("Will Tesla hit $500 by year end?")
    # Tesla не в списке — fail
    assert s.asset_symbol is None
    assert not s.tradable


def test_parse_with_dollar_in_text():
    s = parse_asset_target("Will Bitcoin hit (HIGH) $200,000 in May 2026?")
    assert s.asset_symbol == "BTC"
    assert s.threshold_usd == 200_000
    assert s.deadline_iso == "2026-05-31"


def test_parse_million_suffix():
    s = parse_asset_target("Will NVIDIA market cap hit $4M by 2026-06-30?")
    # NVIDIA not yet in mapping — should fail
    assert s.asset_symbol is None


# ============== BARRIER PROBABILITY ==============

def test_barrier_already_hit_upper():
    """spot=110, barrier=100 → direction inferred as lower (barrier < spot).
    Test что 'already hit' detected когда spot за barrier'ом.
    BUT: для upper barrier "already hit" = spot >= barrier (direction=upper выбран parser'ом).
    Тут direction inferred from prices — это OK для базовой формулы.
    """
    # Already past lower barrier: spot=80, barrier=100 → direction=upper, spot < barrier (not yet hit upper)
    # Чтобы был "already hit lower": spot=80, barrier=100, direction=upper не подходит
    # Лучше тест "spot ровно at upper": spot=100, barrier=99 → direction=lower (barrier < spot), но spot выше — НЕ already hit lower (lower hit = spot ≤ barrier)
    # Простой "already hit": spot=100, barrier=100 → equal
    inputs = BarrierInputs(spot=100, barrier=100, years_to_deadline=1, annualized_vol=0.5)
    r = first_passage_probability(inputs)
    # spot == barrier → either direction уже hit. P=1
    # Логика: direction=upper (default when equal), spot >= barrier → already hit
    assert r.probability == 1.0


def test_barrier_far_above_low_vol_low_prob():
    inputs = BarrierInputs(spot=100, barrier=200, years_to_deadline=1.0, annualized_vol=0.2)
    r = first_passage_probability(inputs)
    # Barrier 2x spot, sigma=20%, 1 year — низкая prob
    assert 0.0 < r.probability < 0.20


def test_barrier_close_high_vol_high_prob():
    inputs = BarrierInputs(spot=100, barrier=110, years_to_deadline=0.25, annualized_vol=0.6)
    r = first_passage_probability(inputs)
    # Barrier 10% above, sigma=60%, 3 months — высокая prob
    assert r.probability > 0.5


def test_barrier_t_zero():
    inputs = BarrierInputs(spot=100, barrier=110, years_to_deadline=0, annualized_vol=0.5)
    r = first_passage_probability(inputs)
    # Time expired, not hit yet → 0
    assert r.probability == 0.0


def test_annualize_vol_from_returns():
    # 30 daily returns of 1% (log) → annualized ~ 0.01 * sqrt(365)
    rs = [0.01] * 30
    # Std of constant is 0
    assert annualize_volatility_from_log_returns(rs) == 0.0
    # Mix with some variance:
    import random
    random.seed(42)
    rs2 = [random.gauss(0.001, 0.02) for _ in range(100)]
    sigma = annualize_volatility_from_log_returns(rs2)
    # Daily std ~0.02 → annualized ~0.02 * sqrt(365) ~ 0.38
    assert 0.30 < sigma < 0.45


def test_ewma_vol_recent_weighted_more():
    rs = [0.01] * 20 + [0.05] * 5  # большой spike в конце
    sigma_ewma = ewma_volatility_from_log_returns(rs, half_life_periods=5.0)
    sigma_simple = annualize_volatility_from_log_returns(rs)
    # EWMA должна сильно реагировать на recent spike
    assert sigma_ewma > sigma_simple


# ============== INTEGRATION (no network — basic shape) ==============

def test_parser_cluster_key_distinct_directions():
    upper = parse_asset_target("Will BTC hit $120K by 2026-05-31?")
    lower = parse_asset_target("Will BTC drop below $80K by 2026-05-31?")
    assert upper.cluster_key != lower.cluster_key


def test_parser_cluster_key_distinct_thresholds():
    """Per [GPT 6]: same asset/deadline but different thresholds — same cluster key.

    Это правильно, потому что 'BTC by 2026-05-31 upper' одно direction-семейство,
    хоть thresholds разные. Cluster guard блокирует duplicate trades в одном cluster.
    """
    a = parse_asset_target("Will BTC hit $120K by 2026-05-31?")
    b = parse_asset_target("Will BTC hit $130K by 2026-05-31?")
    # Both upper, same asset+deadline → same cluster (correct for guard)
    assert a.cluster_key == b.cluster_key == "BTC:2026-05-31:upper"
