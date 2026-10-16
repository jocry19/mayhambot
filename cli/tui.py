"""Textual terminal app: status, live signals, positions, trades, wallet tiers and mode switching."""
from __future__ import annotations

import time

from rich.markup import escape
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Label, Static, TabbedContent, TabPane

from . import data
from .fmt import (EXIT_LABEL, MODE_SHORT, STRAT_SHORT, TIER_LABEL, TIER_STYLE, ago, clock, compact, n, pct, risk, short, sol,
                  tiers, tone, usd, usd_signed)

MODE_CYCLE = ["off", "manual", "semi", "auto"]
REFRESH_S = 5


def cell(v, style="") -> Text:
    return Text.from_markup(v) if "[" in str(v) and not style else Text(str(v), style=style)


def right(v, style="") -> Text:
    t = Text(str(v), style=style)
    t.justify = "right"
    return t


class BuyScreen(ModalScreen[tuple[str, float | None] | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Abbrechen")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            yield Label("[b]Selbst kaufen[/b]  (Auftrag an den laufenden Bot, Modus 2 / du verkaufst)")
            yield Input(placeholder="Mint-Adresse", id="mint")
            yield Input(placeholder="SOL (leer = Preset-Größe)", id="size")
            yield Label("[dim]Enter = kaufen · Esc = abbrechen[/dim]")

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        mint = self.query_one("#mint", Input).value.strip()
        if ev.input.id == "mint" and mint:
            self.query_one("#size", Input).focus()
            return
        raw = self.query_one("#size", Input).value.strip().replace(",", ".")
        try:
            size = float(raw) if raw else None
        except ValueError:
            self.notify("SOL-Betrag ist keine Zahl", severity="error")
            return
        self.dismiss((mint, size) if mint else None)


class MayhamTUI(App):
    TITLE = "mayhambot"
    CSS = """
    Screen { background: #0e1116; }
    #status { height: 3; padding: 0 1; background: #161b22; color: #c9d1d9; }
    #tiles { height: 5; }
    .tile { border: round #30363d; padding: 0 1; width: 1fr; height: 5; }
    DataTable { height: 1fr; }
    #sigdetail { height: 12; border-top: solid #30363d; padding: 0 1; }
    .half { height: 1fr; }
    .lbl { color: #8b949e; padding: 0 1; }
    #dlg { width: 70; height: auto; padding: 1 2; border: round #5598e7; background: #161b22; }
    BuyScreen { align: center middle; }
    """
    BINDINGS = [
        Binding("q", "quit", "Beenden"),
        Binding("r", "refresh", "Neu laden"),
        Binding("p", "pause", "Pause an/aus"),
        Binding("w", "cycle('whale')", "Modus 1 wechseln"),
        Binding("m", "cycle('signal')", "Modus 2 wechseln"),
        Binding("v", "sell", "Position verkaufen"),
        Binding("b", "buy", "Kaufen"),
        Binding("k", "buy_signal", "Signal kaufen"),
        Binding("1", "tab('t-over')", "Übersicht", show=False),
        Binding("2", "tab('t-sig')", "Signale", show=False),
        Binding("3", "tab('t-pos')", "Positionen", show=False),
        Binding("4", "tab('t-wal')", "Wallets", show=False),
        Binding("5", "tab('t-al')", "Hinweise", show=False),
    ]

    def __init__(self):
        super().__init__()
        self.snap: dict | None = None
        self._sell_armed: tuple[int, float] | None = None

    # ------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Static("Lade …", id="status")
        with TabbedContent(initial="t-over"):
            with TabPane("1 Übersicht", id="t-over"):
                with Horizontal(id="tiles"):
                    for k in ("tw", "sg", "ct", "md"):
                        yield Static("", classes="tile", id=f"tile-{k}")
                yield Label("Stärkste Coins jetzt", classes="lbl")
                yield DataTable(id="coins", zebra_stripes=True, cursor_type="row")
            with TabPane("2 Signale", id="t-sig"):
                yield DataTable(id="sigs", zebra_stripes=True, cursor_type="row")
                yield Static("Zeile wählen: Gewichtung und Wallets des Signals · k = diesen Coin kaufen (zweimal drücken)", id="sigdetail")
            with TabPane("3 Positionen", id="t-pos"):
                yield Label("Offene Positionen  (v = verkaufen, zweimal drücken)", classes="lbl")
                yield DataTable(id="open", zebra_stripes=True, cursor_type="row", classes="half")
                yield Label("Abgeschlossene Trades", classes="lbl")
                yield DataTable(id="closed", zebra_stripes=True, cursor_type="row", classes="half")
            with TabPane("4 Wallets", id="t-wal"):
                yield DataTable(id="wallets", zebra_stripes=True, cursor_type="row")
            with TabPane("5 Hinweise", id="t-al"):
                yield DataTable(id="alerts", zebra_stripes=True, cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        cols = {
            "coins": ["Coin", "Note", "Stärke", "Käufer 30 min", "Inflow 30 min", "MC", "1 h", "Signale"],
            "sigs": ["Zeit", "Coin", "MC", "Note", "Score", "Rug-Risiko", "Wallets", "Inflow", "Prognose", "1 h", "Max 1 h"],
            "open": ["#", "Coin", "Modus", "Konto", "Seit", "Größe", "Ergebnis", "Ziel / Stop", "Wer verkauft", "Version", "Rug-Risiko"],
            "closed": ["Zu", "Coin", "Modus", "Konto", "Haltedauer", "Ergebnis", "SOL", "Grund"],
            "wallets": ["#", "Wallet", "Tier", "Score", "Coins", "Treffer", "ROI", "Gewinn", "Einfluss", "Zuletzt"],
            "alerts": ["Zeit", "Modus", "Coin", "Aktion", "Grund"],
        }
        for tid, cs in cols.items():
            self.query_one(f"#{tid}", DataTable).add_columns(*cs)
        self.load()
        self.set_interval(REFRESH_S, self.load)

    # ------------------------------------------------------------ data
    @work(thread=True, exclusive=True)
    def load(self) -> None:
        try:
            snap = data.snapshot()
            try:
                from .report import api_line, credits_line
                snap["api_line"] = "\n".join(x for x in (api_line(), credits_line()) if x)
            except Exception:
                snap["api_line"] = ""
        except Exception as e:  # DB busy or missing: keep the last picture
            self.call_from_thread(self.notify, f"Daten nicht lesbar: {e}", severity="error")
            return
        self.call_from_thread(self.render_snap, snap)

    def render_snap(self, s: dict) -> None:
        self.snap = s
        ctl = s["control"] or {}
        modes = ctl.get("modes", {})
        live_master = ctl.get("live_ready")
        acct = "[b red]ECHTGELD bereit[/]" if live_master else "[b green]Papier[/]"
        paused = "  [b yellow]KÄUFE PAUSIERT[/]" if ctl.get("paused") else ""
        model = s.get("model")
        mtxt = f"Modell v{model['id']}" if model else "Modell noch nicht trainiert"
        self.query_one("#status", Static).update(
            f"[b]mayhambot[/]  {acct}{paused}   Modus 1 Wale: [b]{MODE_SHORT.get(modes.get('whale'), '–')}[/]"
            f"   Modus 2 Signale: [b]{MODE_SHORT.get(modes.get('signal'), '–')}[/]   {mtxt}\n"
            f"[dim]Letzter Trade {ago(s['last_trade_ts'])} · {n(s['trades_24h'])} Trades 24 h · "
            f"aktualisiert {time.strftime('%H:%M:%S')} · alle {REFRESH_S} s[/]"
            + (f"\n{s['api_line']}" if s.get("api_line") else ""))
        t = s["tiers"]
        paper = (s["summary"] or {}).get("paper", {})
        self.query_one("#tile-tw", Static).update(
            f"[dim]Tier-Wallets[/]\n{tiers(t)}\n[dim]{n(s['wallets_total'])} beobachtet[/]")
        self.query_one("#tile-sg", Static).update(
            f"[dim]Signale 24 h[/]\n[b]{s['signals_24h']}[/]\n[dim]{len(s['signals'])} in der Liste[/]")
        pnl = paper.get("pnl_sol")
        self.query_one("#tile-ct", Static).update(
            f"[dim]Copy-Trading Papier[/]\n[b {tone(pnl)}]{sol(pnl, 2, True)}[/]\n"
            f"[dim]{paper.get('trades', 0)} Trades · {pct(paper.get('win_rate'))} Gewinner · {paper.get('open', 0)} offen[/]")
        self.query_one("#tile-md", Static).update(
            f"[dim]Steuerung[/]\nw = Modus 1, m = Modus 2\n[dim]p = Pause · b = Kaufen[/]")
        self._fill_coins(s["top_coins"])
        self._fill_signals(s["signals"])
        self._fill_positions(s["open"], s["closed"])
        self._fill_wallets(s["wallets"])
        self._fill_alerts(s["alerts"])

    def _refill(self, tid: str, rows: list[tuple], keys: list[str]) -> None:
        tbl = self.query_one(f"#{tid}", DataTable)
        cur = tbl.cursor_row
        tbl.clear()
        for k, r in zip(keys, rows):
            tbl.add_row(*r, key=k)
        if rows and cur is not None:
            tbl.move_cursor(row=min(cur, len(rows) - 1))

    def _fill_coins(self, cs: list[dict]) -> None:
        self._refill("coins", [(
            cell(c["symbol"], "bold"), cell(c.get("grade") or "–"), right(n(c.get("strength"))),
            cell(tiers(c.get("buyers_win"))), right(usd_signed(c.get("net_tiered_win")), tone(c.get("net_tiered_win"))),
            right(usd(c.get("mcap_usd"))),
            right(pct((c.get("change_h1") or 0) / 100, 0, True) if c.get("change_h1") is not None else "–", tone(c.get("change_h1"))),
            right(c.get("signals", 0)),
        ) for c in cs], [c["mint"] for c in cs])

    def _fill_signals(self, ss: list[dict]) -> None:
        self._refill("sigs", [(
            cell(clock(x["ts"])), cell(x["symbol"] or x["mint"][:6], "bold"), right(usd(x["mcap_usd"])),
            cell(x["grade"]), right(n(x["score"])), cell(risk(x.get("safety") or x.get("safety_now"), x.get("safety_now"))),
            cell(tiers(x["tiers"])), right(usd(x["inflow_usd"])),
            right(pct(x["prediction"]) if x["prediction"] is not None else "–"),
            right(pct(x["outcome"]["ret_1h"], 0, True), tone(x["outcome"]["ret_1h"])),
            right(pct(x["outcome"]["max_ret_1h"], 0, True), tone(x["outcome"]["max_ret_1h"])),
        ) for x in ss], [str(x["id"]) for x in ss])

    def _fill_positions(self, op: list[dict], cl: list[dict]) -> None:
        modes = ((self.snap or {}).get("control") or {}).get("modes", {})

        def who(p):
            st = p.get("strategy") or "whale"
            return "du" if st == "manual" or modes.get(st) == "semi" else "Bot"
        self._refill("open", [(
            right(p["id"]), cell(p.get("symbol") or p["mint"][:6], "bold"),
            cell(STRAT_SHORT.get(p.get("strategy") or "whale", "")),
            cell("Echtgeld" if p["mode"] == "live" else "Papier", "red" if p["mode"] == "live" else "green"),
            cell(ago(p.get("opened_at"))), right(sol(p.get("size_sol"))),
            right(pct(p.get("unrealized_pct"), 1, True), tone(p.get("unrealized_pct"))),
            right(f"{pct(p.get('take_profit'), 0, True)} / -{pct(p.get('stop_loss'))}"), cell(who(p)),
            cell(f"v{p['param_version']}" if p.get("param_version") else "Preset"),
            cell(risk({"level": p.get("safety_level"), "score": p.get("safety_score")})),
        ) for p in op], [str(p["id"]) for p in op])
        self._refill("closed", [(
            cell(clock(p.get("closed_at"))), cell(p.get("symbol") or p["mint"][:6], "bold"),
            cell(STRAT_SHORT.get(p.get("strategy") or "whale", "")),
            cell("Echtgeld" if p["mode"] == "live" else "Papier"),
            right(f"{n(p.get('hold_min'))} min"),
            right(pct(p.get("pnl_pct"), 1, True), tone(p.get("pnl_pct"))),
            right(sol(p.get("pnl_sol"), 3, True), tone(p.get("pnl_sol"))),
            cell(EXIT_LABEL.get(p.get("exit_reason"), p.get("exit_reason") or "")),
        ) for p in cl], [str(p["id"]) for p in cl])

    def _fill_wallets(self, ws: list[dict]) -> None:
        self._refill("wallets", [(
            right(i + 1), cell(short(w["address"], 5)),
            cell(TIER_LABEL.get(w.get("tier"), "–"), TIER_STYLE.get(w.get("tier"), "dim")),
            right(n(w.get("score"))), right(w.get("n_tokens") or 0), right(pct(w.get("win_rate"))),
            right(pct(w.get("roi"), 0, True), tone(w.get("roi"))),
            right(usd_signed(w.get("total_pnl_usd")), tone(w.get("total_pnl_usd"))),
            right(n(w.get("influence_followers"), 1)), cell(ago(w.get("last_trade_ts"))),
        ) for i, w in enumerate(ws)], [w["address"] for w in ws])

    def _fill_alerts(self, al: list[dict]) -> None:
        self._refill("alerts", [(
            cell(clock(a.get("ts"))), cell(STRAT_SHORT.get(a.get("strategy"), a.get("strategy") or "")),
            cell(a.get("symbol") or (a.get("mint") or "")[:6], "bold"),
            cell(a.get("action") or "", "yellow" if a.get("action") == "sell" else ""), cell(a.get("reason") or ""),
        ) for a in al], [str(i) for i in range(len(al))])

    # ------------------------------------------------------------ interaction
    def on_data_table_row_highlighted(self, ev: DataTable.RowHighlighted) -> None:
        if ev.data_table.id != "sigs" or not self.snap or ev.row_key is None:
            return
        sid = ev.row_key.value
        sig = next((x for x in self.snap["signals"] if str(x["id"]) == sid), None)
        if not sig:
            return
        lines = [f"[b]{escape(sig['symbol'] or '')}[/] bei MC {usd(sig['mcap_usd'])} · Score {n(sig['score'])} ({sig['grade']}) · "
                 f"{tiers(sig['tiers'])} · Inflow {usd(sig['inflow_usd'])} · {sig['model_version']}"]
        for b in (sig.get("breakdown") or [])[:6]:
            share = b.get("share")
            bar = "█" * int(round((share or 0) * 30))
            label = escape(str(b.get("label") or b.get("feature") or ""))[:32]
            lines.append(f"  {label:<32} [#5598e7]{bar:<30}[/] {pct(share) if share is not None else ''}  [dim]{escape(str(b.get('why') or ''))}[/]")
        for why in (sig.get("safety") or {}).get("reasons") or []:
            lines.append(f"  [#ec835a]Rug-Schutz:[/] {escape(str(why))}")
        ws = ", ".join(f"[{TIER_STYLE.get(w['tier'], 'dim')}]{short(w['address'])}[/] {usd(w['buy_usd'])}" for w in sig["wallets"][:6])
        if ws:
            lines.append(f"  Wallets: {ws}")
        self.query_one("#sigdetail", Static).update("\n".join(lines))

    def on_data_table_row_selected(self, ev: DataTable.RowSelected) -> None:
        if ev.data_table.id == "coins" and self.snap:
            self.notify(f"Details im Dashboard: http://localhost:8600/#/coin/{ev.row_key.value}", timeout=6)

    def action_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    def action_refresh(self) -> None:
        self.load()

    def _do(self, fn, *a) -> None:
        try:
            self.notify(fn(*a))
        except Exception as e:
            self.notify(str(e), severity="error")
        self.load()

    def action_pause(self) -> None:
        on = not ((self.snap or {}).get("control") or {}).get("paused")
        self._do(data.set_paused, on)

    def action_cycle(self, strategy: str) -> None:
        cur = ((self.snap or {}).get("control") or {}).get("modes", {}).get(strategy, "off")
        nxt = MODE_CYCLE[(MODE_CYCLE.index(cur) + 1) % len(MODE_CYCLE)] if cur in MODE_CYCLE else "manual"
        self._do(data.set_mode, strategy, nxt)

    def action_sell(self) -> None:
        tbl = self.query_one("#open", DataTable)
        if self.query_one(TabbedContent).active != "t-pos" or not tbl.row_count:
            self.notify("Im Tab 3 Positionen eine offene Position wählen, dann v", severity="warning")
            return
        key = tbl.coordinate_to_cell_key(tbl.cursor_coordinate).row_key.value
        pid = int(key)
        now = time.time()
        if self._sell_armed and self._sell_armed[0] == pid and now - self._sell_armed[1] < 5:
            self._sell_armed = None
            self._do(data.sell, pid)
        else:
            self._sell_armed = (pid, now)
            sym = next((p.get("symbol") for p in self.snap["open"] if p["id"] == pid), f"#{pid}")
            self.notify(f"{sym} verkaufen? Zum Bestätigen innerhalb von 5 s nochmal v drücken.", severity="warning")

    def action_buy_signal(self) -> None:
        tbl = self.query_one("#sigs", DataTable)
        if self.query_one(TabbedContent).active != "t-sig" or not tbl.row_count or not self.snap:
            self.notify("Im Tab 2 Signale ein Signal wählen, dann k", severity="warning")
            return
        sid = tbl.coordinate_to_cell_key(tbl.cursor_coordinate).row_key.value
        sig = next((x for x in self.snap["signals"] if str(x["id"]) == sid), None)
        if not sig:
            return
        ctl = self.snap.get("control") or {}
        name = (ctl.get("active_presets") or {}).get("signal") or "standard"
        size = ((ctl.get("presets") or {}).get(name) or {}).get("size_sol")
        acct = "ECHTGELD" if ctl.get("live_ready") and (ctl.get("live") or {}).get("signal") else "Papier"
        key = ("buy", sig["mint"])
        now = time.time()
        if self._sell_armed and self._sell_armed[0] == key and now - self._sell_armed[1] < 5:
            self._sell_armed = None
            self._do(data.buy, sig["mint"], None)
        else:
            self._sell_armed = (key, now)
            self.notify(f"{sig['symbol']} kaufen: {sol(size)} · Preset {name} · {acct}. Zum Bestätigen innerhalb von 5 s nochmal k.",
                        severity="warning", timeout=6)

    def action_buy(self) -> None:
        def done(res):
            if res:
                self._do(data.buy, res[0], res[1])
        self.push_screen(BuyScreen(), done)


def run() -> None:
    MayhamTUI().run()
