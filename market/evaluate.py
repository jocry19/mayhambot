"""Does the market analysis predict anything? Real outcomes (sniper launches after 60 min, signals after 1 h)
grouped by the market phase, sector heat and narrative heat that were known at the moment of the decision.
This is what decides whether MARKET_SIGNAL_WEIGHT and MARKET_SNIPER_GATE get switched on."""
import bisect
import json
import statistics
import time
from collections import defaultdict

from market import narratives, store
from market.analysis import LABELS
from market.config import settings as default_cfg

HEAT_BUCKETS = [("kalt <40", 0, 40), ("mittel 40-60", 40, 60), ("heiß ≥60", 60, 101)]


class Timeline:
    """Snapshots in memory for fast point-in-time lookups over thousands of outcomes."""

    def __init__(self, conn, since):
        self.snaps = [dict(r) for r in conn.execute(
            "SELECT ts, score, label, launch_hit2x FROM market_snapshots WHERE ts>=? ORDER BY ts", (since - 3600,))] \
            if store.has(conn, "market_snapshots") else []
        self.ts = [s["ts"] for s in self.snaps]
        self.conn = conn
        self._sec, self._term = {}, {}

    def at(self, ts, max_age):
        i = bisect.bisect_right(self.ts, ts) - 1
        if i < 0 or ts - self.ts[i] > max_age:
            return None
        return self.snaps[i]

    def sectors(self, snap_ts):
        if snap_ts not in self._sec:
            self._sec[snap_ts] = {r[0]: r[1] for r in self.conn.execute(
                "SELECT sector, heat FROM market_sectors WHERE ts=?", (snap_ts,))}
        return self._sec[snap_ts]

    def terms(self, snap_ts):
        if snap_ts not in self._term:
            self._term[snap_ts] = {r[0]: (r[1], r[2]) for r in self.conn.execute(
                "SELECT term, heat, hit2x FROM market_terms WHERE ts=?", (snap_ts,))}
        return self._term[snap_ts]


def _group(rows, key, hit, extra=None):
    g = defaultdict(list)
    for r in rows:
        k = key(r)
        if k is not None:
            g[k].append(r)
    out = {}
    for k, rs in g.items():
        d = {"n": len(rs), "hit_rate": round(sum(1 for r in rs if hit(r)) / len(rs), 4)}
        if extra:
            d.update(extra(rs))
        out[k] = d
    return out


def _launch_extra(rs):
    mx = [r["max_ret_60m"] for r in rs if r["max_ret_60m"] is not None]
    return {"rug_rate": round(sum(1 for r in rs if r["rugged"]) / len(rs), 4),
            "median_max_ret": round(statistics.median(mx), 4) if mx else None}


def _bucket(v, defs):
    if v is None:
        return None
    for name, lo, hi in defs:
        if lo <= v < hi:
            return name
    return None


