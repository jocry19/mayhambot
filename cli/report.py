"""Plain Rich reports for scripts, SSH and Claude (no interactive terminal needed)."""
from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import data
from .fmt import (EXIT_LABEL, MODE_SHORT, RISK_LABEL, STRAT_SHORT, TIER_LABEL, TIER_STYLE, ago, clock, n, pct, risk, short, sol,
                  tiers, tone, usd, usd_signed)

con = Console()


def _t(title: str, *cols: str) -> Table:
    t = Table(title=title, title_justify="left", title_style="bold", header_style="dim", box=None, pad_edge=False)
    for c in cols:
        t.add_column(c, justify="left" if c in ("Coin", "Wallet", "Tier", "Grund", "Modus", "Konto", "Zeit", "Seit") else "right")
    return t


def _c(v, style=""):
    v = "–" if v is None else str(v)
    return f"[{style}]{escape(v)}[/]" if style else escape(v)


def status() -> None:
    s = data.snapshot(n_signals=10, n_wallets=0)
    ctl = s["control"] or {}
    modes = ctl.get("modes", {})
    acct = "[b red]Echtgeld bereit[/]" if ctl.get("live_ready") else "[b green]Papier[/]"
    con.print(f"[b]mayhambot[/]  Konto {acct}{'  [b yellow]Käufe pausiert[/]' if ctl.get('paused') else ''}  "
              f"Modus 1 Wale [b]{MODE_SHORT.get(modes.get('whale'), '–')}[/]  "
              f"Modus 2 Signale [b]{MODE_SHORT.get(modes.get('signal'), '–')}[/]")
    m = s.get("model")
    con.print(f"Tier-Wallets {tiers(s['tiers'])} von {n(s['wallets_total'])} · Signale 24 h {s['signals_24h']} · "
              f"letzter Trade {ago(s['last_trade_ts'])} · "
              + (f"Modell v{m['id']}" + (f" (AUC 1 h {n(m['auc_1h'], 2)})" if m.get("auc_1h") is not None else "") if m else "Modell noch nicht trainiert"))
    for mode, sm in (s["summary"] or {}).items():
        con.print(f"Copy-Trading {'Papier' if mode == 'paper' else 'Echtgeld'}: [{tone(sm.get('pnl_sol'))}]{sol(sm.get('pnl_sol'), 2, True)}[/] · "
                  f"{sm.get('trades', 0)} Trades · {pct(sm.get('win_rate'))} Gewinner · {sm.get('open', 0)} offen")
    con.print()
    _signals(s["signals"][:8])
    con.print()
    _open(s["open"])


def _signals(ss) -> None:
    t = _t("Letzte Signale", "Zeit", "Coin", "MC", "Note", "Score", "Rug-Risiko", "Wallets U/S/G", "Inflow", "Prognose", "1 h", "Max 1 h")
    for x in ss:
        o = x["outcome"]
        t.add_row(clock(x["ts"]), _c(x["symbol"], "bold"), usd(x["mcap_usd"]), x["grade"], n(x["score"]),
                  risk(x.get("safety") or x.get("safety_now"), x.get("safety_now")), tiers(x["tiers"]),
                  usd(x["inflow_usd"]), pct(x["prediction"]) if x["prediction"] is not None else "–",
                  _c(pct(o["ret_1h"], 0, True), tone(o["ret_1h"])), _c(pct(o["max_ret_1h"], 0, True), tone(o["max_ret_1h"])))
    con.print(t if ss else "[dim]Noch keine Signale.[/]")


def signals(limit: int = 15) -> None:
    s = data.snapshot(n_signals=limit, n_wallets=0)
    _signals(s["signals"])
    for x in s["signals"][:3]:
        con.print(f"\n[b]{escape(x['symbol'] or '')}[/] {clock(x['ts'])} · Score {n(x['score'])} ({x['grade']}) · {x['model_version']}")
        for why in (x.get("safety") or {}).get("reasons") or []:
            con.print(f"  [#ec835a]Rug-Schutz:[/] {escape(str(why))}")
        for b in x.get("breakdown") or []:
            share = b.get("share") or 0
            con.print(f"  {escape(str(b.get('label') or '')):<30} [#5598e7]{'█' * int(round(share * 30)):<30}[/] {pct(share)}  [dim]{escape(str(b.get('why') or ''))}[/]")


def _open(op) -> None:
    t = _t("Offene Positionen", "#", "Coin", "Modus", "Konto", "Seit", "Größe", "Ergebnis", "Version", "Rug-Risiko")
    for p in op:
        t.add_row(str(p["id"]), _c(p.get("symbol"), "bold"), STRAT_SHORT.get(p.get("strategy") or "whale", ""),
                  "Echtgeld" if p["mode"] == "live" else "Papier", ago(p.get("opened_at")), sol(p.get("size_sol")),
                  _c(pct(p.get("unrealized_pct"), 1, True), tone(p.get("unrealized_pct"))),
                  f"v{p['param_version']}" if p.get("param_version") else "Preset",
                  risk({"level": p.get("safety_level"), "score": p.get("safety_score")}))
    con.print(t if op else "[dim]Keine offenen Positionen.[/]")


