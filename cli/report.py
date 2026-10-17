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
        con.print(f"Copy-Trading { {'paper': 'Papier streng', 'explore': 'Erkundung'}.get(mode, 'Echtgeld')}: {_c(sol(sm.get('total_sol'), 2, True), tone(sm.get('total_sol')))} (realisiert + offen) · "
                  f"{sm.get('trades', 0)} Trades · {pct(sm.get('win_rate'))} Gewinner · {sm.get('open', 0)} offen")
    line = api_line()
    if line:
        con.print(line)
    cl = credits_line()
    if cl:
        con.print(cl)
    rl = rpc_pool_line()
    if rl:
        con.print(rl)
    sl = sniper_line(s.get("sniper"))
    if sl:
        con.print(sl)
    bl = brain_line(s.get("brain"))
    if bl:
        con.print(bl)
    ml = market_line(s.get("market"))
    if ml:
        con.print(ml)
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


def api_line() -> str:
    """One line: calls in the last minute vs budget per host group over all running services, 429s, services above 80 %."""
    from web import queries as Q

    u = Q.api_usage()
    rows = [r for r in u.get("rows", []) if not r.get("stale")]
    if not rows:
        return ""
    groups: dict = {}
    for r in rows:
        g = groups.setdefault(r["group"], {"last": 0, "budget": 0, "429": 0, "credits": None, "cap": None})
        g["last"] += r.get("last_min") or 0
        g["budget"] += r.get("budget_per_min") or 0
        g["429"] += r.get("429") or 0
    names = {"helius": "Helius", "geckoterminal": "GeckoTerminal"}
    parts = []
    for name, g in sorted(groups.items(), key=lambda kv: -kv[1]["last"])[:4]:
        txt = f"{escape(names.get(name, name))} {n(g['last'])}"
        if g["budget"]:
            ld = g["last"] / g["budget"]
            txt += f"/{n(g['budget'])} [{'red' if ld >= 1 else 'yellow' if ld >= .8 else 'dim'}]({pct(ld)})[/]"
        if g["429"]:
            txt += f" [red]{n(g['429'])}×429[/]"
        parts.append(txt)
    hot = [f"{r['service']} {escape(names.get(r['group'], r['group']))} {pct(r['load'])}" for r in rows if (r.get("load") or 0) >= .8]
    return "API letzte Minute: " + " · ".join(parts) + (f"  [b yellow]knapp: {', '.join(hot)}[/]" if hot else "")


def credits_line() -> str:
    """'Helius-Credits diesen Monat: c von 1.000.000 (x %)', yellow from 80 %, red from 100 %."""
    from web import queries as Q

    u = Q.api_usage()
    t = u.get("credits")
    if not t:
        return ""
    ld = t.get("load") or 0
    st = "red" if ld >= 1 else "yellow" if ld >= .8 else "dim"
    refused = sum(r.get("refused") or 0 for r in u.get("rows", []))
    return (f"Helius-Credits diesen Monat: {n(t['used'])} von {n(t['limit'])} [{st}]({pct(ld, 1)})[/]"
            + (f" · [red]{n(refused)} Aufrufe abgewiesen[/]" if refused else ""))


def brain_line(b: dict | None) -> str:
    """'Entscheider (Papier): Equity 5,24 SOL (+0,24), 6 offen · genommen Ø +0,8 % gegen ausgelassen … und alles kopiert …'."""
    if not b or not b.get("available"):
        return ""
    a, h = b["account"], b["honest"]
    res = a.get("result_sol")
    head = (f"Entscheider (Papier): Equity {sol(a.get('equity_sol'), 2)} ({_c(sol(res, 2, True), tone(res))}), {n(a.get('open'))} offen, "
            f"{n(b['last24']['n'])} Entscheidungen 24 h, {n(b['last24']['entered'])} gekauft")
    g = b.get("goal") or {}
    if g.get("today") is not None:
        lo, hi = (g.get("mult") or (2, 4))[:2]
        head += f", Tagesfaktor heute {_c('×' + n(g['today'], 2), 'green' if g['today'] >= lo else 'red' if g['today'] < 1 else '')} (Ziel ×{lo}–{hi})"
    if not h.get("n"):
        return head + " · noch nichts nachgeprüft"
    return (head + f" · ehrlich: genommen Ø {_c(pct(h.get('taken_mean'), 1, True), tone(h.get('taken_mean')))}"
            f" gegen ausgelassen Ø {pct(h.get('passed_mean'), 1, True)} und alles kopiert Ø {pct(h.get('copy_all_mean'), 1, True)}"
            f" ({n(h.get('n_taken'))}/{n(h.get('target'))} nachgeprüft)")


