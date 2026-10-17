"""Coin features from the intel service (intel.features_at), transformed for the model.

Only intel features that are computed from timestamped data <= t are used. Smart-money features from intel are
left out on purpose: before the first model version intel falls back to today's tiers (look-ahead); the model has
its own point-in-time tier features. Unknown values (None, or intel not available) get a neutral value, and
i_coverage says which share of these features was known. Whether they help is decided by the model selection:
every candidate is also tested without the 'intel' group on the newest 30 % of cases.
"""
import logging
import math
import time

log = logging.getLogger("research.intel_features")


def _log(x):
    return math.copysign(math.log1p(abs(x)), x)


def _clip(lo, hi):
    return lambda x: max(lo, min(hi, x))


# (intel name, label, transform, neutral value after transform)
SPEC = (
    ("vol_roundtrip_60m", "Wash/Hin-und-her-Anteil", _clip(0, 1), 0.2),
    ("vol_entities_60m", "Echte Käufer (Personen) 60 min, log", _log, 2.0),
    ("vol_top3_60m", "Top-3-Anteil am Volumen", _clip(0, 1), 0.4),
    ("flow_buy_ratio_15m", "Kaufdruck 15 min", _clip(0, 1), 0.5),
    ("flow_buyers_accel", "Käufer-Beschleunigung", _clip(0, 10), 1.0),
    ("flow_whale_share_60m", "Wal-Anteil an Käufen", _clip(0, 1), 0.3),
    ("flow_retail_buyers_60m", "Kleinanleger-Käufer, log", _log, 1.5),
    ("flow_insider_sell_usd_60m", "Insider-Verkäufe 60 min (log USD)", _log, 0.0),
    ("micro_ofi_5m", "Order-Flow-Imbalance 5 min", _clip(-1, 1), 0.0),
    ("micro_vpin", "Toxizität (VPIN)", _clip(0, 1), 0.3),
    ("micro_absorption", "Absorption", _clip(-5, 5), 0.0),
    ("micro_buy_momentum", "Kauftempo 5 min ggü. davor", _clip(0, 10), 1.0),
    ("tape_hhi", "Halter-Konzentration", _clip(0, 1), 0.1),
    ("tape_new_holders_1h", "Neue Halter 1 h, log", _log, 2.0),
    ("tape_insider_sold_1h", "Insider-Verkauf 1 h", _clip(0, 1), 0.0),
    ("net_dev_rug_rate", "Rug-Rate des Devs", _clip(0, 1), 0.3),
    ("net_bot_share_60m", "Bot-Volumen 60 min", _clip(0, 1), 0.2),
    ("liq_mcap", "Liquidität / Market Cap", _clip(0, 2), 0.15),
    ("liq_chg_15m", "Liquidität 15 min", _clip(-1, 3), 0.0),
    ("exec_rt_0_25", "Kosten hin und zurück 0,25 SOL", _clip(0, 1), 0.05),
    ("hold_top10", "Top-10-Holder", _clip(0, 1), 0.3),
    ("px_vol_15m", "Volatilität 15 min", _clip(0, 5), 0.1),
)
NAMES = ["i_" + s[0] for s in SPEC] + ["i_coverage"]
LABELS = dict([("i_" + s[0], s[1]) for s in SPEC] + [("i_coverage", "Anteil bekannter intel-Merkmale")])


def neutral():
    out = {"i_" + name: nv for name, _, _, nv in SPEC}
    out["i_coverage"] = 0.0
    return out


class Source:
    """Callable (mint, ts) -> dict of model features; switches itself off when intel is not usable."""

    MAX_FAILS = 20

    def __init__(self, conn):
        self.conn = conn
        self.ok = True
        self.cache = {}
        self.fails = 0  # consecutive failures; off only after MAX_FAILS (one locked read must not neutralise a retrain)
        try:
            import intel  # noqa: F401
        except Exception:  # noqa: BLE001
            self.ok = False

    def __call__(self, mint, ts):
        if not self.ok:
            return neutral()
        k = (mint, int(ts) // 60)
        if k in self.cache:
            return self.cache[k]
        import intel
        raw = None
        for attempt in range(3):
            try:
                raw = intel.features_at("coin", mint, int(ts), conn=self.conn).get("features") or {}
                break
            except Exception as e:  # noqa: BLE001
                busy = "locked" in str(e).lower() or "busy" in str(e).lower()
                if busy and attempt < 2:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                self.fails += 1
                if self.fails >= self.MAX_FAILS:
                    log.info("intel features unavailable (%s), using neutral values from now on", str(e)[:120])
                    self.ok = False
                return neutral()  # not cached: the next call tries again
        self.fails = 0
        out, known = {}, 0
        for name, _, fn, nv in SPEC:
            v = raw.get(name)
            if v is None or (isinstance(v, float) and math.isnan(v)):
                out["i_" + name] = nv
            else:
                out["i_" + name] = fn(float(v))
                known += 1
        out["i_coverage"] = known / len(SPEC)
        self.cache[k] = out
        return out
