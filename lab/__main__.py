"""python -m lab once | run   (run: every LAB_EVERY_H hours, default 6)"""
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

TRAINING_MAX_S = 90 * 60  # a TRAINING marker older than this is stale (deploy/ops.training)


def next_round(out: Path, db_path, every: float, now: float | None = None) -> float:
    """Seconds until the next round is due: the last report's age counts (a deploy restarts every service,
    so without it each restart would start a fresh round), and while the research service retrains the
    wallet model (data/TRAINING next to the DB) the round waits, at most 10 minutes at a time."""
    now = time.time() if now is None else now
    try:
        import json
        at = float(json.loads((out / "report.json").read_text()).get("at") or 0)
    except (OSError, ValueError):
        at = 0.0
    wait = max(0.0, at + every - now)
    try:
        age = now - (Path(db_path).parent / "TRAINING").stat().st_mtime
        if 0 <= age < TRAINING_MAX_S:
            wait = max(wait, 600.0)
    except OSError:
        pass
    return wait


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
        try:
            os.nice(10)  # pandas over 72 h of prices: the trading services come first on the Mac's CPU
        except OSError:
            pass
        while not stop.is_set():
            wait = next_round(out, settings.db_path, every)
            if wait > 0:
                stop.wait(wait)
                continue
            try:
                pruefstand.run_once(settings.db_path, out)
            except Exception:
                logging.getLogger("lab").exception("lab round failed")
                stop.wait(600)  # no report written: retry later, not in a tight loop
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
