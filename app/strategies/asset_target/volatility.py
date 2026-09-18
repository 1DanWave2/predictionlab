"""Volatility ensemble per [GPT 6] spec.

  sigma = weighted_median(
    Deribit IV:        weight 0.50,  (v1.5, not yet)
    30d realized:      weight 0.30,
    7d EWMA:           weight 0.20,
  )

В v1 нет Deribit feed — используем historical only с uncertainty haircut:
  sigma = max(30d_realized, 0.7 * 7d_realized + 0.3 * 30d_realized)
  uncertainty_haircut += 0.04
"""

from __future__ import annotations

from dataclasses import dataclass

from app.integrations.crypto_price import CryptoPriceClient
from app.strategies.asset_target.barrier_probability import (
    annualize_volatility_from_log_returns,
    ewma_volatility_from_log_returns,
)


@dataclass
class VolEstimate:
    annualized_vol: float        # decimal, e.g. 0.65 = 65%
    components: dict[str, float] # debug: {"realized_30d": 0.55, "ewma_7d": 0.62, ...}
    uncertainty: float           # additional haircut to add ON TOP of 0.03 floor
    source: str                  # "historical" | "deribit_ensemble" | "fallback"


async def estimate_volatility(
    asset_symbol: str,
    price_client: CryptoPriceClient,
    asset_class: str = "crypto",
) -> VolEstimate | None:
    """Compute ensemble volatility for given asset.

    Возвращает None если данных недостаточно.
    """
    if not price_client.supported(asset_symbol):
        # Для commodities/equities пока fallback (нет feed в v1)
        return None

    # 30 daily candles → log-returns × sqrt(365) = annualized
    daily = await price_client.get_log_returns(asset_symbol, interval="1d", limit=30)
    if daily is None or len(daily.log_returns) < 7:
        return None

    realized_30d = annualize_volatility_from_log_returns(
        daily.log_returns, sample_per_year=365.0,
    )

    # 7d EWMA на тех же daily returns
    ewma_7d = ewma_volatility_from_log_returns(
        daily.log_returns[-14:],  # last 14 days с decay
        half_life_periods=5.0,
        sample_per_year=365.0,
    )

    # GPT spec: sigma = max(30d, 0.7*7d + 0.3*30d)
    weighted = 0.7 * ewma_7d + 0.3 * realized_30d
    sigma = max(realized_30d, weighted)

    # Дополнительная haircut если crypto без Deribit IV
    uncertainty = 0.04 if asset_class == "crypto" else 0.06

    return VolEstimate(
        annualized_vol=sigma,
        components={
            "realized_30d": round(realized_30d, 4),
            "ewma_7d": round(ewma_7d, 4),
            "weighted_70_30": round(weighted, 4),
        },
        uncertainty=uncertainty,
        source="historical",
    )
