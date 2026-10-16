"""Copy-trader command line. Run from the mayhambot/ folder:

  python -m copytrade run                 # paper mode (default), loops forever
  python -m copytrade discover            # find whale wallets on hot tokens + load their history
  python -m copytrade rank                # evaluate whale buys with candles and rank wallets
  python -m copytrade add <wallet> [label]  # follow a wallet manually
  python -m copytrade stats               # paper/live results + top wallets
  python -m copytrade backtest            # walk-forward estimate from ranked history
  python -m copytrade learn [on|off <whale|signal>]   # one learning round + what the bot has learned
  python -m copytrade reset-paper         # wipe paper positions and balance
  python -m copytrade status              # modes, presets, open positions
  python -m copytrade mode <whale|signal> <off|manual|semi|auto>
  python -m copytrade preset <whale|signal> <name>      # vorsichtig | standard | aggressiv | own
  python -m copytrade live <whale|signal> <on|off>      # needs LIVE_TRADING=true + key in .env
  python -m copytrade pause | resume
  python -m copytrade buy <mint> [size_sol]             # manual buy (you sell)
  python -m copytrade sell <position_id>
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import chain, control, learn, ranking, stats, store, tracker
from .config import load


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="copytrade")
    ap.add_argument("cmd", choices=["run", "discover", "rank", "add", "stats", "backtest", "learn", "reset-paper", "status", "mode",
                                       "preset", "live", "pause", "resume", "buy", "sell"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    s = load()
    conn = store.connect(s)

    if a.cmd == "run":
        from .engine import Engine

        if s.live_trading and not s.live_ready:
            print("LIVE_TRADING=true, aber kein Schlüssel (SOLANA_PRIVATE_KEY / SOLANA_KEYPAIR_PATH): bleibe im Papiermodus.")
        if s.live_ready:
            print("!!! LIVE-MODUS: echte Swaps mit echtem Geld. Abbrechen mit Ctrl+C. !!!")
        Engine(s).run()
    elif a.cmd == "discover":
        rpc = chain.Rpc(s)
        tracker.load_manual(conn, s)
        tracker.load_core_smart(conn)
        print("neue Wallets:", tracker.discover(conn, s, rpc))
        for w in tracker.active_wallets(conn)[:12]:
            if not conn.execute("SELECT 1 FROM ct_swaps WHERE wallet=? LIMIT 1", (w["address"],)).fetchone():
                print(w["address"], "swaps:", tracker.backfill(conn, s, rpc, w["address"]))
    elif a.cmd == "rank":
        n = ranking.evaluate_pending(conn, s, max_mints=int(a.args[0]) if a.args else 25)
        k = ranking.rank_wallets(conn, s)
        print(f"{n} Wal-Käufe ausgewertet, {k} Wallets bewertet")
        _print_wallets(conn)
    elif a.cmd == "add":
        if not a.args:
            ap.error("add <wallet> [label]")
        store.add_wallets(conn, [a.args[0]], "manual", " ".join(a.args[1:]))
        conn.execute("UPDATE ct_wallets SET source='manual', active=1 WHERE address=?", (a.args[0],))
        conn.commit()
        print("folge", a.args[0])
    elif a.cmd == "stats":
        for mode in ("paper", "live"):
            for st in ("whale", "signal", "manual"):
                sm = stats.summary(stats.positions(conn, mode, st))
                if sm.get("trades") or sm.get("open"):
                    print(mode.upper(), st, {k: round(v, 4) if isinstance(v, float) else v for k, v in sm.items()})
        _print_wallets(conn)
    elif a.cmd == "backtest":
        bt = stats.backtest(conn, s)
        if bt.empty:
            print("noch keine bewerteten Käufe: erst `discover` und `rank` laufen lassen")
        else:
            wins = (bt["gain"] > 0).mean()
            print(f"{len(bt)} kopierte Käufe, Trefferquote {wins:.0%}, PnL {bt['pnl_sol'].sum():+.2f} SOL "
                  f"bei {s.position_size_sol} SOL pro Trade")
    elif a.cmd == "learn":
        if len(a.args) == 2 and a.args[0] in ("on", "off") and a.args[1] in control.STRATEGIES:
            control.set_value(conn, f"autotune:{a.args[1]}", "1" if a.args[0] == "on" else "0")
            print(f"Selbstlernen {a.args[1]}: {a.args[0]}")
            return 0
        res = learn.run(conn, s)
        print(f"neu ausgewertet: {res['trades']} Trades, {res['whale_buys']} Wal-Käufe, {res['signals']} Signale")
        for k, v in (res["tuned"] or {}).items():
            if v:
                print(f"  {k}: {'ÜBERNOMMEN' if v['accepted'] else 'verworfen'} "
                      f"Test {v['test_gain']:+.1%} vs. bisher {v['baseline']:+.1%} pro Trade -> {v['params']}")
        for strat, st in learn.status(conn).items():
            act = st["active_version"]
            print(f"{strat}: Lernen {'an' if st['autotune'] else 'aus'}, {st['total']} Einstiege ausgewertet "
                  f"{st['evaluated']}" + (f", noch {st['needed_for_tuning']} bis zum ersten Tuning"
                                          if st["needed_for_tuning"] else "")
                  + (f", aktiv: Version {act['version']}" if act else ", aktiv: dein Preset")
                  + (f", {st['wallet_versions']} Wal-spezifische Versionen" if st["wallet_versions"] else ""))
    elif a.cmd == "reset-paper":
        conn.execute("DELETE FROM ct_positions WHERE mode='paper'")
        conn.execute("DELETE FROM ct_equity WHERE mode='paper'")
        conn.execute("DELETE FROM ct_state WHERE key='paper_balance_sol'")
        conn.commit()
        print("Papierkonto zurückgesetzt auf", s.paper_start_balance_sol, "SOL")
    elif a.cmd == "status":
        print("Live-Hauptschalter (.env):", "AN" if s.live_ready else "aus", "| pausiert:", control.paused(conn))
        for st in control.STRATEGIES:
            live = "LIVE" if (s.live_ready and control.live_wanted(conn, st)) else "Papier"
            print(f"{control.STRATEGY_LABELS[st]}: {control.MODE_LABELS[control.mode(conn, st)]}, "
                  f"Preset {control.get(conn, 'preset:' + st)}, {live}")
        df = stats.positions(conn)
        op = df[df.status == "open"] if len(df) else df
        for r in op.itertuples():
            print(f"  #{r.id} {r.symbol} [{r.strategy}/{r.mode}/{r.managed}] {r.current_pct:+.1%}"
                  + (f"  HINWEIS: {r.exit_hint}" if r.exit_hint else ""))
    elif a.cmd in ("mode", "preset", "live"):
        if len(a.args) != 2:
            ap.error(f"{a.cmd} <whale|signal> <wert>")
        st, val = a.args
        if a.cmd == "mode":
            control.set_mode(conn, st, val)
        elif a.cmd == "preset":
            control.use_preset(conn, st, val)
        else:
            if val == "on" and not s.live_ready:
                print("Achtung: LIVE_TRADING=true und ein Schlüssel fehlen in .env, es bleibt beim Papiermodus.")
            control.set_live(conn, st, val == "on")
        print("ok")
    elif a.cmd in ("pause", "resume"):
        control.set_paused(conn, a.cmd == "pause")
        print("pausiert" if a.cmd == "pause" else "läuft")
    elif a.cmd == "buy":
        if not a.args:
            ap.error("buy <mint> [size_sol]")
        print("Auftrag", control.request_buy(conn, a.args[0], float(a.args[1]) if len(a.args) > 1 else None),
              "(führt die laufende App aus)")
    elif a.cmd == "sell":
        if not a.args:
            ap.error("sell <position_id>")
        print("Auftrag", control.request_sell(conn, int(a.args[0])), "(führt die laufende App aus)")
    return 0


def _print_wallets(conn) -> None:
    df = stats.wallet_table(conn)
    df = df[df["samples"].notna()].head(15)
    if df.empty:
        print("noch keine bewerteten Wallets")
        return
    cols = ["address", "samples", "hit_rate", "median_first_pump", "median_pump_min", "median_lead_min",
            "expected_value", "score", "take_profit"]
    print(df[cols].to_string(index=False, float_format=lambda x: f"{x:.2f}"))


if __name__ == "__main__":
    sys.exit(main())
