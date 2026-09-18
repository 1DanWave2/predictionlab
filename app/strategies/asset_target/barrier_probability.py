"""Closed-form first-passage barrier probability for GBM-modeled assets.

Per AI debate spec [GPT 6]: используем one-touch barrier formula, НЕ vanilla
Black-Scholes N(d2). Polymarket markets вида "BTC hit $X by DATE" — это
first-passage problem, не "close above strike at expiry".

Formula (upper barrier):
  S = current spot
  B = barrier / threshold (B > S для upper)
  T = years to deadline
  sigma = annualized volatility (decimal, e.g. 0.6 for 60%)
  mu = drift_log = drift - 0.5 * sigma^2  (для v1: drift=0)
  a = ln(B / S)

  P(hit upper B before T) =
    Phi((mu*T - a) / (sigma*sqrt(T)))
    + exp(2*mu*a / sigma^2) * Phi(-(mu*T + a) / (sigma*sqrt(T)))

Для lower barrier (B < S): применяем формулу к 1/S и 1/B (переворот).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def _phi(x: float) -> float:
    """Standard normal CDF Phi(x)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class BarrierInputs:
    spot: float            # current asset price
    barrier: float         # threshold price
    years_to_deadline: float
    annualized_vol: float  # decimal, e.g. 0.6 for 60%
    drift: float = 0.0     # log-drift, default 0 (conservative)


@dataclass
class BarrierResult:
    probability: float    # P(hit barrier before deadline)
    direction: str        # "upper" if barrier > spot else "lower"
    inputs: BarrierInputs
    notes: str = ""


def first_passage_probability(inputs: BarrierInputs) -> BarrierResult:
    """One-touch barrier hit probability under GBM.

    Returns BarrierResult с probability ∈ [0, 1].
    Edge cases:
      * spot already past barrier → probability = 1.0
      * T <= 0 → probability = 0.0 если barrier ещё не достигнут
      * vol <= 0 или nan → probability = 0.0 (вырожденный случай)
    """
    spot = inputs.spot
    barrier = inputs.barrier
    T = inputs.years_to_deadline
    sigma = inputs.annualized_vol
    drift = inputs.drift

    if spot <= 0 or barrier <= 0:
        return BarrierResult(0.0, "invalid", inputs, "non-positive spot/barrier")

    # Direction inferred only when prices differ.
    # Special case spot == barrier → already at barrier (any direction).
    if spot == barrier:
        return BarrierResult(1.0, "exact", inputs, "spot equals barrier")

    direction = "upper" if barrier > spot else "lower"

    if T <= 0 or sigma <= 0 or math.isnan(sigma):
        return BarrierResult(0.0, direction, inputs, f"degenerate T={T} sigma={sigma}")

    # Для lower barrier — используем симметрию: invert spot/barrier
    if direction == "lower":
        s = barrier
        b = spot
        # Теперь s < b, формула upper применима к 1/X = log(spot/barrier)
        # Просто пересчитаем a с обратным знаком: a = ln(barrier/spot) is negative
        # Проще — используем общую формулу с |a|:
        a = math.log(spot / barrier)  # positive (spot > barrier case)
        # Probability hit lower from above с drift = mu_log
    else:
        a = math.log(barrier / spot)  # positive (barrier > spot)

    sigma_sqrt_T = sigma * math.sqrt(T)
    mu_log = drift - 0.5 * sigma * sigma  # log-drift

    # term1 = Phi((mu*T - a) / (sigma*sqrt(T)))
    # term2 = exp(2*mu*a / sigma^2) * Phi(-(mu*T + a) / (sigma*sqrt(T)))
    # ВАЖНО: для lower barrier mu effective инвертируется
    if direction == "lower":
        # Когда смотрим P(hit lower before T) = P(hit upper for 1/S before T)
        # эквивалентно: drift_inverted = -mu_log
        mu_eff = -mu_log
    else:
        mu_eff = mu_log

    z1 = (mu_eff * T - a) / sigma_sqrt_T
    z2 = -(mu_eff * T + a) / sigma_sqrt_T
    term1 = _phi(z1)
    # Защита от overflow в exp:
    exp_arg = 2.0 * mu_eff * a / (sigma * sigma)
    if exp_arg > 700.0:
        # exp слишком большой — но Phi(z2) около 0 → вторая term ≈ 0
        term2 = 0.0
    else:
        term2 = math.exp(exp_arg) * _phi(z2)

    prob = max(0.0, min(1.0, term1 + term2))
    return BarrierResult(prob, direction, inputs)


def annualize_volatility_from_log_returns(
    log_returns: list[float],
    sample_per_year: float = 365.0,
) -> float:
    """Std of log returns × sqrt(samples/year) = annualized vol.

    For daily returns: sample_per_year=365.
    For hourly: sample_per_year=365*24.
    """
    if len(log_returns) < 2:
        return 0.0
    n = len(log_returns)
    mean = sum(log_returns) / n
    var = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
    return math.sqrt(var * sample_per_year)


def ewma_volatility_from_log_returns(
    log_returns: list[float],
    half_life_periods: float = 20.0,
    sample_per_year: float = 365.0,
) -> float:
    """Exponentially-weighted moving avg vol. Recent более weighted.

    half_life_periods=20 для 7d EWMA на daily data — расположение веса
    к недавним 1-2 неделям.
    """
    if len(log_returns) < 2:
        return 0.0
    decay = math.exp(-math.log(2.0) / half_life_periods)
    var = 0.0
    weight_sum = 0.0
    for i, r in enumerate(reversed(log_returns)):
        w = decay ** i
        var += w * (r * r)
        weight_sum += w
    var /= max(weight_sum, 1e-12)
    return math.sqrt(var * sample_per_year)
