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
    lines = [
        f"🟢 <b>Smart Money: {html.escape(s['symbol'] or s['mint'][:6])}</b>  Score <b>{s['score']:.0f}</b>",
        f"MCap beim Signal {_usd(s['mcap_usd'])} · Preis {s['price_usd'] or 0:.8g} $",
        f"Netto-Inflow {_usd(s['net_usd'])} ({(s['net_sol'] if 'net_sol' in keys and s['net_sol'] else 0):.1f} SOL)",
        f"Wallets: {s['n_ultra'] or 0} Ultra · {s['n_smart'] or 0} Smart · {s['n_good'] or 0} Good · Qualität {s['avg_quality']:.0f}"
        if "n_ultra" in keys else f"{s['smart_buyers']} Smart Wallets · Qualität {s['avg_quality']:.0f}",
    ]
    if "prediction" in keys and s["prediction"] is not None:
        lines.append(f"Vorhersage {s['prediction']*100:.0f}% · {html.escape(s['prediction_label'] or '')}")
    if "contributions_json" in keys and s["contributions_json"]:
        c = json.loads(s["contributions_json"])
        lines.append("Gewichtung: " + " · ".join(f"{k} {v['points']:.0f}" for k, v in c.items()))
    lines.append(f"<code>{s['mint']}</code>")
    for w in wallets[:5]:
        lines.append(f"• {w['wallet'][:4]}…{w['wallet'][-4:]} {w.get('tier', '')} Score {w['score']:.0f} kauft {_usd(w['buy_usd'])}")
    lines.append(f'<a href="https://dexscreener.com/solana/{s["mint"]}">DexScreener</a>')
    return "\n".join(lines)


def notify(conn, signals):
    ns = active_notifiers()
    for s in signals:
        text = format_signal(s)
        ok = all(n.send(text) is not False for n in ns)
        conn.execute("UPDATE signals SET notified=? WHERE id=?", (1 if ok else 0, s["id"]))
    conn.commit()
