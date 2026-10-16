"""Live watchdog (`python -m live run`, service 'live' in mayham.py). Uses only free public RPC, no Helius credits.

Every 30 s: heartbeat (guard refuses real buys without it), resolve unclear orders, market-data health
(Jupiter quote for 0.1 SOL -> USDC; 3 failures in a row = data outage, guard pauses buys), daily-loss alert.
Every 10 min with real money: tokens in the trading wallet that no open position accounts for.
Telegram commands from your own chat only (TELEGRAM_CHAT_ID):
  /notaus          no more live buys (sells and exits keep running)
  /alles_verkaufen Not-Aus plus sell every open live/dry position now
  /weiter          lift the Not-Aus and reset the failure streak
  /live            status
"""
from __future__ import annotations

import logging
import os
import threading
import time

from . import guard, store
from .config import load

log = logging.getLogger("live.service")
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
HELP = ("/notaus: keine Echtgeld-Käufe mehr\n/alles_verkaufen: Not-Aus und alle Live-Positionen verkaufen\n"
        "/weiter: Not-Aus aufheben\n/live: Status\n/betrieb: Dienste, Update, Backup, Speicher")


def _tg_cfg() -> tuple[str, str]:
    return os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")


def send(text: str) -> bool:
    token, chat = _tg_cfg()
    if not (token and chat):
        log.info("ALERT %s", text)
        return False
    try:
        import requests

        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=15)
        return r.ok
    except Exception as e:  # noqa: BLE001 - never log the URL (token)
        log.warning("telegram send failed (%s)", type(e).__name__)
        return False


def alert_once(conn, key: str, text: str, every_s: float = 3600) -> None:
    last = float(store.get(conn, f"alert:{key}") or 0)
    if time.time() - last >= every_s:
        store.put(conn, f"alert:{key}", int(time.time()))
        send(text)


def status_text(conn, cfg) -> str:
    from . import readiness

    rep = readiness.report(conn, cfg)
    mode = "aus" if not cfg.enabled else ("DRY-RUN" if cfg.dry_run else "ECHTGELD")
    lines = [f"Live: {mode} · {cfg.stage.name}", f"Not-Aus: {store.killed(conn) or 'nein'}"]
    for m in ("live", "dry"):
        op = guard.open_positions(conn, m)
        if op or m == ("dry" if cfg.dry_run else "live"):
            lines.append(f"{m}: {len(op)} offen, Einsatz {guard.exposure(conn, m):.3f} SOL, "
                         f"24 h {guard.loss_24h(conn, m):+.3f} SOL")
    lines.append("Go-Live-Prüfung: " + ("bestanden" if rep["ready"] else "offen"))
    for c in rep["checks"]:
        if c["required"] and not c["ok"]:
            lines.append(f"  ✗ {c['label']}: {c['detail']}")
    return "\n".join(lines)


def ops_report() -> str:
    """Operations report from deploy/ops.py (services, auto-update, backup, disk); read-only."""
    try:
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location("mayham_ops", Path(__file__).resolve().parents[1] / "deploy" / "ops.py")
        ops = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ops)
        return ops.ops_report().replace("<b>", "").replace("</b>", "")
    except Exception as e:  # noqa: BLE001
        return f"Betriebsbericht nicht verfügbar ({type(e).__name__})"


def handle_command(conn, cfg, text: str) -> str:
    cmd = (text or "").strip().split()[0].split("@")[0].lower() if text and text.strip() else ""
    if cmd == "/notaus":
        store.kill(conn, "Telegram /notaus")
        return "🛑 Not-Aus: keine Echtgeld-Käufe mehr. Offene Positionen werden weiter normal verkauft."
    if cmd == "/alles_verkaufen":
        store.kill(conn, "Telegram /alles_verkaufen")
        n = 0
        try:
            from copytrade import control  # type: ignore

            for m in ("live", "dry"):
                for p in guard.open_positions(conn, m):
                    control.request_sell(conn, p["id"])
                    n += 1
        except Exception as e:  # noqa: BLE001
            return f"🛑 Not-Aus gesetzt, Verkauf nicht beauftragt: {e}"
        return f"🛑 Not-Aus gesetzt, {n} Position(en) zum Verkauf beauftragt."
    if cmd == "/weiter":
        store.unkill(conn)
        return "✅ Not-Aus aufgehoben, Fehlerzähler zurückgesetzt."
    if cmd in ("/live", "/status"):
        return status_text(conn, cfg)
    if cmd in ("/betrieb", "/bot"):
        return ops_report()
    return HELP


