"""Round-trip cost of one paper trade, by venue, liquidity and stake (plan ueberarbeitung 18.10., section 3 'Neu' 2).

fee per side by venue, price impact of the stake against the pool (constant product: about stake / half the
liquidity per side), a fixed transaction cost per side (base + priority fee) against the stake, and a delay
surcharge for entering after the trigger. All as a share of the stake (0.05 = 5 %)."""
from __future__ import annotations

FEE_SIDE = {"pumpfun": 0.0125, "curve": 0.0125, "pumpswap": 0.003, "raydium": 0.0025, "meteora": 0.0025,
            "meteoradbc": 0.01, "orca": 0.003, "launchlab": 0.01}
FEE_DEFAULT = 0.005
TX_SOL_SIDE = 0.0002  # base fee + priority fee per transaction, SOL
DELAY = {"launch": 0.03, "copy": 0.02, "market": 0.01}  # adverse move between trigger and fill
SOL_USD = 180.0
CURVE_LIQ_USD = 12000.0  # depth of a fresh pump.fun curve (~30 SOL virtual reserve on each side) when none is known


def roundtrip(dex: str | None, liq_usd: float | None, stake_sol: float = 0.1, kind: str = "market",
              sol_usd: float = SOL_USD) -> float:
    dex = (dex or "").lower()
    fee = FEE_SIDE.get(dex, FEE_DEFAULT)
    liq = liq_usd if liq_usd and liq_usd > 0 else (CURVE_LIQ_USD if dex in ("pumpfun", "curve") else 0.0)
    stake_usd = stake_sol * sol_usd
    impact = min(0.5, stake_usd / (liq / 2)) if liq > 0 else 0.05
    tx = TX_SOL_SIDE / stake_sol if stake_sol > 0 else 0.0
    return 2 * (fee + impact + tx) + DELAY.get(kind, 0.01)
