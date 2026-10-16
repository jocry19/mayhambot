"""python -m safety assess <MINT> [--quick] | refresh | learn | run | list | block <MINT> [grund] | allow <MINT> | clear <MINT>"""
import argparse
import logging
import time

from safety import engine, store
from safety.config import settings


def _print(a):
    print(f"{a.mint}  Risiko {a.score:.0f}/100  {engine.LEVEL_DE.get(a.level, a.level)}"
          f"{'  GESPERRT' if a.blocked else ''}  Abdeckung {a.coverage:.0%}  ({a.calls} API-Calls)")
    for r in a.reasons:
        print(f"  [{r['risk']:>4.0f}] {r['text']}")
    for p in a.positives:
        print(f"  [  ok] {p}")
    for name, c in a.checks.items():
        if c["status"] != "ok":
            print(f"  ({c['label']}: {c['status']})")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m safety")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("assess")
    p.add_argument("mint")
    p.add_argument("--quick", action="store_true", help="ohne Launch-, Dev- und Insider-Analyse")
    sub.add_parser("refresh")
    sub.add_parser("learn", help="Ausgänge auswerten und Gewichte der Checks neu lernen (ohne Netz)")
    sub.add_parser("run", help="Dauerschleife: alle SAFETY_REFRESH_EVERY_SEC Sekunden ein Refresh")
    p = sub.add_parser("list")
    p.add_argument("-n", type=int, default=30)
    for c in ("block", "allow", "clear"):
        p = sub.add_parser(c)
        p.add_argument("mint")
        p.add_argument("reason", nargs="?", default="")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    conn = store.connect()
    if a.cmd == "assess":
        _print(engine.assess(a.mint, conn, deep=not a.quick, force=True))
    elif a.cmd == "learn":
        from safety import learn
        print(learn.report_text(learn.learn(conn)))
    elif a.cmd == "refresh":
        for x in engine.refresh(conn):
            _print(x)
    elif a.cmd == "run":
        while True:
            done = engine.refresh(conn)
            logging.info("safety refresh: %d coins geprüft", len(done))
            time.sleep(settings.refresh_every_sec)
    elif a.cmd == "list":
        for r in conn.execute("SELECT mint, score, level, blocked, ts FROM safety_tokens ORDER BY ts DESC LIMIT ?", (a.n,)):
            print(f"{r['mint']}  {r['score']:>5.1f}  {engine.LEVEL_DE.get(r['level'], r['level']):<9}"
                  f"{' GESPERRT' if r['blocked'] else ''}  {time.strftime('%d.%m %H:%M', time.localtime(r['ts']))}")
    elif a.cmd == "block":
        engine.block(conn, a.mint, a.reason or "manuell")
    elif a.cmd == "allow":
        engine.allow(conn, a.mint, a.reason or "manuell freigegeben")
    elif a.cmd == "clear":
        engine.clear_override(conn, a.mint)


if __name__ == "__main__":
    main()