def brain(limit: int = 12) -> None:
    """The learning decider: account, honest scorecard, open positions, newest decisions with their reasons, diary."""
    b = data.brain(limit)
    if not b.get("available"):
        con.print(b.get("note") or "Entscheider: keine Daten")
        return
    con.print(brain_line(b))
    t = _t("Offene Positionen (Entscheider)", "Coin", "Quelle", "Plan", "Einsatz", "Jetzt", "Seit")
    for p in b["open"]:
        u = p.get("unrealized_pct")
        t.add_row(escape(p.get("symbol") or (p.get("mint") or "")[:6]), p.get("kind_label") or "", p.get("plan_label") or "",
                  sol(p.get("size_sol"), 2), _c(pct(u, 1, True), tone(u)), ago(p.get("opened_at")))
    if b["open"]:
        con.print(t)
    con.print()
    for d in b["decisions"]:
        style = {"good": "green", "warn": "yellow"}.get(d.get("tone"), "dim")
        con.print(f"[{style}]{d['verdict']}[/] {d['kind_label']} [b]{escape(d.get('symbol') or (d.get('mint') or '')[:6])}[/] "
                  f"{ago(d['ts'])} · Plan {d.get('plan_label') or '–'} · erwartet {pct(d.get('pred_mean'), 1, True)}, sicher zu {pct(d.get('pred_p'))}")
        for line in d.get("note") or []:
            con.print(f"   [dim]{escape(line)}[/]")
        if d.get("evaluated"):
            con.print(f"   nachgespielt: {_c(pct(d.get('realized_plan'), 1, True), tone(d.get('realized_plan')))} "
                      f"(stumpf kopiert {pct(d.get('realized_base'), 1, True)})")
    if b["diary"]:
        con.print()
        con.print(escape(b["diary"][0]["text"]))


def sniper_line(sn: dict | None) -> str:
    """'Launch-Sniper (Papier): n Launches heute, k gekauft, Ergebnis +x SOL, letzter Kauf SYM vor 5 min'."""
    if not sn:
        return ""
    pnl = sn.get("pnl_sol")
    tone_ = "green" if (pnl or 0) > 0 else "red" if (pnl or 0) < 0 else "dim"
    lb = sn.get("last_buy")
    last = f"letzter Kauf {escape(lb.get('symbol') or lb['mint'][:6] + '…')} {ago(lb['opened_ts'])}" if lb else "noch kein Kauf"
    down = " · [red]Dienst steht[/]" if sn.get("service_up") is False else \
        " · [yellow]Websocket getrennt[/]" if sn.get("service_up") and not sn.get("connected") else ""
    return (f"Launch-Sniper (Papier): {n(sn.get('launches_today'))} Launches heute, {n(sn.get('entered_today'))} gekauft, "
            f"Ergebnis [{tone_}]{sol(pnl, 3, signed=True)}[/] aus {n(sn.get('closed'))} Trades, {last}{down}")


def market_line(m: dict | None) -> str:
    """'Markt: Risk-on 62/100 · SOL 24 h +3,1 % · Top-Sektor KI & Agenten · Narrativ "tesla roadster" · vor 3 min'."""
    if not m or m.get("score") is None:
        return ""
    lab = m.get("label")
    tone_ = "green" if lab in ("risk_on", "hot") else "red" if lab == "risk_off" else "yellow"
    sol24 = m.get("sol_ret_24h")
    parts = [f"Markt: [{tone_}]{escape(m.get('label_de') or '–')} {m['score']:.0f}/100[/]"]
    if sol24 is not None:
        parts.append(f"SOL 24 h {sol24 * 100:+.1f} %".replace(".", ","))
    if m.get("top_sector"):
        parts.append(f"Top-Sektor {escape(m.get('top_sector_label') or m['top_sector'])}")
    if m.get("top_term"):
        parts.append(f'Narrativ "{escape(m["top_term"])}"')
    parts.append(ago(m.get("ts")))
    stale = (m.get("age_s") or 0) > 1800
    return " · ".join(parts) + (" · [red]Daten veraltet[/]" if stale else "")


def rpc_pool_line() -> str:
    """'RPC-Pool: n von m Endpunkten aktiv, schnellster X ms' (red when none is active, yellow above 1 s)."""
    from web import queries as Q

    p = Q.rpc_pool()
    if not p.get("available") or not p.get("total"):
        return ""
    a, m = p["active"], p["total"]
    head = f"[red]{a} von {m} Endpunkten aktiv[/]" if not a else f"{a} von {m} Endpunkten aktiv"
    ms = p.get("fastest_ms")
    fast = "" if ms is None else f", schnellster [{'yellow' if ms > p['slow_ms'] else 'dim'}]{n(ms)} ms[/] ({escape(p['fastest_host'])})"
    paused = [r["host"] for r in p["rows"] if r["state"] == "pausiert"]
    return f"RPC-Pool: {head}{fast}" + (f" · pausiert: {escape(', '.join(paused))}" if paused else "")
