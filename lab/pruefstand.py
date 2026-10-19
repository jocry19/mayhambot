"""Evaluate every hypothesis after costs and write the report (data/lab/report.json, report.md).

A pocket carries ("traegt") only if, after costs, it is positive in each of three equal time thirds, has at least
MIN_N entries on MIN_COINS coins, and its mean over coins (each coin averaged first, so one coin with many entries
counts once) is at least MIN_T standard errors above zero. "Kandidat": positive overall and in two of three thirds.
Everything else "traegt nicht". Only a carrying pocket should get stake in the main account.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import costs, hypotheses

log = logging.getLogger("lab")

MIN_N = 200
MIN_COINS = 50
MIN_T = 3.0
WINDOW_H = float(os.getenv("LAB_WINDOW_H", "72"))
STAKE_SOL = float(os.getenv("LAB_STAKE_SOL", "0.1"))


def connect_ro(path: str) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn
    except sqlite3.Error:
        return sqlite3.connect(path, timeout=30)


def net_of_costs(E: pd.DataFrame, stake_sol: float = STAKE_SOL) -> pd.DataFrame:
    E = E.copy()
    E["cost"] = [costs.roundtrip(d, l, stake_sol, k) for d, l, k in zip(E.dex, E.liq, E.kind)]
    E["net"] = np.where(E.gross <= -1.0, -1.0, E.gross - E.cost)
    return E


def brain_kind(hyp: str) -> str:
    """The brain's opportunity kind a pocket stands for (brain/trader.py frees main-account buys per kind):
    label_* rows are the pool's opportunities, launch_* the sniper's launches, signal_* the signal calls and the funnel's
    rejects, the price-panel and migration families coins the market kind sees."""
    if hyp.startswith("label_"):
        return "pool"
    if hyp.startswith("launch_"):
        return "launch"
    if hyp.startswith("signal_"):
        return "signal"  # a kind of its own: a carrying signal pocket must not free every market buy
    return "market"


def evaluate(E: pd.DataFrame) -> list[dict]:
    out = []
    for hyp, g in E.groupby("hyp"):
        g = g.sort_values("t")
        n, coins = len(g), g.mint.nunique()
        third = np.array_split(g.net.to_numpy(), 3) if n >= 3 else [g.net.to_numpy()]
        thirds = [float(x.mean()) if len(x) else float("nan") for x in third]
        per_coin = g.groupby("mint").net.mean()
        se = float(per_coin.std(ddof=1) / math.sqrt(len(per_coin))) if len(per_coin) > 1 else float("nan")
        mean_c = float(per_coin.mean())
        t = mean_c / se if se and se > 0 else float("nan")
        pos = sum(1 for x in thirds if x > 0)
        if n >= MIN_N and coins >= MIN_COINS and pos == 3 and t >= MIN_T:
            verdict = "traegt"
        elif mean_c > 0 and pos >= 2:
            verdict = "kandidat"
        else:
            verdict = "traegt nicht"
        out.append({"hyp": hyp, "brain_kind": brain_kind(hyp),
                    "source": hyp[6:].rsplit("_", 1)[0].removeprefix(hypotheses.LABEL_TOP + "_").removeprefix(hypotheses.LABEL_POOL + "_")
                    if hyp.startswith("label_") else None,
                    "n": n, "coins": int(coins), "gross": round(float(g.gross.mean()), 4),
                    "cost": round(float(g.cost.mean()), 4), "net": round(float(g.net.mean()), 4),
                    "net_per_coin": round(mean_c, 4), "se": round(se, 4) if se == se else None,
                    "t": round(t, 2) if t == t else None, "thirds": [round(x, 4) for x in thirds],
                    "rug": round(float((g.gross <= -1.0).mean()), 4), "verdict": verdict,
                    "from": int(g.t.min()), "to": int(g.t.max())})
    order = {"traegt": 0, "kandidat": 1, "traegt nicht": 2}
    return sorted(out, key=lambda r: (order[r["verdict"]], -r["net_per_coin"]))


def report_md(res: list[dict], info: dict, at: float) -> str:
    lines = [f"# Prüfstand {time.strftime('%d.%m.%Y %H:%M', time.localtime(at))}", "",
             f"Fenster {WINDOW_H:g} h, Einsatz {STAKE_SOL:g} SOL, {info.get('coins', 0)} Coins mit Kursreihe, "
             f"{len(res)} Hypothesen. Freigabe: nach Kosten in allen drei Zeitdritteln positiv, n ≥ {MIN_N}, "
             f"≥ {MIN_COINS} Coins, t ≥ {MIN_T:g} (je Coin gemittelt).", "",
             "| Hypothese | n | Coins | brutto | Kosten | netto je Coin | t | Drittel | Rug | Urteil |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in res:
        th = " / ".join(f"{x * 100:+.1f}" for x in r["thirds"])
        lines.append(f"| {r['hyp']} | {r['n']} | {r['coins']} | {r['gross'] * 100:+.1f} % | {r['cost'] * 100:.1f} % | "
                     f"{r['net_per_coin'] * 100:+.1f} % | {r['t'] if r['t'] is not None else '–'} | {th} | "
                     f"{r['rug'] * 100:.0f} % | {r['verdict']} |")
    errs = {k: v for k, v in info.items() if k.startswith("error_")}
    if errs:
        lines += ["", "Fehler: " + "; ".join(f"{k[6:]}: {v}" for k, v in errs.items())]
    return "\n".join(lines) + "\n"


def run_once(db_path: str, out_dir: Path, now: float | None = None) -> list[dict]:
    now = now or time.time()
    conn = connect_ro(db_path)
    try:
        E, info = hypotheses.all_entries(conn, int(now - WINDOW_H * 3600))
    finally:
        conn.close()
    res = evaluate(net_of_costs(E)) if len(E) else []
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "report.json.tmp"
    tmp.write_text(json.dumps({"at": int(now), "window_h": WINDOW_H, "stake_sol": STAKE_SOL, "info": info,
                               "results": res}, ensure_ascii=False, indent=1))
    tmp.replace(out_dir / "report.json")
    (out_dir / "report.md").write_text(report_md(res, info, now), encoding="utf-8")
    carry = [r["hyp"] for r in res if r["verdict"] == "traegt"]
    cand = [r["hyp"] for r in res if r["verdict"] == "kandidat"]
    log.info("lab: %d hypotheses, carries: %s, candidates: %s", len(res), ", ".join(carry) or "none",
             ", ".join(cand[:5]) or "none")
    return res