class Service:
    def __init__(self, conn=None, rpc=None, cfg=None):
        from copytrade import chain, store as ct_store  # type: ignore
        from copytrade.config import Settings  # type: ignore

        self.s = Settings()
        self.cfg = cfg or load()
        self.conn = conn or ct_store.connect(self.s)
        store.init(self.conn)
        self.rpc = rpc or chain.Rpc(self.s, url=chain.PUBLIC_RPC)
        self._stop = threading.Event()
        self._data_fails = 0
        self._last_data = 0.0
        self._last_orphans = 0.0

    # -------------------------------------------------------------- watchdog
    def tick(self) -> None:
        c = self.conn
        store.put(c, "heartbeat", int(time.time()))
        self.reconcile()
        if self.cfg.enabled:
            if time.time() - self._last_data >= 60:
                self._last_data = time.time()
                self.data_check()
            for m in ("live", "dry"):
                loss = guard.loss_24h(c, m)
                if loss <= -self.cfg.max_daily_loss_sol:
                    alert_once(c, f"loss:{m}", f"⚠️ {m}: Tagesverlust-Grenze erreicht ({loss:+.3f} SOL in 24 h). "
                                              "Neue Käufe pausiert, bis die 24 h rollen.", 6 * 3600)
            if not self.cfg.dry_run and time.time() - self._last_orphans >= 600:
                self._last_orphans = time.time()
                self.orphans()
        k = store.killed(c)
        if k:
            alert_once(c, "kill", f"🛑 Live-Not-Aus aktiv: {k}. Aufheben mit /weiter.", 6 * 3600)

    def reconcile(self) -> None:
        wait = self.cfg.confirm_timeout_s + 120  # never race the executor while it still confirms
        for o in store.pending(self.conn):
            if time.time() - o["ts"] < wait:
                continue
            try:
                st = self.rpc.call("getSignatureStatuses", [[o["sig"]], {"searchTransactionHistory": True}])
                st = ((st or {}).get("value") or [None])[0]
                height = int(self.rpc.call("getBlockHeight", [{"commitment": "confirmed"}]))
            except Exception as e:  # noqa: BLE001
                log.debug("reconcile %s: %s", o["id"], e)
                continue
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                if st.get("err"):
                    if store.settle(self.conn, o["id"], status="failed", error=f"spät geprüft: {st['err']}"):
                        self._buy_failed(o)
                elif o["side"] == "sell":  # the executor books it at the next close attempt (_late_sell)
                    store.settle(self.conn, o["id"], status="landed_late", error="spät gelandet")
                else:
                    self._book_late_buy(o)
            elif o["last_valid_height"] and height > o["last_valid_height"] + 10:
                if store.settle(self.conn, o["id"], status="expired", error="spät geprüft: nie gelandet"):
                    self._buy_failed(o)

    def _row_for(self, o):
        """The strategy's position row of this buy: same coin, live, written just before the swap, no tokens yet."""
        if o["side"] != "buy":
            return None
        return self.conn.execute(
            "SELECT * FROM ct_positions WHERE mode='live' AND mint=? AND status IN ('opening','failed') "
            "AND COALESCE(tokens,0)=0 AND opened_at BETWEEN ? AND ? ORDER BY id DESC LIMIT 1",
            (o["mint"], o["ts"] - 300, o["ts"] + 60)).fetchone()

    def _buy_failed(self, o) -> None:
        row = self._row_for(o)
        if row is not None and row["status"] == "opening":
            self.conn.execute("UPDATE ct_positions SET status='failed' WHERE id=? AND status='opening'", (row["id"],))
            self.conn.commit()

    def _book_late_buy(self, o) -> None:
        """A buy that landed after the executor gave up: book it into its position row so stops and exits run."""
        from .executor import parse_deltas

        pub = store.get(self.conn, "wallet_pubkey")
        try:
            tx = self.rpc.call("getTransaction", [o["sig"], {"encoding": "jsonParsed", "commitment": "confirmed",
                                                             "maxSupportedTransactionVersion": 0}])
        except Exception as e:  # noqa: BLE001
            log.debug("late buy %s: %s", o["id"], e)
            return  # next round
        d = parse_deltas(tx, pub, o["mint"]) if tx and pub else None
        if not d or d["token_raw"] <= 0:
            return
        row = self._row_for(o)
        dec = d.get("decimals") if d.get("decimals") is not None else 6
        if row is None:
            if store.settle(self.conn, o["id"], status="landed_unbooked", out_raw=d["token_raw"],
                            sol_delta_lamports=d["sol_lamports"], fee_lamports=d["fee"],
                            error="spät gelandet, keine Position gefunden: Wallet prüfen"):
                send(f"⚠️ Kauf {o['mint'][:6]}… ist spät gelandet, aber ohne Position: Token liegen ohne Stop in "
                     "der Wallet. python -m live orphans")
            return
        if not store.settle(self.conn, o["id"], status="landed", out_raw=d["token_raw"],
                            sol_delta_lamports=d["sol_lamports"], fee_lamports=d["fee"], error="spät gelandet"):
            return
        tokens, cost = d["token_raw"] / 10 ** dec, -d["sol_lamports"] / 1e9
        price = cost / tokens
        self.conn.execute(
            "UPDATE ct_positions SET status='open', tokens=?, token_decimals=?, entry_cost_sol=?, entry_price_sol=?, "
            "entry_tx=?, high_price_sol=COALESCE(high_price_sol, ?), last_price_sol=COALESCE(last_price_sol, ?) "
            "WHERE id=? AND status IN ('opening','failed')",
            (tokens, dec, cost, price, o["sig"], price, price, row["id"]))
        self.conn.commit()
        send(f"ℹ️ Kauf {row['symbol'] or o['mint'][:6]} ist spät gelandet und jetzt gebucht ({cost:.3f} SOL); "
             "Stops und Ausstiege laufen.")

    def data_check(self) -> None:
        from copytrade.config import Settings  # type: ignore

        from . import jupiter

        q = jupiter.quote(Settings(), SOL, USDC, 100_000_000, 50)
        if q and int(q.get("outAmount") or 0) > 0:
            self._data_fails = 0
            if store.get(self.conn, "data_down") == "1":
                store.put(self.conn, "data_down", "0")
                send("✅ Marktdaten wieder da, Live-Käufe laufen wieder.")
            return
        self._data_fails += 1
        if self._data_fails >= 3:
            store.put(self.conn, "data_down", "1")
            store.put(self.conn, "data_down_at", int(time.time()))
            store.put(self.conn, "data_down_reason", "Jupiter-Quote fällt aus")
            alert_once(self.conn, "data", "⚠️ Datenausfall (Jupiter): Live-Käufe pausiert.", 3600)

    def orphans(self) -> list[dict]:
        """Tokens in the trading wallet without an open live position (a late landing, a manual buy, an airdrop)."""
        pub = store.get(self.conn, "wallet_pubkey")
        if not pub:
            return []
        held: dict[str, int] = {}
        for prog in TOKEN_PROGRAMS:
            try:
                r = self.rpc.call("getTokenAccountsByOwner", [pub, {"programId": prog},
                                                              {"encoding": "jsonParsed", "commitment": "confirmed"}])
            except Exception as e:  # noqa: BLE001
                log.debug("orphans: %s", e)
                return []
            for a in (r or {}).get("value") or []:
                info = a["account"]["data"]["parsed"]["info"]
                amt = int(info["tokenAmount"]["amount"])
                if amt > 0:
                    held[info["mint"]] = held.get(info["mint"], 0) + amt
        known = {p["mint"] for p in guard.open_positions(self.conn, "live")}
        out = [{"mint": m, "amount": a} for m, a in held.items() if m not in known]
        store.put(self.conn, "orphans", ",".join(o["mint"] for o in out))
        if out:
            alert_once(self.conn, "orphans:" + ",".join(sorted(o["mint"] for o in out))[:80],
                       f"⚠️ {len(out)} Token in der Trading-Wallet ohne offene Position: "
                       + ", ".join(o["mint"][:6] + "…" for o in out[:5]), 24 * 3600)
        return out

    # -------------------------------------------------------------- telegram
    def telegram_loop(self) -> None:
        token, chat = _tg_cfg()
        if not (token and chat) or os.getenv("LIVE_TELEGRAM_COMMANDS", "1").strip().lower() in ("0", "false", "off"):
            return
        import requests

        api = f"https://api.telegram.org/bot{token}"
        while not self._stop.is_set():
            try:
                off = int(store.get(self.conn, "tg_offset") or 0)
                r = requests.get(f"{api}/getUpdates", params={"offset": off, "timeout": 20,
                                                              "allowed_updates": '["message"]'}, timeout=30)
                for u in r.json().get("result", []):
                    store.put(self.conn, "tg_offset", u["update_id"] + 1)
                    m = u.get("message") or {}
                    if str((m.get("chat") or {}).get("id")) != str(chat) or not (m.get("text") or "").startswith("/"):
                        continue  # only your own chat may switch anything
                    send(handle_command(self.conn, self.cfg, m["text"]))
            except Exception as e:  # noqa: BLE001 - never log the URL (token)
                log.debug("telegram poll: %s", type(e).__name__)
                self._stop.wait(10)

    def run(self) -> None:
        threading.Thread(target=self.telegram_loop, daemon=True, name="live-telegram").start()
        log.info("live watchdog: %s, %s", "aus" if not self.cfg.enabled else ("dry-run" if self.cfg.dry_run
                                                                           else "ECHTGELD"), self.cfg.stage.name)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("live tick")
            self._stop.wait(30)

    def stop(self) -> None:
        self._stop.set()
