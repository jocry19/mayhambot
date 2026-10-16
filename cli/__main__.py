"""mayhambot im Terminal.

    python -m cli                      Live-Oberfläche (Textual)
    python -m cli status               Kurzbericht: Status, Signale, Positionen
    python -m cli signals [-n 20]      Letzte Signale mit Gewichtung
    python -m cli wallets [--tier ultra] [-n 30]
    python -m cli positions            Offene und letzte Trades
    python -m cli mode whale|signal off|manual|semi|auto
    python -m cli pause on|off
    python -m cli preset whale|signal NAME
    python -m cli sell POSITION_ID
    python -m cli buy MINT [--sol 0.5]

Liest die gemeinsame Datenbank (MAYHAM_DB_PATH oder core.config). Schreibt nur Steuerwerte über
copytrade.control; der Echtgeld-Hauptschalter in .env wird nie angefasst oder angezeigt.
"""
from __future__ import annotations

import argparse
import sys

try:  # redact api keys in any log line, before anything makes a request
    import core.http  # type: ignore  # noqa: F401
except Exception:
    pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cli", description="mayhambot im Terminal")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("tui", help="Live-Oberfläche (Standard)")
    sub.add_parser("status", help="Kurzbericht")
    p = sub.add_parser("signals", help="Letzte Signale")
    p.add_argument("-n", type=int, default=15)
    p = sub.add_parser("wallets", help="Rangliste Wallets")
    p.add_argument("-n", type=int, default=25)
    p.add_argument("--tier", choices=["ultra", "smart", "good"])
    sub.add_parser("positions", help="Offene und letzte Trades")
    p = sub.add_parser("mode", help="Betriebsart setzen")
    p.add_argument("strategy", choices=["whale", "signal"])
    p.add_argument("mode", choices=["off", "manual", "semi", "auto"])
    p = sub.add_parser("pause", help="Neue Käufe pausieren")
    p.add_argument("state", choices=["on", "off"])
    p = sub.add_parser("preset", help="Preset für einen Modus wählen")
    p.add_argument("strategy", choices=["whale", "signal"])
    p.add_argument("name")
    p = sub.add_parser("sell", help="Offene Position verkaufen")
    p.add_argument("position_id", type=int)
    p = sub.add_parser("buy", help="Coin kaufen (Auftrag an den Bot)")
    p.add_argument("mint")
    p.add_argument("--sol", type=float, default=None)
    a = ap.parse_args(argv)

    if a.cmd in (None, "tui"):
        if not sys.stdout.isatty():
            from . import report
            report.status()
            return 0
        from .tui import run
        run()
        return 0

    from . import data, report
    try:
        if a.cmd == "status":
            report.status()
        elif a.cmd == "signals":
            report.signals(a.n)
        elif a.cmd == "wallets":
            report.wallets(a.n, a.tier)
        elif a.cmd == "positions":
            report.positions()
        elif a.cmd == "mode":
            print(data.set_mode(a.strategy, a.mode))
        elif a.cmd == "pause":
            print(data.set_paused(a.state == "on"))
        elif a.cmd == "preset":
            print(data.use_preset(a.strategy, a.name))
        elif a.cmd == "sell":
            print(data.sell(a.position_id))
        elif a.cmd == "buy":
            print(data.buy(a.mint, a.sol))
    except (ValueError, ImportError) as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
