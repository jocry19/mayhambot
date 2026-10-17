"""Operating modes, real-money presets and manual commands.

Everything here lives in the database, so the dashboard, the terminal (cli/) and
the trading loop share it without restarts. Three strategies:

  whale   mode 1: copy the whales that trigger the best copy-trading pumps, sell into the first pump
  signal  mode 2: buy the best smart-money signals of the signal engine with your preset
  launch  mode 3: buy the launches the sniper decided to enter (sniper_launches.decision='enter'), sniper exits

Each strategy has an operating mode:
  off     nothing
  manual  only alerts (ct_alerts), no trades; you buy yourself or click "kaufen"
  semi    the bot buys, YOU sell (the bot only shows exit hints)
  auto    the bot buys and sells

Real money needs both: LIVE_TRADING=true plus a key in .env (master switch, only by hand)
AND live switched on for that strategy here. Otherwise the strategy trades on paper.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, fields

STRATEGIES = ("whale", "signal", "launch")
MODES = ("off", "manual", "semi", "auto")
MODE_LABELS = {"off": "Aus", "manual": "Manuell (nur Signale)", "semi": "Halbautomatisch (Bot kauft, du verkaufst)",
               "auto": "Automatisch (Bot kauft und verkauft)"}
STRATEGY_LABELS = {"whale": "Modus 1 · Wal-Pumps kopieren", "signal": "Modus 2 · Smart-Money-Signale kaufen",
                   "launch": "Modus 3 · Launch-Sniper-Käufe", "manual": "Manuell gekauft"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ct_control (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS ct_presets (name TEXT PRIMARY KEY, data TEXT, builtin INTEGER DEFAULT 0, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS ct_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, action TEXT, position_id INTEGER, mint TEXT,
    size_sol REAL, strategy TEXT, status TEXT DEFAULT 'pending', result TEXT, done_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ct_commands_status ON ct_commands(status);
CREATE TABLE IF NOT EXISTS ct_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, strategy TEXT, mint TEXT, symbol TEXT, wallet TEXT,
    price_sol REAL, price_usd REAL, mcap_usd REAL, score REAL, action TEXT, reason TEXT, data TEXT,
    position_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ct_alerts_ts ON ct_alerts(ts);
"""


@dataclass
class Preset:
    """Risk and exit rules. Fractions: 0.2 = 20 %."""
    size_sol: float = 0.5  # per trade
    max_open: int = 5
    max_daily_loss_sol: float = 2.0
    take_profit: float = 0.5  # signal strategy; whale uses the per-wallet learned target if learned_tp
    learned_tp: bool = True  # whale: use the target learned per wallet
    stop_loss: float = 0.2
    trail_activate: float = 0.2
    trail_drawdown: float = 0.12
    max_hold_min: float = 240
    min_score: float = 60  # signal: minimum score of the signal engine (0-100), used while the model is untrained
    min_model_score: float = 30  # signal: minimum model score = P(pump within 1 h) x 100 (grade C from 30)
    require_model: bool = False  # signal: buy only with a trained model prediction
    min_smart_buyers: int = 2  # signal only
    min_liquidity_usd: float = 15000
    max_mcap_usd: float = 20_000_000
    max_signal_age_s: float = 180  # signal only: older signals are not bought
    slippage_bps: int = 300

    @classmethod
    def from_dict(cls, d: dict) -> "Preset":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in names})


BUILTIN = {
    "vorsichtig": Preset(size_sol=0.1, max_open=3, max_daily_loss_sol=0.5, take_profit=0.3, stop_loss=0.12,
                         trail_activate=0.15, trail_drawdown=0.08, max_hold_min=90, min_score=70, min_model_score=45,
                         min_smart_buyers=3, min_liquidity_usd=30000),
    "standard": Preset(),
    "aggressiv": Preset(size_sol=1.0, max_open=8, max_daily_loss_sol=5.0, take_profit=1.0, stop_loss=0.3,
                        trail_activate=0.3, trail_drawdown=0.18, max_hold_min=480, min_score=50, min_model_score=20,
                        min_smart_buyers=2, min_liquidity_usd=10000),
}

DEFAULTS = {
    "mode:whale": "auto", "mode:signal": "auto",  # in paper mode everything trades, so the stats fill up
    "mode:launch": "off",  # sniper/ already paper-trades every launch it enters; this mode is for real buys later
    "preset:whale": "standard", "preset:signal": "standard", "preset:launch": "vorsichtig",
    "live:whale": "0", "live:signal": "0", "live:launch": "0",
    "paused": "0",
    "autotune:whale": "1", "autotune:signal": "1",  # self-learning exits/filters (copytrade/learn.py)
    "autotune:launch": "0",  # exits come from sniper/ (SNIPER_* settings), not from the grid
}


