"""Model settings from .env: tier rules, horizons, window, retraining. All transparent and configurable."""
import os
from dataclasses import asdict, dataclass, field

import core.config  # noqa: F401  (loads mayhambot/.env)


def _f(name, default):
    v = os.getenv(name)
    return float(v) if v not in (None, "") else float(default)


def _horizons():
    raw = os.getenv("MODEL_HORIZONS") or "15m:0.3,1h:0.5,6h:1.0"
    out = []
    for part in raw.split(","):
        name, thr = part.split(":")
        name = name.strip()
        sec = int(name[:-1]) * {"m": 60, "h": 3600, "d": 86400}[name[-1]]
        out.append((name, sec, float(thr)))
    return out


TIERS = ("ultra", "smart", "good")
TIER_LABELS = {"ultra": "Ultra Smart", "smart": "Smart", "good": "Good"}

PRIOR_WALLET_WEIGHTS = dict(win=0.17, early=0.17, roi=0.13, consistency=0.10, timing=0.08, lead=0.12,
                            influence=0.13, rug_avoid=0.10)


@dataclass
class TierRule:
    min_score: float
    min_tokens: int
    min_win_rate: float
    max_rug_rate: float
    min_pnl_usd: float = 0.0


@dataclass
class ModelSettings:
    rules: dict = field(default_factory=lambda: {
        "ultra": TierRule(_f("MODEL_ULTRA_MIN_SCORE", 72), int(_f("MODEL_ULTRA_MIN_TOKENS", 6)),
                          _f("MODEL_ULTRA_MIN_WINRATE", 0.45), _f("MODEL_MAX_RUG_RATE", 0.35)),
        "smart": TierRule(_f("MODEL_SMART_MIN_SCORE", 60), int(_f("MODEL_SMART_MIN_TOKENS", 4)),
                          _f("MODEL_SMART_MIN_WINRATE", 0.35), _f("MODEL_MAX_RUG_RATE", 0.35)),
        "good": TierRule(_f("MODEL_GOOD_MIN_SCORE", 48), int(_f("MODEL_GOOD_MIN_TOKENS", 3)),
                         _f("MODEL_GOOD_MIN_WINRATE", 0.25), _f("MODEL_GOOD_MAX_RUG_RATE", 0.5)),
    })
    horizons: list = field(default_factory=_horizons)          # [(name, seconds, pump threshold)]
    # rank caps: only the best N per tier, the rest drops to the next tier (0 = no cap)
    caps: dict = field(default_factory=lambda: {"ultra": int(_f("MODEL_ULTRA_MAX", 40)),
                                                "smart": int(_f("MODEL_SMART_MAX", 150)),
                                                "good": int(_f("MODEL_GOOD_MAX", 0))})
    window_min: int = int(_f("MODEL_WINDOW_MIN", 30))
    event_min_score: float = _f("MODEL_EVENT_MIN_SCORE", 40)  # untiered wallets this good also trigger research events
    event_dedupe_min: int = int(_f("MODEL_EVENT_DEDUPE_MIN", 10))
    retrain_hours: float = _f("MODEL_RETRAIN_HOURS", 6)
    wallet_weight_prior_n: float = _f("MODEL_WALLET_WEIGHT_PRIOR_N", 400)  # evidence needed to trust learned weights 50 %
    min_train_events: int = int(_f("MODEL_MIN_TRAIN_EVENTS", 40))
    min_train_pos: int = int(_f("MODEL_MIN_TRAIN_POSITIVES", 6))
    l2: float = _f("MODEL_L2", 5.0)

    def rules_dict(self):
        return {k: dict(asdict(v), max_wallets=self.caps.get(k, 0)) for k, v in self.rules.items()}


msettings = ModelSettings()
