"""German number and time formatting for the terminal (mirrors the dashboard)."""
from __future__ import annotations

import time

TIER_LABEL = {"ultra": "Ultra Smart", "smart": "Smart", "good": "Good"}
TIER_STYLE = {"ultra": "bold #9ec5f4", "smart": "#5598e7", "good": "#2a78d6"}
MODE_SHORT = {"off": "Aus", "manual": "Manuell", "semi": "Halbauto", "auto": "Auto"}
STRAT_SHORT = {"whale": "Wal-Pumps", "signal": "Signale", "manual": "Manuell"}
EXIT_LABEL = {
    "target": "Ziel erreicht", "inflow_peak": "Copy-Ansturm vorbei", "trail": "Trailing-Stop", "stop": "Stop-Loss",
    "time": "Zeit abgelaufen", "whale": "Wal verkauft", "no_market": "kein Markt", "smart_exit": "Smart Money raus",
    "manual": "von dir verkauft",
}


def _de(s: str) -> str:
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def n(v, d=0) -> str:
    if v is None:
        return "–"
    return _de(f"{v:,.{d}f}")


def compact(v, d=1) -> str:
    if v is None:
        return "–"
    a = abs(v)
    for div, suf in ((1e9, " Mrd"), (1e6, "M"), (1e3, "k")):
        if a >= div:
            return _de(f"{v / div:.{d}f}") + suf
    return _de(f"{v:.{0 if a >= 100 else 2}f}")


def usd(v, d=1) -> str:
    return "–" if v is None else ("-$" if v < 0 else "$") + compact(abs(v), d)


def usd_signed(v, d=1) -> str:
    return "–" if v is None else ("+" if v > 0 else "-" if v < 0 else "") + "$" + compact(abs(v), d)


def sol(v, d=2, signed=False) -> str:
    if v is None:
        return "–"
    s = n(v, d)
    return (("+" + s) if signed and v > 0 else s) + " SOL"


def pct(v, d=0, signed=False) -> str:
    if v is None:
        return "–"
    s = n(v * 100, d) + " %"
    return ("+" + s) if signed and v > 0 else s


def ago(ts) -> str:
    if not ts:
        return "–"
    s = max(0, time.time() - ts)
    if s < 60:
        return f"vor {int(s)} s"
    if s < 3600:
        return f"vor {int(s // 60)} min"
    if s < 86400:
        return f"vor {int(s // 3600)} h {int(s % 3600 // 60)} min"
    return f"vor {int(s // 86400)} T"


def clock(ts) -> str:
    if not ts:
        return "–"
    lt = time.localtime(ts)
    if time.strftime("%Y%m%d", lt) == time.strftime("%Y%m%d"):
        return time.strftime("%H:%M", lt)
    return time.strftime("%d.%m. %H:%M", lt)


def tone(v) -> str:
    """Rich style for a profit/loss number."""
    if v is None or v == 0:
        return ""
    return "green" if v > 0 else "red"


def tiers(t: dict | None) -> str:
    t = t or {}
    return f"[{TIER_STYLE['ultra']}]{t.get('ultra', 0)}U[/] [{TIER_STYLE['smart']}]{t.get('smart', 0)}S[/] [{TIER_STYLE['good']}]{t.get('good', 0)}G[/]"


def short(addr: str | None, k=4) -> str:
    return "–" if not addr else f"{addr[:k]}…{addr[-k:]}"
