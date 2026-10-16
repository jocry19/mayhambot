"""python -m intel run | once | coin <MINT> | wallet <ADDR> | market | events [-n 30] | eval [--days 14] [--json]"""
import argparse
import json
import logging
import time


def _v(v):
    if v is None:
        return "–"
    return f"{v:,.4g}" if isinstance(v, float) else str(v)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m intel")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="Dauerbetrieb (Launcher-Dienst intel)")
    sub.add_parser("once", help="eine Runde")
    for k in ("coin", "wallet"):
        p = sub.add_parser(k)
        p.add_argument("key")
        p.add_argument("--ts", type=int)
    sub.add_parser("market")
    p = sub.add_parser("events")
    p.add_argument("-n", type=int, default=30)
    p = sub.add_parser("eval")
    p.add_argument("--days", type=float, default=14)
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.cmd in ("run", "once"):
        from intel.service import Service
        s = Service(db_path=a.db)
        if a.cmd == "run":
            s.run()
        else:
            s.last["coins"] = s.last["wallets"] = s.last["persist"] = 0
            print(json.dumps(s.round(), ensure_ascii=False))
        return
    import intel
    from intel import store
    conn = store.connect(a.db)
    if a.cmd in ("coin", "wallet", "market"):
        r = intel.features_at(a.cmd, getattr(a, "key", "solana"), getattr(a, "ts", None) or int(time.time()), conn)
        labels = intel.FEATURES[a.cmd]
        for k, v in r["features"].items():
            print(f"  {labels[k][1]:<44} {_v(v)}")
        print("Abdeckung:", ", ".join(f"{g} {100 * c:.0f} %" for g, c in r["coverage"].items()))
    elif a.cmd == "events":
        for r in conn.execute("SELECT ts, title, text FROM intel_events ORDER BY ts DESC LIMIT ?", (a.n,)):
            print(time.strftime("%H:%M", time.localtime(r[0])), r[1], "·", r[2])
    elif a.cmd == "eval":
        ev = intel.evaluation(conn, days=a.days)
        if a.json:
            print(json.dumps(ev, ensure_ascii=False, indent=1, default=str))
            return
        print(ev["note"])
        for r in ev["features"]:
            if r.get("z") is None:
                continue
            print(f"  {'KLAR ' if r['clear'] else '     '}{r['label']:<40} n={r['n']:>5}  IC {r['ic'] or 0:+.3f}  "
                  f"oben {100 * r['hit_top']:.1f} % / unten {100 * r['hit_bottom']:.1f} %  z {r['z']:+.1f}")


if __name__ == "__main__":
    main()
