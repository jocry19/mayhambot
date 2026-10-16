"""python -m market run | once | backfill [--hours 24] | status | coin <MINT> | sectors | terms | eval [--days 14] [--json]"""
import argparse
import json
import logging

from market import context, evaluate, narratives, store
from market.analysis import LABELS
from market.config import settings


def _pct(x, d=1):
    return "–" if x is None else f"{100 * x:+.{d}f} %"


def _r(x, d=0):
    return "–" if x is None else f"{100 * x:.{d}f} %"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m market")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="Dauerbetrieb (Launcher-Dienst market)")
    sub.add_parser("once", help="eine Runde mit Abruf")
    p = sub.add_parser("backfill")
    p.add_argument("--hours", type=int, default=24)
    sub.add_parser("status")
    p = sub.add_parser("coin")
    p.add_argument("mint")
    sub.add_parser("sectors")
    sub.add_parser("terms")
    p = sub.add_parser("eval")
    p.add_argument("--days", type=float, default=14)
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.cmd in ("run", "once", "backfill"):
        from market.service import Service
        s = Service(settings, a.db)
        if a.cmd == "run":
            s.run()
        elif a.cmd == "once":
            s.round()
        else:
            s.backfill(a.hours)
        return
    conn = store.connect(a.db)
    if a.cmd == "status":
        p = context.phase(conn)
        if not p:
            print("Noch kein Markt-Snapshot.")
            return
        print(f"Markt {p['label_de']} {p['score']:.0f}/100 (Abdeckung {100 * (p['coverage'] or 0):.0f} %, "
              f"vor {p['age_s'] // 60} min)  SOL 24h {_pct(p.get('sol_ret_24h'))}  Breite {_r(p.get('breadth'))}")
        for k, c in sorted(p["components"].items(), key=lambda kv: -kv[1]["points"]):
            print(f"  {c['points']:>5.1f} P  {c['value']:.2f} × {c['weight']:.2f}  {c['why']}")
        print(f"Top-Sektor {narratives.SECTOR_LABEL.get(p.get('top_sector'), '–')} · Narrativ {p.get('top_term') or '–'}")
    elif a.cmd == "coin":
        print(json.dumps(context.coin_context(conn, a.mint), ensure_ascii=False, indent=1, default=str))
    elif a.cmd == "sectors":
        ts = conn.execute("SELECT MAX(ts) FROM market_sectors").fetchone()[0]
        for r in conn.execute("SELECT * FROM market_sectors WHERE ts=? ORDER BY heat DESC", (ts,)):
            print(f"{narratives.SECTOR_LABEL.get(r['sector'], r['sector']):<20} Hitze {r['heat'] or 0:>5.1f}  "
                  f"{r['n_tokens']:>4} Coins  Anteil {_r(r['vol_share'], 1)}  1h-Median {r['median_ret_h1'] or 0:+.1f} %  "
                  f"Smart netto ${r['smart_net_usd'] or 0:,.0f}  Launches {r['launches']}")
    elif a.cmd == "terms":
        ts = conn.execute("SELECT MAX(ts) FROM market_terms").fetchone()[0]
        for r in conn.execute("SELECT * FROM market_terms WHERE ts=? ORDER BY heat DESC", (ts,)):
            print(f"{r['term']:<24} Hitze {r['heat']:>5.1f}  {r['n_recent']:>4} Launches  Lift {r['lift']:>5.1f}x  "
                  f"2x-Rate {_r(r['hit2x'])}  Volumen ${r['volume_h1_usd'] or 0:,.0f}")
    elif a.cmd == "eval":
        ev = evaluate.evaluate(conn, days=a.days)
        if a.json:
            print(evaluate.to_json(ev))
            return
        print(ev.get("note"))
        la = ev.get("launches") or {}
        for lens, title in (("by_phase", "nach Marktphase"), ("by_sector_heat", "nach Sektor-Hitze"), ("by_term", "nach Narrativ")):
            print(f"\nLaunches {title} (Basis {_r(la.get('base_hit_rate'))} mit 2x):")
            for k, v in (la.get(lens) or {}).items():
                print(f"  {k:<24} n={v['n']:>5}  2x {_r(v['hit_rate'], 1)}  Rug {_r(v.get('rug_rate'), 1)}")
            s = (ev.get("significant") or {}).get(lens)
            if s:
                print(f"  -> {'klar' if s['clear'] else 'nicht klar'}: {s['text']}")
        print("\n(Phasen:", ", ".join(LABELS.values()) + ")")


if __name__ == "__main__":
    main()