def evaluate(conn, days=14, now=None, cfg=default_cfg):
    """-> {launches: {n, base_hit_rate, by_phase, by_sector_heat, by_term, gate}, signals: {n, by_phase},
    note}. hit = max +100 % within 60 min for launches, max +50 % within 1 h for signals."""
    now = int(now or time.time())
    since = now - days * 86400
    tl = Timeline(conn, since)
    out = {"days": days, "snapshots": len(tl.snaps), "launches": None, "signals": None}
    if not tl.snaps:
        out["note"] = "Noch keine Markt-Snapshots: die Auswertung füllt sich, sobald der Dienst market läuft."
        return out
    max_age = cfg.max_age_sec
    if store.has(conn, "sniper_launches"):
        rows = []
        for r in conn.execute("SELECT mint, name, symbol, created_ts, decided_ts, decision, stage, score, max_ret_60m, rugged "
                              "FROM sniper_launches WHERE created_ts>=? AND max_ret_60m IS NOT NULL", (since,)):
            d = dict(r)
            s = tl.at(d["decided_ts"] or d["created_ts"], max_age)
            if not s:
                continue
            d["label"] = s["label"]
            sec = narratives.classify(d["name"], d["symbol"])[0]
            d["sector_heat"] = tl.sectors(s["ts"]).get(sec)
            terms = tl.terms(s["ts"])
            th = [terms[t][0] for t in narratives.terms_of(d["name"], d["symbol"]) if t in terms]
            d["term_heat"] = max(th) if th else None
            rows.append(d)
        hit = lambda r: (r["max_ret_60m"] or 0) >= 1.0  # noqa: E731
        if rows:
            base = sum(1 for r in rows if hit(r)) / len(rows)
            by_phase = _group(rows, lambda r: r["label"], hit, _launch_extra)
            la = {"n": len(rows), "base_hit_rate": round(base, 4),
                  "by_phase": {LABELS.get(k, k): v for k, v in by_phase.items()},
                  "by_sector_heat": _group(rows, lambda r: _bucket(r["sector_heat"], HEAT_BUCKETS), hit, _launch_extra),
                  "by_term": _group(rows, lambda r: "mit heißem Narrativ" if (r["term_heat"] or 0) >= 60 else
                                    ("mit Narrativ" if r["term_heat"] is not None else "ohne Narrativ"), hit, _launch_extra)}
            # what the sniper gate would have changed among launches the sniper bought or would have bought
            cand = [r for r in rows if r["decision"] == "enter"]
            blocked = [r for r in cand if r["label"] == "risk_off"]
            kept = [r for r in cand if r["label"] != "risk_off"]
            la["gate"] = {"candidates": len(cand), "blocked": len(blocked),
                          "blocked_hit_rate": round(sum(map(hit, blocked)) / len(blocked), 4) if blocked else None,
                          "kept_hit_rate": round(sum(map(hit, kept)) / len(kept), 4) if kept else None,
                          "blocked_rug_rate": round(sum(1 for r in blocked if r["rugged"]) / len(blocked), 4) if blocked else None}
            out["launches"] = la
    if store.has(conn, "signals"):
        rows = []
        for r in conn.execute("SELECT ts, mint, score, max_ret_1h FROM signals WHERE ts>=? AND max_ret_1h IS NOT NULL", (since,)):
            s = tl.at(r["ts"], max_age)
            if s:
                rows.append(dict(r, label=s["label"]))
        if rows:
            out["signals"] = {"n": len(rows), "by_phase": {LABELS.get(k, k): v for k, v in _group(
                rows, lambda r: r["label"], lambda r: (r["max_ret_1h"] or 0) >= 0.5).items()}}
    n = (out["launches"] or {}).get("n", 0)
    out["note"] = (f"{n} Launches mit bekanntem Ergebnis und Markt-Stand zum Zeitpunkt der Entscheidung."
                   if n else "Noch keine Launches mit Ergebnis seit dem ersten Snapshot.")
    out["significant"] = significance(out)
    return out


def significance(ev, min_n=40):
    """Plain-language verdict per lens: is the hit rate in the best group clearly above the worst one?
    Two-proportion z-test, |z| >= 2 counts as clear (about 95 %)."""
    la = ev.get("launches") or {}
    verdicts = {}
    for lens in ("by_phase", "by_sector_heat", "by_term"):
        g = [(k, v) for k, v in (la.get(lens) or {}).items() if v["n"] >= min_n]
        if len(g) < 2:
            verdicts[lens] = {"clear": False, "text": f"zu wenig Daten (je Gruppe mindestens {min_n} nötig)"}
            continue
        hi = max(g, key=lambda x: x[1]["hit_rate"])
        lo = min(g, key=lambda x: x[1]["hit_rate"])
        p1, n1, p2, n2 = hi[1]["hit_rate"], hi[1]["n"], lo[1]["hit_rate"], lo[1]["n"]
        p = (p1 * n1 + p2 * n2) / (n1 + n2)
        se = (p * (1 - p) * (1 / n1 + 1 / n2)) ** 0.5
        z = (p1 - p2) / se if se else 0.0
        verdicts[lens] = {"clear": abs(z) >= 2, "z": round(z, 2), "best": hi[0], "worst": lo[0],
                          "text": f"{hi[0]}: {p1:.0%} Treffer gegen {lo[0]}: {p2:.0%} (z = {z:.1f})"}
    return verdicts


def to_json(ev):
    return json.dumps(ev, ensure_ascii=False, indent=1)
