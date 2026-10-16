"""python -m live <command>

  run                  live watchdog + Telegram Not-Aus (started by mayham.py as service 'live')
  status               mode, stage limits, open live/dry positions, go-live check
  check                go-live check in detail (exit code 0 = passed)
  setup                new trading wallet (or the existing one) + .env for the dry run in one step
  new-wallet [PFAD]    new separate trading wallet (keypair file, chmod 600); prints only the public key
  kill [GRUND]         Not-Aus: no more live buys
  resume               lift the Not-Aus, reset the failure streak
  orders [N]           last live/dry-run orders
  orphans              tokens in the trading wallet without an open position
  dryrun MINT [SOL]    one buy through the whole pipeline (quote, build, sign, simulate), never sent, not booked
"""
from __future__ import annotations

import argparse
import logging
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m live")
    ap.add_argument("cmd", choices=["run", "status", "check", "setup", "new-wallet", "kill", "resume", "orders", "orphans",
                                    "dryrun"])
    ap.add_argument("args", nargs="*")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    from . import readiness, service, store
    from .config import load

    if a.cmd == "new-wallet":
        from .wallet import DEFAULT_KEYFILE, new_wallet

        path = a.args[0] if a.args else str(DEFAULT_KEYFILE)
        try:
            pub = new_wallet(path)
        except FileExistsError as e:
            print(e)
            return 1
        print(f"Neue Trading-Wallet: {pub}\nSchlüsseldatei: {path} (nur für dich lesbar, nie teilen, nicht ins Repo)")
        print(f"In .env eintragen:  SOLANA_KEYPAIR_PATH={path}\nDann nur einen kleinen Betrag an {pub} schicken.")
        return 0

    if a.cmd == "setup":
        from .config import ROOT
        from .wallet import DEFAULT_KEYFILE, _from_text, new_wallet, pubkey_of, set_env

        path = DEFAULT_KEYFILE
        if path.exists():
            pub = pubkey_of(_from_text(path.read_text()))
            print(f"Trading-Wallet existiert schon: {pub}")
        else:
            pub = new_wallet(path)
            print(f"Neue Trading-Wallet: {pub}")
        set_env(ROOT / ".env", {"SOLANA_KEYPAIR_PATH": str(path), "LIVE_TRADING": "true", "LIVE_DRY_RUN": "true"})
        print("In .env gesetzt: SOLANA_KEYPAIR_PATH, LIVE_TRADING=true, LIVE_DRY_RUN=true (nur Dry-Run, sendet nichts).")
        print(f"Jetzt: .venv/bin/python mayham.py restart  und 1 SOL an {pub} schicken.")
        return 0

    cfg = load()
    svc = service.Service(cfg=cfg) if a.cmd in ("run", "orphans") else None
    if a.cmd == "run":
        svc.run()
        return 0
    from copytrade import store as ct_store  # type: ignore
    from copytrade.config import Settings  # type: ignore

    conn = svc.conn if svc else ct_store.connect(Settings())
    store.init(conn)
    if a.cmd == "status":
        print(service.status_text(conn, cfg))
        print("Grenzen:", ", ".join(f"{k}={v}" for k, v in cfg.limits().items()))
        return 0
    if a.cmd == "check":
        rep = readiness.report(conn, cfg)
        for c in rep["checks"]:
            print(f"{'✓' if c['ok'] else '✗'} {c['label']}{'' if c['required'] else ' (Info)'}: {c['detail']}")
        if rep["advance"]:
            v = rep["advance"]
            print(f"Nächste Stufe ({v['next']}): {v['round_trips']}/{v['min_trips']} Live-Runden, netto "
                  f"{v['net_sol']:+.3f} SOL, Slippage-Parität {'ok' if v['parity'] else 'offen'} -> "
                  f"{'bereit (LIVE_STAGE erhöhen)' if v['ok'] else 'noch nicht'}")
        print("GO-LIVE:", "bestanden" if rep["ready"] else "nicht bestanden")
        return 0 if rep["ready"] else 1
    if a.cmd == "kill":
        store.kill(conn, " ".join(a.args) or "von Hand")
        print("Not-Aus gesetzt.")
        return 0
    if a.cmd == "resume":
        store.unkill(conn)
        print("Not-Aus aufgehoben.")
        return 0
    if a.cmd == "orders":
        for o in store.recent(conn, int(a.args[0]) if a.args else 20):
            print(f"#{o['id']} {'DRY ' if o['dry'] else ''}{o['side']} {o['mint'][:8]}… {o['status']} "
                  f"impact={o['price_impact']} slip={o['realized_slippage']} {o['error'] or ''}")
        return 0
    if a.cmd == "orphans":
        for o in svc.orphans():
            print(o["mint"], o["amount"])
        return 0
    if a.cmd == "dryrun":
        if not a.args:
            print("python -m live dryrun MINT [SOL]")
            return 2
        from copytrade import chain  # type: ignore

        from .executor import GuardedLiveExecutor
        from .wallet import load_keypair, pubkey_of

        s = Settings()
        kp = load_keypair(s.keypair_path, s.private_key, cfg.keychain_service)
        pub = pubkey_of(kp) if kp else cfg.wallet_pubkey
        if not pub:
            print("Kein Schlüssel und keine LIVE_WALLET_PUBKEY in .env.")
            return 1
        rpc = chain.Rpc(s)
        ex = GuardedLiveExecutor(s, rpc, cfg, kp, pub, dry=True, conn=conn)
        size = min(float(a.args[1]) if len(a.args) > 1 else cfg.max_position_sol, cfg.max_position_sol)
        dec = chain.token_decimals(rpc, a.args[0])
        oid, r = ex._swap("buy", a.args[0], int(size * 1e9), cfg.buy_slippage_bps, 1, None, dec)
        print(f"Order #{oid}: {r['status']} {r.get('error') or ''}".strip())
        if r.get("quote"):
            print(f"Quote: {int(r['quote']['outAmount']) / 10 ** dec:,.2f} Token für {size} SOL, Preiseinfluss "
                  f"{r['impact']:.2%}, Priority-Fee {r.get('priority_lamports', 0) / 1e9:.6f} SOL")
        return 0 if r["status"] == "dry_ok" else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
