"""Known rug wallets: serial launchers, bundle/dev wallets, insiders, their funders, block-0 snipers and the setup wallets of the brand scam (alone a veto).

Two sources, merged: the curated file safety/bad_wallets.txt (versioned, `address  role  note`) and the table
safety_bad_wallets (runtime additions: `python -m safety watch add <ADDRESS> <role> "note"`). Exchange hot wallets
from exchanges.txt are dropped on load whatever the source says: an exchange withdrawal funds thousands of
unrelated wallets (the 2026-10-17 chain scan named Binance 5tzFki… as a 'bundle funder')."""
import os
import time

from safety import store
from safety.checks import load_exchanges

ROLES = {
    "launcher": "Serien-Launcher",
    "bundle": "Bundle-/Dev-Wallet",
    "insider": "Insider",
    "funder": "Geldgeber von Bundle-Wallets",
    "sniper0": "Block-0-Käufer",
    "setup": "Setup-Wallet der Marken-Masche",
}
FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bad_wallets.txt")
_cache = {"key": None, "file": {}}


def _file(path):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _cache["key"] != (path, mtime):
        out = {}
        with open(path) as f:
            for line in f:
                parts = line.split("#")[0].split(None, 2)
                if len(parts) >= 2 and parts[1] in ROLES:
                    out[parts[0]] = {"role": parts[1], "note": parts[2].strip() if len(parts) > 2 else ""}
        _cache.update(key=(path, mtime), file=out)
    return _cache["file"]


def load(conn=None, cfg=None):
    """address -> {role, note}; table entries override the file, exchanges never appear."""
    out = dict(_file(getattr(cfg, "bad_wallets_file", None) or FILE))
    if conn is not None:
        try:
            store.ensure_schema(conn)
            for r in conn.execute("SELECT address, role, note FROM safety_bad_wallets"):
                if r[1] in ROLES:
                    out[r[0]] = {"role": r[1], "note": r[2] or ""}
        except Exception:  # noqa: BLE001
            pass
    ex = load_exchanges(cfg.exchanges_file) if cfg is not None else {}
    return {a: v for a, v in out.items() if a not in ex}


def add(conn, address, role, note=""):
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    store.ensure_schema(conn)
    conn.execute("INSERT OR REPLACE INTO safety_bad_wallets(address, role, note, added_at) VALUES (?,?,?,?)", (address, role, note, int(time.time())))
    conn.commit()


def remove(conn, address):
    store.ensure_schema(conn)
    conn.execute("DELETE FROM safety_bad_wallets WHERE address=?", (address,))
    conn.commit()