def positions() -> None:
    s = data.snapshot(n_signals=0, n_wallets=0)
    _open(s["open"])
    con.print()
    t = _t("Letzte Trades", "Zeit", "Coin", "Modus", "Konto", "Haltedauer", "Ergebnis", "SOL", "Grund")
    for p in s["closed"][:25]:
        t.add_row(clock(p.get("closed_at")), _c(p.get("symbol"), "bold"), STRAT_SHORT.get(p.get("strategy") or "whale", ""),
                  "Echtgeld" if p["mode"] == "live" else "Papier", f"{n(p.get('hold_min'))} min",
                  _c(pct(p.get("pnl_pct"), 1, True), tone(p.get("pnl_pct"))), _c(sol(p.get("pnl_sol"), 3, True), tone(p.get("pnl_sol"))),
                  EXIT_LABEL.get(p.get("exit_reason"), p.get("exit_reason") or ""))
    con.print(t if s["closed"] else "[dim]Noch keine abgeschlossenen Trades.[/]")


def wallets(limit: int = 25, tier: str | None = None) -> None:
    from web import queries as Q
    from web.db import read_conn

    res = Q.wallets(read_conn(), tier=tier, limit=limit)
    items = res.get("items", []) if isinstance(res, dict) else res
    t = _t(f"Rangliste Wallets ({res.get('total', len(items)) if isinstance(res, dict) else len(items)})",
           "#", "Wallet", "Tier", "Score", "Coins", "Treffer", "ROI", "Gewinn", "Einfluss", "Zuletzt")
    for i, w in enumerate(items, 1):
        t.add_row(str(i), w["address"] if con.width > 150 else short(w["address"], 5),
                  _c(TIER_LABEL.get(w.get("tier"), "–"), TIER_STYLE.get(w.get("tier"), "dim")), n(w.get("score")),
                  str(w.get("n_tokens") or 0), pct(w.get("win_rate")), _c(pct(w.get("roi"), 0, True), tone(w.get("roi"))),
                  _c(usd_signed(w.get("total_pnl_usd")), tone(w.get("total_pnl_usd"))), n(w.get("influence_followers"), 1),
                  ago(w.get("last_trade_ts")))
    con.print(t if items else "[dim]Noch keine bewerteten Wallets.[/]")


def safety(mint: str | None = None) -> None:
    """Without mint: blocked coins and manual decisions. With mint: the stored assessment with reasons."""
    from web import queries as Q
    from web.db import read_conn

    conn = read_conn()
    if not mint:
        d = Q.safety_list(conn)
        if not d["available"]:
            con.print("[dim]Der Rug-Schutz hat noch keine Coins geprüft.[/]")
        c = d.get("counts") or {}
        con.print("Geprüft: " + " · ".join(f"{n(c.get(k, 0))} {RISK_LABEL[k]}" for k in ("low", "caution", "high", "critical")))
        t = _t("Gesperrt oder von dir entschieden", "Coin", "Risiko", "Konto", "Grund")
        for r in d["items"]:
            t.add_row(_c(r.get("symbol") or r["mint"][:6], "bold"), risk(r),
                      {"block": "von dir gesperrt", "allow": "von dir freigegeben"}.get(r.get("override"), "automatisch"),
                      _c(r.get("override_reason") or (r.get("reasons") or ["–"])[0]))
        con.print(t if d["items"] else "[dim]Kein Coin gesperrt.[/]")
        con.print(f"[dim]{escape('python -m cli safety block|allow|clear MINT [Grund]')}[/]")
        return
    s = Q.safety_detail(conn, mint)
    if not s["checked"]:
        con.print(f"[dim]{escape(mint)} wurde noch nicht geprüft.[/]" + (" [bold red]Manuell gesperrt.[/]" if s["blocked"] else ""))
        return
    con.print(f"[b]{escape(mint)}[/]  Rug-Risiko {risk(s)} von 100 · Stand {clock(s['ts'])} ({ago(s['ts'])}) · "
              f"Copy-Trading {'[bold red]gesperrt[/]' if s['blocked'] else '[green]erlaubt[/]'}")
    if s.get("override"):
        o = s["override"]
        con.print(f"Manuell {'gesperrt' if o['action'] == 'block' else 'freigegeben'}: {escape(o.get('reason') or '')}")
    for r in s["reasons"]:
        con.print(f"  [#ec835a]{n(r.get('risk')):>3}[/] {escape(str(r.get('text') or ''))}")
    for p in s.get("positives") or []:
        con.print(f"  [green]  ✓[/] {escape(str(p))}")
