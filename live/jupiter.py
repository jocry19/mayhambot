"""Jupiter quote and swap-transaction build for the live path. Unlike copytrade.market.jupiter_swap_tx, slippage is
fixed by us (no dynamicSlippage that could widen it) and the priority fee has a hard lamport cap."""
from __future__ import annotations

from copytrade import http, market  # type: ignore
from copytrade.config import Settings  # type: ignore


def quote(s: Settings, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int) -> dict | None:
    try:
        return http.get_json(f"{s.jupiter_url}/swap/v1/quote",
                             params={"inputMint": input_mint, "outputMint": output_mint, "amount": int(amount_raw),
                                     "slippageBps": int(slippage_bps), "restrictIntermediateTokens": "true"},
                             headers=market._jup_headers(s), limiter_name="jupiter", retries=1)
    except Exception:
        return None


def swap_tx(s: Settings, q: dict, pubkey: str, max_priority_lamports: int, level: str) -> dict:
    """{'swapTransaction': base64, 'lastValidBlockHeight': int, 'prioritizationFeeLamports': int, ...}"""
    d = http.post_json(f"{s.jupiter_url}/swap/v1/swap", {
        "quoteResponse": q, "userPublicKey": pubkey, "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True, "dynamicSlippage": False,
        "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {
            "maxLamports": int(max_priority_lamports), "priorityLevel": level}},
    }, headers=market._jup_headers(s), limiter_name="jupiter", retries=1)
    if not d or not d.get("swapTransaction"):
        raise RuntimeError(f"Jupiter swap ohne Transaktion: {str(d)[:160]}")
    return d
