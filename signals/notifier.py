"""Signal notifiers. Telegram only activates when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set."""
import html
import json
import logging

from core.config import settings
from core.http import post_json

log = logging.getLogger("mayhambot.notify")


class Notifier:
    def send(self, text: str):  # pragma: no cover - interface
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    def send(self, text):
        log.info("SIGNAL\n%s", text)


class TelegramNotifier(Notifier):
    def __init__(self, token, chat_id):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id

    def send(self, text):
        try:
            post_json(self.url, {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                                 "disable_web_page_preview": True})
        except Exception as e:  # noqa: BLE001
            log.warning("telegram send failed: %s", e)
            return False
        return True


def active_notifiers():
    out = [ConsoleNotifier()]
    if settings.telegram_bot_token and settings.telegram_chat_id:
        out.append(TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id))
    return out


def _usd(x):
    return f"${x:,.0f}" if x is not None else "?"


def format_signal(s) -> str:
    wallets = json.loads(s["wallets_json"] or "[]")
    keys = s.keys()
    v2 = "kind" in keys and s["kind"] == "v2"
    head = "🟣 <b>Smart-Money-Akkumulation (v2)" if v2 else "🟢 <b>Smart Money"
    lines = [
        f"{head}: {html.escape(s['symbol'] or s['mint'][:6])}</b>  Score <b>{s['score']:.0f}</b>",
        f"MCap beim Signal {_usd(s['mcap_usd'])} · Preis {s['price_usd'] or 0:.8g} $",
        f"{'Gehaltene Netto-Position' if v2 else 'Netto-Inflow'} {_usd(s['net_usd'])} "
        f"({(s['net_sol'] if 'net_sol' in keys and s['net_sol'] else 0):.1f} SOL)",
        f"Besitzer: {s['n_ultra'] or 0} Ultra · {s['n_smart'] or 0} Smart · Qualität {s['avg_quality']:.0f}" if v2 else
        f"Wallets: {s['n_ultra'] or 0} Ultra · {s['n_smart'] or 0} Smart · {s['n_good'] or 0} Good · Qualität {s['avg_quality']:.0f}"
        if "n_ultra" in keys else f"{s['smart_buyers']} Smart Wallets · Qualität {s['avg_quality']:.0f}",
    ]
    if "liquidity_usd" in keys:
        lines.append(f"Liquidität {_usd(s['liquidity_usd'])}"
                     + (f" · Call Nr. {s['call_no']} auf diesen Coin" if "call_no" in keys and (s["call_no"] or 1) > 1 else ""))
    if "lag_last_s" in keys and s["lag_last_s"] is not None:
        lag = s["lag_last_s"]
        lines.append(f"⏱ letzter Smart-Kauf vor {lag // 60} min {lag % 60} s"
                     + (f" · erster vor {s['lag_first_s'] // 60} min" if v2 and s["lag_first_s"] else "")
                     + (" · Preis vor dem Kauf prüfen" if lag > 600 and not v2 else ""))
    if "target_ret" in keys and s["target_ret"] is not None and s["price_usd"]:
        p0 = s["price_usd"]
        stop = s["stop_ret"] if s["stop_ret"] is not None else None
        lines.append(f"📋 Plan: Einstieg ~{p0:.8g} $ · Ziel <b>{s['target_ret'] * 100:+.0f} %</b> ({p0 * (1 + s['target_ret']):.8g} $)"
                     + (f" · Stopp {stop * 100:.0f} % ({p0 * (1 + stop):.8g} $)" if stop is not None else ""))
    if not v2 and (s["smart_buyers"] or 0) >= 3:  # hypothesis under test (funnel.CONFIDENCE_GROUPS), not a size rule
        lines.append("⭐ Konfidenz hoch: 3+ unabhängige Smart-Gruppen (bisher 33 % davon mit +50 % in 1 h)")
    if "prediction" in keys and s["prediction"] is not None:
        lines.append(f"Vorhersage {s['prediction']*100:.0f}% · {html.escape(s['prediction_label'] or '')}")
    if "ev" in keys and s["ev"] is not None:
        lines.append(f"Erwartung nach Kosten {s['ev']*100:+.0f} % (aus {s['ev_n'] or 0} ähnlichen Setups)")
    if "reason" in keys and s["reason"]:
        lines.append(html.escape(s["reason"]))
    if "contributions_json" in keys and s["contributions_json"]:
        c = json.loads(s["contributions_json"])
        lines.append("Gewichtung: " + " · ".join(f"{k} {v['points']:.0f}" for k, v in c.items()))
    lines.append(f"<code>{s['mint']}</code>")
    for w in wallets[:5]:
        role = " Empfänger" if w.get("role") == "Empfänger" else ""
        lines.append(f"• {w['wallet'][:4]}…{w['wallet'][-4:]} {w.get('tier', '')}{role} Score {w['score']:.0f} "
                     f"{'hat gekauft' if v2 else 'kauft'} {_usd(w['buy_usd'])}")
    lines.append(f'<a href="https://dexscreener.com/solana/{s["mint"]}">DexScreener</a>')
    return "\n".join(lines)


def notify(conn, signals):
    ns = active_notifiers()
    for s in signals:
        text = format_signal(s)
        ok = all(n.send(text) is not False for n in ns)
        conn.execute("UPDATE signals SET notified=? WHERE id=?", (1 if ok else 0, s["id"]))
    conn.commit()


def format_followup(e) -> str:
    name = html.escape(e["symbol"] or e["mint"][:6]) + (" (v2)" if e.get("signal_kind") == "v2" else "")
    call = f" (Call Nr. {e['call_no']})" if (e.get("call_no") or 1) > 1 else ""
    if e["kind"] == "target":
        head = f"🎯 <b>{name}{call}: Ziel {e['ret'] * 100:+.0f} % erreicht</b> nach {e['minutes']} min"
        tail = "Gewinn sichern oder Stopp nachziehen."
    else:
        head = f"🔻 <b>{name}{call}: Stopp {e['ret'] * 100:.0f} % erreicht</b> nach {e['minutes']} min"
        tail = "Laut Plan raus."
    return "\n".join([head, f"Preis {e['price']:.8g} $ · {tail}", f"<code>{e['mint']}</code>",
                      f'<a href="https://dexscreener.com/solana/{e["mint"]}">DexScreener</a>'])


def notify_followups(events):
    ns = active_notifiers()
    for e in events:
        text = format_followup(e)
        for n in ns:
            n.send(text)
