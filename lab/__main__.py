"""python -m lab once | run   (run: every LAB_EVERY_H hours, default 6)"""
import logging
import os
import signal
import sys
import threading
from pathlib import Path


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "once"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from core.config import ROOT, settings
    from lab import pruefstand

    out = Path(os.getenv("LAB_DIR") or ROOT / "data" / "lab")
    if cmd == "once":
        for r in pruefstand.run_once(settings.db_path, out):
            print(f"{r['verdict']:13s} {r['hyp']:42s} n={r['n']:6d} netto/Coin {r['net_per_coin'] * 100:+6.1f} % "
                  f"t={r['t']}")
        return 0
    if cmd == "run":
        every = float(os.getenv("LAB_EVERY_H", "6")) * 3600
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        while not stop.is_set():
            try:
                pruefstand.run_once(settings.db_path, out)
            except Exception:
                logging.getLogger("lab").exception("lab round failed")
            stop.wait(every)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
