"""Organic exits for the signal paper account: the brain's exit model (brain/exits.py) decides when to sell.

Each open signal position in a paper account is shown to the exit model once a minute in the same state tokens the
brain learned from (up since the buy, below the high, the last minute and five, volume against its peak, minutes held,
the other smart wallets' flow). The model only takes over when both hold:
  - it is trained (exits.mature: enough weighted minute samples), and
  - its out-of-sample replays of signal coins beat the plans (exits.verdict(conn, "signal")["live"], every 10 min).
Otherwise `active()` is False and the account's own exits (target, trail, inflow, smart exit) run exactly as before.
The account's hard guards stay either way: rug check, its stop-loss and its maximum hold (strategy.update).

Every decision is logged with its reason (copytrade.log); every organic sell and every switch of the gate is also
an event in ct_events.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from . import store

log = logging.getLogger("copytrade.organic")

GATE_EVERY_S = 600  # verdict re-check
RELOAD_EVERY_S = 600  # exit model file re-check (the brain service saves it as it learns)


class OrganicExit:
    def __init__(self, conn, s, *, exits_mod=None, brain_settings=None, clock=time.time):
        self.conn, self.s, self.clock = conn, s, clock
        self._exits = exits_mod
        self._bs = brain_settings
        self.xm = None
        self._mtime = None
        self._loaded_at = 0.0
        self._gate = (0.0, False, {})
        self.live = None
        self.decisions = {"hold": 0, "half": 0, "sell": 0}
        self.broken = False

    # ------------------------------------------------------------------ wiring (lazy: numpy, brain package)
    def _mods(self) -> bool:
        if self.broken:
            return False
        if self._exits is None or self._bs is None:
            try:
                from brain import config as bconfig  # type: ignore
                from brain import exits  # type: ignore
            except Exception as e:
                log.info("organic exits unavailable (%s): the account keeps its own exits", e)
                self.broken = True
                return False
            self._exits = self._exits or exits
            self._bs = self._bs or bconfig.load()
        if self.live is None:
            self.live = self._exits.Live()
        return True

    def model_path(self) -> Path:
        return Path(self.s.db_path).parent / "brain" / "exit_model.npz"

    def _load(self, now: float) -> None:
        if self.xm is not None and now - self._loaded_at < RELOAD_EVERY_S:
            return
        self._loaded_at = now
        path = self.model_path()
        try:
            mt = path.stat().st_mtime
        except OSError:
            self.xm = None
            return
        if mt == self._mtime and self.xm is not None:
            return
        try:
            from brain.model import Brain  # type: ignore

            self.xm = Brain.load(path, 1, self._bs.half_life_h)
            self._mtime = mt
        except Exception as e:
            log.info("exit model %s not loaded: %s", path, e)
            self.xm = None

    # ------------------------------------------------------------------ gate
    def active(self, now: float | None = None) -> bool:
        """True only while the exit model is trained and its replays beat the plans."""
        now = now or self.clock()
        if not self._mods():
            return False
        at, on, v = self._gate
        if now - at < GATE_EVERY_S:
            return on
        self._load(now)
        mature = self._exits.mature(self.xm)
        try:
            v = self._exits.verdict(self.conn, "signal") if mature else {}  # only signal coins count
        except Exception as e:
            log.debug("exit verdict failed: %s", e)
            v = {}
        new = bool(mature and v.get("live"))
        if new != on:
            org, plan = v.get("organic"), v.get("plan")
            why = (f"Modell reif, Replays organisch {org:+.1%} gegen Pläne {plan:+.1%} (n {v.get('n')})"
                   if new and org is not None and plan is not None else
                   "Modell noch nicht reif" if not mature else
                   f"Replays schlagen die Pläne nicht (n {v.get('n', 0)})")
            log.info("organic exits for the signal paper account %s: %s", "ON" if new else "off", why)
            try:
                store.event(self.conn, "organic", f"Organischer Ausstieg (Signal-Papier) {'an' if new else 'aus'}: "
                            f"{why}", data={"on": new, **{k: v.get(k) for k in ("n", "organic", "plan")}})
            except Exception as e:
                log.debug("organic event failed: %s", e)
        self._gate = (now, new, v)
        return new

    # ------------------------------------------------------------------ one position
    def observe(self, p, snap, now: float) -> None:
        if self.live is not None and snap and snap.price_sol:
            self.live.observe(p["id"], now, snap.price_sol, getattr(snap, "volume_m5", None))

    def decide(self, p, snap, now: float, price: float, high: float) -> tuple[str, str] | None:
        """("sell" | "half" | "hold", reason) once a minute per position, None between checks."""
        ex = self._exits
        if self.live is None or not self.live.due(p["id"], now):
            return None
        entry = p["entry_price_sol"]
        held = (now - p["opened_at"]) / 60
        ago = self.live.price_ago(p["id"], now, 60)
        vpeak = self.live.vpeak.get(p["id"], 0.0)
        try:
            from brain.features import liq_bucket  # type: ignore

            liq = liq_bucket(snap)
        except Exception:
            liq = None
        swaps = ex.coin_swaps(self.conn, p["mint"], p["opened_at"] - 60, now)
        toks = ex.tokens("signal", liq, price / entry - 1, 1 - price / high if high > 0 else 0.0,
                         price / ago - 1 if ago else None,
                         snap.change_m5 / 100 if getattr(snap, "change_m5", None) is not None else None,
                         (snap.volume_m5 or 0) / vpeak if vpeak > 0 else None, held, None,
                         ex.smart_flow(swaps, p["opened_at"], now, None))
        act, mean, pp = ex.advise(self.xm, toks, self._bs, True, price / entry - 1)  # no partial sells here
        self.decisions[act] = self.decisions.get(act, 0) + 1
        why = (f"nächste {ex.FWD_MIN} min {mean:+.1%} erwartet, P(steigt) {pp:.0%}, "
               f"{price / entry - 1:+.1%} seit Kauf, {held:.0f} min gehalten")
        log.info("organic %s %s: %s [%s]", act, p["symbol"] or p["mint"][:6], why, " ".join(toks[:9]))
        return act, why

    def drop(self, pid: int) -> None:
        if self.live is not None:
            self.live.drop(pid)
