"""Buyers in the first slots of launches that migrate at once (18.10.: the creator buys 79.31 % of the curve in
the create, the curve completes, and setup wallets buy 160 to 2150 SOL on the pump AMM in the same block). Those
buys never touch the bonding curve, so the trade stream cannot see them; they are read from the chain: the mint's
oldest signatures (getSignaturesForAddress, paged back) and, per transaction of the first slots, who gained the
token (token balance deltas) and what it cost them in SOL."""
import logging

log = logging.getLogger("sniper.launchblock")
SLOTS, MAX_TX, PAGES = 3, 15, 4
MIN_SOL = 0.01
# highest transaction version getTransaction returns: v1 transactions exist on mainnet, and an RPC answers -32015
# for any transaction above this, so the newest (often exactly the pump AMM setup buys) would be missing
TX_VERSION = 1
WSOL = "So11111111111111111111111111111111111111112"


def _owner_deltas(meta, mint):
    pre = {(b.get("owner"), b.get("accountIndex")): float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
           for b in meta.get("preTokenBalances") or [] if b.get("mint") == mint}
    out = {}
    for b in meta.get("postTokenBalances") or []:
        if b.get("mint") != mint:
            continue
        k = (b.get("owner"), b.get("accountIndex"))
        d = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0) - pre.get(k, 0.0)
        out[k[0]] = out.get(k[0], 0.0) + d
    return out


def tx_buyers(tx, mint, exclude=()):
    """[(wallet, tokens, sol)] of one jsonParsed transaction: owners whose balance of `mint` rose and whose own
    account paid at least MIN_SOL for it."""
    meta = (tx or {}).get("meta") or {}
    if meta.get("err"):
        return []
    keys = [k.get("pubkey") if isinstance(k, dict) else k
            for k in (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])]
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    lam = {k: (pre[i] - post[i]) / 1e9 for i, k in enumerate(keys) if i < len(pre) and i < len(post)}
    # the SOL actually spent is native plus wrapped SOL: a buyer that wraps 5000 SOL and keeps the unspent rest in
    # its WSOL account loses 5000 native lamports but spends 1 SOL; one that wrapped earlier pays only in WSOL
    for w, d in _owner_deltas(meta, WSOL).items():
        lam[w] = lam.get(w, 0.0) - d
    out = []
    for owner, d in _owner_deltas(meta, mint).items():
        # a buyer pays SOL from its own account; the curve, the pool vaults and the migration gain tokens without
        # paying (they receive SOL), so they are left out
        sol = lam.get(owner, 0.0)
        if d > 0 and owner and owner not in exclude and sol >= MIN_SOL:
            out.append((owner, d, sol))
    return out


def block_buyers(rpc, mint, create_slot=None, slots=SLOTS, max_tx=MAX_TX, exclude=()):
    """Rows for sniper_launch_wallets (role 'launch_block') of the first `slots` slots of `mint`."""
    sigs, before = [], None
    for _ in range(PAGES):
        opts = {"limit": 1000}
        if before:
            opts["before"] = before
        page = rpc("getSignaturesForAddress", [mint, opts]) or []
        sigs += page
        if len(page) < 1000:
            break
        before = page[-1]["signature"]
    sigs = sorted((s for s in sigs if not s.get("err") and s.get("slot") is not None), key=lambda s: s["slot"])
    if not sigs:
        return []
    s0 = create_slot or sigs[0]["slot"]
    rows = {}
    for s in [s for s in sigs if s0 <= s["slot"] < s0 + slots][:max_tx]:
        tx = rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": TX_VERSION}])
        for w, tokens, sol in tx_buyers(tx, mint, exclude):
            r = rows.setdefault(w, {"wallet": w, "role": "launch_block", "slot": s["slot"], "ts": s.get("blockTime"),
                                    "tokens": 0.0, "sol": 0.0})
            r["tokens"] += tokens
            r["sol"] += sol
    return list(rows.values())
