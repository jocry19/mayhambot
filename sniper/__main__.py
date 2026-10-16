"""python -m sniper run | watch [--minutes N] [--db PATH] [--record FILE] | replay FILE [--db PATH]
                  | stats [--hours H] | list [-n N] [--entered] | show MINT | wallet ADDRESS | reset-paper"""
import argparse
import json
import logging
import sys
import time

from sniper import analytics as st
from sniper import store
from sniper.config import settings


def _pct(v):
    return "   –  " if v is None else f"{100 * v:+6.0f}%"


def _print_stats(s):
    if not s.get("available"):
        print("Noch keine Sniper-Daten.")
        return
    span = "Alle Launches" if not s["hours"] else f"Letzte {round(s['hours'], 1)} h"
    print(f"{span}: {s['launches']} Launches, {s['stage2']} tief geprüft, {s['entered']} Papier-Käufe "
          f"(Bundle {s['bundle_flagged']}, Insider {s['insider_flagged']})")
    c = s["calls_per_deep_launch"]
    print(f"Kosten pro tief geprüftem Launch: {c['rpc']} RPC-Calls, {c['pump_api']} API-/Metadaten-Calls")
    print(f"\n{'Gruppe':<22}{'n':>6}{'1m':>8}{'5m':>8}{'15m':>8}{'60m':>8}{'max60':>8}{'2x':>7}{'Rug':>7}")
    for name, g in (("gekauft", s["groups"]["entered"]), ("tief geprüft, skip", s["groups"]["skipped_deep"]),
                    ("Stufe 1 skip", s["groups"]["skipped_stage1"]), ("alle", s["groups"]["all"])):
        print(f"{name:<22}{g['n']:>6}{_pct(g['median_ret_1m'])}{_pct(g['median_ret_5m'])}{_pct(g['median_ret_15m'])}"
              f"{_pct(g['median_ret_60m'])}{_pct(g['median_max_ret_60m'])}{_pct(g['hit_rate_2x'])}{_pct(g['rug_rate'])}")
    print(f"\n{'Filter':<44}{'n':>6}{'verpasste 2x':>14}{'vermiedene Rugs':>17}")
    for f in s["filters"]:
        print(f"{f['label']:<44}{f['n']:>6}{f['missed_winners']:>14}{f['avoided_rugs']:>17}")
    p = s["paper"]
    if p:
        print(f"\nPapier: Guthaben {p['balance_sol']} SOL, {p['n_closed']} geschlossen, {p['n_open']} offen, "
              f"Trefferquote {_pct(p['win_rate'])}, PnL {p['pnl_sol']:+.4f} SOL, Schnitt {_pct(p['avg_pnl_pct'])}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m sniper")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="Dauerbetrieb (Dienst 'sniper')")
    p = sub.add_parser("watch", help="im Vordergrund beobachten, z. B. mit eigener Test-Datenbank")
    p.add_argument("--minutes", type=float, default=10)
    p.add_argument("--db")
    p.add_argument("--record", help="rohe Websocket-Nachrichten in diese Datei (für replay)")
    p = sub.add_parser("replay", help="aufgezeichnete Nachrichten offline durchspielen")
    p.add_argument("file")
    p.add_argument("--db")
    p = sub.add_parser("stats")
    p.add_argument("--hours", type=float, default=24)
    p.add_argument("--db")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("list")
    p.add_argument("-n", type=int, default=30)
    p.add_argument("--entered", action="store_true")
    p.add_argument("--deep", action="store_true")
    p.add_argument("--db")
    p = sub.add_parser("show")
    p.add_argument("mint")
    p.add_argument("--db")
    p = sub.add_parser("wallet")
    p.add_argument("address")
    p.add_argument("--db")
    p = sub.add_parser("reset-paper")
    p.add_argument("--db")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for noisy in ("urllib3", "requests", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    db_path = getattr(a, "db", None)

    if a.cmd == "run":
        from sniper.service import run
        run(settings)
    elif a.cmd == "watch":
        from sniper.service import run
        svc = run(settings, db_path=db_path, duration=a.minutes * 60, record=a.record)
        _print_stats(st.stats(svc.conn, hours=round(a.minutes / 60, 2)))
    elif a.cmd == "replay":
        from sniper.service import replay
        eng, n, _ = replay(settings, a.file, db_path)
        print(f"{n} Nachrichten durchgespielt")
        _print_stats(st.stats(eng.conn, hours=0))
    else:
        conn = store.connect(db_path)
        if a.cmd == "stats":
            s = st.stats(conn, hours=a.hours)
            print(json.dumps(s, indent=1, default=str)) if a.json else _print_stats(s)
        elif a.cmd == "list":
            for r in st.recent(conn, a.n, decision="enter" if a.entered else None, stage=2 if a.deep else None):
                print(f"{time.strftime('%d.%m %H:%M:%S', time.localtime(r['decided_ts']))} {r['mint'][:8]} "
                      f"{(r['symbol'] or '')[:10]:<10} St{r['stage']} {r['decision']:<5} {r['skip_label'] or '':<36} "
                      f"Score {r['score'] or 0:>4.0f} Risiko {r['risk'] or 0:>3.0f} 5m {_pct(r['ret_5m'])} "
                      f"60m {_pct(r['ret_60m'])} max {_pct(r['max_ret_60m'])}")
        elif a.cmd == "show":
            d = st.launch(conn, a.mint)
            if not d:
                print("unbekannt")
                return 1
            print(f"{d['symbol']} {d['mint']}  Stufe {d['stage']}  {d['decision']}  {d['skip_label'] or ''}")
            print(f"Score {d['score']}  Momentum {d['momentum']}  Risiko {d['risk']}  Bundle {d['bundle_pct']} % "
                  f"({d['bundle_wallets']} Wallets)  Dev {d['dev_buy_pct']} %  Top10 {d['top10_pct']} %")
            for r in d["reasons"]:
                print(f"  [{r['risk']:>4.0f}] {r['text']}")
            for p in d["positives"]:
                print(f"  [  ok] {p}")
            print("Ergebnis:", ", ".join(f"{h} {_pct(d[f'ret_{h}'])}" for h in ("1m", "5m", "15m", "60m")),
                  f"max {_pct(d['max_ret_60m'])}")
            for w in d["wallets"][:20]:
                print(f"  {w['role']:<8} {w['wallet']}  {w['tokens'] or 0:>14,.0f}  Geldquelle {w['funder'] or '–'}  "
                      f"verkauft {w['sold_pct'] if w['sold_pct'] is not None else '–'} %")
        elif a.cmd == "wallet":
            for r in st.wallet_history(conn, a.address):
                print(f"{r['mint'][:8]} {r['symbol'] or '':<10} {r['role']:<8} 60m {_pct(r['ret_60m'])} "
                      f"{'RUG' if r['rugged'] else ''}")
        elif a.cmd == "reset-paper":
            conn.execute("DELETE FROM sniper_positions")
            conn.execute("DELETE FROM sniper_state WHERE key='paper_balance_sol'")
            conn.commit()
            print("Papierkonto des Snipers zurückgesetzt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
