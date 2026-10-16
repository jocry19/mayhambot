"""python -m research discover [--loop] | report | selftest"""
import argparse
import logging


def main():
    ap = argparse.ArgumentParser(prog="python -m research")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover", parents=[common], help="find and backfill good wallets (early buyers of pumped coins, top traders)")
    d.add_argument("--loop", action="store_true")
    sub.add_parser("report", parents=[common], help="print the latest research report")
    sub.add_parser("selftest", parents=[common], help="run the whole pipeline on a synthetic market (no network)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if a.cmd == "discover":
        from research import discovery
        if a.loop:
            discovery.loop()
        else:
            print(discovery.run_once())
    elif a.cmd == "report":
        from research.store import connect
        r = connect().execute("SELECT markdown FROM research_reports ORDER BY id DESC LIMIT 1").fetchone()
        print(r[0] if r else "Noch kein Report. Erst: python -m model retrain")
    elif a.cmd == "selftest":
        import os
        import tempfile
        from model import train
        from research import fixtures
        conn, truth, now = fixtures.build(os.path.join(tempfile.mkdtemp(), "selftest.db"))
        train.run_once(conn, now=now, write_files=False)
        print(conn.execute("SELECT markdown FROM research_reports ORDER BY id DESC LIMIT 1").fetchone()[0])


if __name__ == "__main__":
    main()