def init(conn) -> None:
    conn.executescript(SCHEMA)
    now = int(time.time())
    for name, p in BUILTIN.items():
        conn.execute("INSERT OR IGNORE INTO ct_presets(name, data, builtin, updated_at) VALUES (?,?,1,?)", (name, json.dumps(asdict(p)), now))
    for k, v in DEFAULTS.items():
        conn.execute("INSERT OR IGNORE INTO ct_control(key, value, updated_at) VALUES (?,?,?)", (k, v, now))
    conn.commit()


def get(conn, key: str) -> str:
    r = conn.execute("SELECT value FROM ct_control WHERE key=?", (key,)).fetchone()
    return r[0] if r else DEFAULTS.get(key, "")


def set_value(conn, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO ct_control(key, value, updated_at) VALUES (?,?,?)", (key, str(value), int(time.time())))
    conn.commit()


# ---------------------------------------------------------------- modes
def mode(conn, strategy: str) -> str:
    m = get(conn, f"mode:{strategy}")
    return m if m in MODES else "off"


def set_mode(conn, strategy: str, m: str) -> None:
    if strategy not in STRATEGIES or m not in MODES:
        raise ValueError(f"unknown strategy/mode {strategy}/{m}")
    set_value(conn, f"mode:{strategy}", m)


def paused(conn) -> bool:
    return get(conn, "paused") == "1"


def set_paused(conn, on: bool) -> None:
    set_value(conn, "paused", "1" if on else "0")


def live_wanted(conn, strategy: str) -> bool:
    return get(conn, f"live:{strategy}") == "1"


def set_live(conn, strategy: str, on: bool) -> None:
    set_value(conn, f"live:{strategy}", "1" if on else "0")


# ---------------------------------------------------------------- presets
def presets(conn) -> dict[str, Preset]:
    return {r[0]: Preset.from_dict(json.loads(r[1])) for r in conn.execute("SELECT name, data FROM ct_presets ORDER BY name")}


def preset(conn, strategy: str) -> Preset:
    name = get(conn, f"preset:{strategy}")
    r = conn.execute("SELECT data FROM ct_presets WHERE name=?", (name,)).fetchone()
    return Preset.from_dict(json.loads(r[0])) if r else BUILTIN["standard"]


def save_preset(conn, name: str, p: Preset) -> None:
    conn.execute("INSERT OR REPLACE INTO ct_presets(name, data, builtin, updated_at) VALUES (?,?,0,?)", (name, json.dumps(asdict(p)), int(time.time())))
    conn.commit()


def use_preset(conn, strategy: str, name: str) -> None:
    if not conn.execute("SELECT 1 FROM ct_presets WHERE name=?", (name,)).fetchone():
        raise ValueError(f"unknown preset {name}")
    set_value(conn, f"preset:{strategy}", name)


# ---------------------------------------------------------------- commands (dashboard/terminal -> trading loop)
def request_sell(conn, position_id: int) -> int:
    cur = conn.execute("INSERT INTO ct_commands(ts, action, position_id) VALUES (?, 'sell', ?)",
                       (int(time.time()), position_id))
    conn.commit()
    return cur.lastrowid


def request_buy(conn, mint: str, size_sol: float | None = None, strategy: str = "manual") -> int:
    cur = conn.execute("INSERT INTO ct_commands(ts, action, mint, size_sol, strategy) VALUES (?, 'buy', ?, ?, ?)",
                       (int(time.time()), mint, size_sol, strategy))
    conn.commit()
    return cur.lastrowid


COMMAND_TTL_S = {"buy": 120, "sell": 900}  # a buy click is about that moment's price; a sell stays wanted longer


def pending_commands(conn) -> list:
    """Pending commands; ones older than their TTL expire (a buy clicked 10 min ago must not fill at today's price)."""
    now = int(time.time())
    for action, ttl in COMMAND_TTL_S.items():
        conn.execute("UPDATE ct_commands SET status='expired', result='abgelaufen', done_at=? "
                     "WHERE status='pending' AND action=? AND ts<?", (now, action, now - ttl))
    conn.commit()
    return conn.execute("SELECT * FROM ct_commands WHERE status='pending' ORDER BY id").fetchall()


def finish_command(conn, cid: int, status: str, result: str = "") -> None:
    conn.execute("UPDATE ct_commands SET status=?, result=?, done_at=? WHERE id=?",
                 (status, result, int(time.time()), cid))
    conn.commit()


def alert(conn, strategy: str, mint: str, symbol: str, action: str, reason: str, *, wallet: str = "",
          price_sol: float = 0, price_usd: float = 0, mcap_usd: float = 0, score: float = 0,
          data: dict | None = None, position_id: int | None = None) -> int:
    """action: 'bought' | 'signal' (manual mode, not bought) | 'skipped'"""
    cur = conn.execute(
        """INSERT INTO ct_alerts(ts, strategy, mint, symbol, wallet, price_sol, price_usd, mcap_usd, score,
             action, reason, data, position_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (int(time.time()), strategy, mint, symbol, wallet, price_sol, price_usd, mcap_usd, score, action, reason,
         json.dumps(data or {}), position_id),
    )
    conn.commit()
    return cur.lastrowid
