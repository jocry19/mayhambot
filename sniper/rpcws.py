"""Free trade stream from the Solana RPC websocket: one logsSubscribe per watched mint (or one on the program), pump.fun's
TradeEvent decoded from the "Program data:" log line (mint, SOL and token amount, side, trader, curve
reserves). Used when PumpPortal refuses per-token trades without a funded API key. No keys needed."""
import asyncio
import base64
import hashlib
import json
import logging
import struct
from collections import deque

log = logging.getLogger("sniper.rpcws")
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TRADE_DISC = hashlib.sha256(b"event:TradeEvent").digest()[:8]
CREATE_DISC = hashlib.sha256(b"event:CreateEvent").digest()[:8]
# pump.fun's mint authority signs into every create (and nothing else): mentioning it streams creates only
PUMP_MINT_AUTHORITY = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"
PUMP_INITIAL_V_SOL, PUMP_INITIAL_V_TOK = 30.0, 1_073_000_000.0
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58(b):
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(b) - len(b.lstrip(b"\0"))
    return "1" * pad + out


def decode_trade(data_b64):
    """pump.fun TradeEvent -> dict, or None for other events. Layout (Anchor): disc 8, mint 32, sol_amount u64,
    token_amount u64, is_buy bool, user 32, timestamp i64, virtual_sol_reserves u64, virtual_token_reserves u64."""
    try:
        raw = base64.b64decode(data_b64)
    except (ValueError, TypeError):
        return None
    if len(raw) < 113 or raw[:8] != TRADE_DISC:
        return None
    mint = b58(raw[8:40])
    sol, tok = struct.unpack_from("<QQ", raw, 40)
    is_buy = raw[56] == 1
    user = b58(raw[57:89])
    ts, vsol, vtok = struct.unpack_from("<qQQ", raw, 89)
    return {"mint": mint, "sol_lamports": sol, "token_raw": tok, "is_buy": is_buy, "user": user, "ts": ts,
            "v_sol_lamports": vsol, "v_tok_raw": vtok}


def to_stream_msg(ev, signature, slot=None):
    """Same shape as a PumpPortal trade message, so the engine needs no second code path."""
    v_sol, v_tok = ev["v_sol_lamports"] / 1e9, ev["v_tok_raw"] / 1e6
    return {"signature": signature, "mint": ev["mint"], "traderPublicKey": ev["user"],
            "txType": "buy" if ev["is_buy"] else "sell", "tokenAmount": ev["token_raw"] / 1e6,
            "solAmount": ev["sol_lamports"] / 1e9, "newTokenBalance": None, "vSolInBondingCurve": v_sol,
            "vTokensInBondingCurve": v_tok, "marketCapSol": v_sol / v_tok * 1e9 if v_tok else 0.0,
            "pool": "pump", "slot": slot, "chainTs": ev["ts"], "src": "rpc_logs"}


def decode_create(data_b64):
    """pump.fun CreateEvent -> dict: name, symbol, uri (u32-length strings), mint, bonding_curve, user (32 each),
    then in newer program versions creator, timestamp, virtual/real reserves (read only when present)."""
    try:
        raw = base64.b64decode(data_b64)
    except (ValueError, TypeError):
        return None
    if len(raw) < 8 or raw[:8] != CREATE_DISC:
        return None
    o, out = 8, {}
    try:
        for k in ("name", "symbol", "uri"):
            n = struct.unpack_from("<I", raw, o)[0]
            if n > 2000:
                return None
            out[k] = raw[o + 4:o + 4 + n].decode("utf-8", "replace")
            o += 4 + n
        for k in ("mint", "bonding_curve", "user"):
            out[k] = b58(raw[o:o + 32])
            o += 32
        if len(raw) >= o + 32:
            out["creator"] = b58(raw[o:o + 32])
            o += 32
        if len(raw) >= o + 24:
            out["ts"], out["v_tok_raw"], out["v_sol_lamports"] = struct.unpack_from("<qQQ", raw, o)
    except struct.error:
        return None
    return out if len(out.get("mint", "")) >= 32 else None


def parse_creates(m):
    """logsNotification of a create tx -> list of PumpPortal-shaped create messages. The dev's first buy in the
    same tx sets initialBuy and the curve state after it (it is not fed again as a trade)."""
    if m.get("method") != "logsNotification":
        return []
    res = (m.get("params") or {}).get("result") or {}
    v = res.get("value") or {}
    if v.get("err"):
        return []
    creates, trades = [], []
    for line in v.get("logs") or []:
        if line.startswith("Program data: "):
            d = line[14:].strip()
            c = decode_create(d)
            if c:
                creates.append(c)
                continue
            t = decode_trade(d)
            if t:
                trades.append(t)
    out = []
    for c in creates:
        dev = c.get("creator") or c["user"]
        buy = next((t for t in trades if t["mint"] == c["mint"] and t["is_buy"]), None)
        if buy:
            v_sol, v_tok = buy["v_sol_lamports"] / 1e9, buy["v_tok_raw"] / 1e6
        elif c.get("v_tok_raw"):
            v_sol, v_tok = c["v_sol_lamports"] / 1e9, c["v_tok_raw"] / 1e6
        else:
            v_sol, v_tok = PUMP_INITIAL_V_SOL, PUMP_INITIAL_V_TOK
        out.append({"signature": v.get("signature") or "", "mint": c["mint"], "traderPublicKey": dev, "txType": "create",
                    "initialBuy": buy["token_raw"] / 1e6 if buy else 0.0, "solAmount": buy["sol_lamports"] / 1e9 if buy else 0.0,
                    "bondingCurveKey": c["bonding_curve"], "vTokensInBondingCurve": v_tok, "vSolInBondingCurve": v_sol,
                    "marketCapSol": v_sol / v_tok * 1e9 if v_tok else 0.0, "name": c["name"], "symbol": c["symbol"],
                    "uri": c["uri"], "pool": "pump", "slot": (res.get("context") or {}).get("slot"), "src": "rpc_logs"})
    return out


def parse_notification(m):
    """logsNotification -> list of stream messages (one tx can hold several trades)."""
    if m.get("method") != "logsNotification":
        return []
    res = (m.get("params") or {}).get("result") or {}
    v = res.get("value") or {}
    if v.get("err"):
        return []
    slot = (res.get("context") or {}).get("slot")
    out = []
    for line in v.get("logs") or []:
        if line.startswith("Program data: "):
            ev = decode_trade(line[14:].strip())
            if ev:
                out.append(to_stream_msg(ev, v.get("signature") or "", slot))
    return out


class LogStream:
    """One websocket, logsSubscribe per wanted account ("mints" mode) or a single subscription on a program
    ("program" mode, messages filtered locally to the wanted mints). The public RPC limits subscription
    *attempts* per connection and closes with 1013 "Too many subscriptions attempted": requests are paced, the
    back-off only resets after a connection stayed up, and mode "auto" moves to one program-wide subscription
    after repeated rate-limit closes, so the per-coin churn stops entirely."""
    RATE_LIMIT_SWITCH, RATE_LIMIT_WINDOW = 3, 600
    BUFFER_SEC = 15

    def __init__(self, url, on_msg, max_subs=150, parser=None, name="RPC-Log-Datenstrom", stale_sec=None,
                 subs_per_sec=4.0, mode="mints", program=None):
        self.url, self.on_msg, self.max_subs, self.stale_sec = url, on_msg, max_subs, stale_sec
        self.subs_per_sec = subs_per_sec
        self.parser, self.name = parser or parse_notification, name
        self.auto = mode == "auto"
        self.mode = "mints" if self.auto else mode
        self.program = program
        self.want, self.subs = set(), {}       # account -> subscription id
        self.want_ts = {}                      # account -> when it was first wanted
        self.pending = {}                      # request id -> account
        self.next_id = 1
        self.connected = False
        self.msgs = 0
        self.attempts = 0                      # logsSubscribe requests sent (all connections)
        self.rejects = 0                       # refused subscriptions + rate-limit closes
        self.rate_limit_log = deque(maxlen=50)
        self.reconnects = 0
        self.server_drops = 0                  # closes by the RPC with an invalid close code (1002)
        self.recent, self._buffered, self.replayed = {}, 0, 0   # program mode: recent trades of unwanted coins
        self._program_since = None
        self.ws = None
        self._tokens, self._last = 0.0, None
        self.on_mode = None                    # callback(mode) when auto switches (the service persists it)

    def subscribe(self, mints, now=None):
        import time as _t
        now = now or _t.time()
        new = set(mints) - self.want
        for m in new:
            self.want_ts[m] = now
        self.want |= set(mints)
        # program mode: trades of a coin that came in before we wanted it (bundles in the creation slot, the
        # seconds until the create reached us) are replayed from the buffer, so the stream has them all
        for m in new:
            for ts, msg in self.recent.pop(m, ()):
                if now - ts <= self.BUFFER_SEC:
                    self.replayed += 1
                    self.on_msg(msg)

    def unsubscribe(self, mints):
        self.want -= set(mints)
        for m in mints:
            self.want_ts.pop(m, None)

    def is_live(self, mint):
        """Trades of this mint actually arrive (its own subscription, or the program-wide one, is confirmed)."""
        return (self.program in self.subs) if self.mode == "program" else (mint in self.subs)

    def not_live(self, now, grace=30):
        """Mints wanted for longer than `grace` seconds without a confirmed subscription (refused, over the
        subscription limit, or the connection is down)."""
        return [m for m in list(self.want_ts) if now - self.want_ts[m] > grace and not self.is_live(m)]

    def active_subs(self):
        return len(self.subs)

    def _targets(self):
        return {self.program} if self.mode == "program" else self.want

    def _allow(self, now):
        if self._last is None:
            self._tokens, self._last = 1.0, now
        self._tokens = min(max(1.0, self.subs_per_sec), self._tokens + (now - self._last) * self.subs_per_sec)
        self._last = now
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False

    async def _sync(self, ws, now=None):
        import time as _t
        now = _t.monotonic() if now is None else now
        targets = self._targets()
        for m in [m for m in targets if m not in self.subs and m not in self.pending.values()]:
            if len(self.subs) + len(self.pending) >= self.max_subs or not self._allow(now):
                break
            rid, self.next_id = self.next_id, self.next_id + 1
            self.pending[rid] = m
            self.attempts += 1
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "logsSubscribe",
                                      "params": [{"mentions": [m]}, {"commitment": "processed"}]}))
        for m in [m for m in self.subs if m not in targets]:
            sid = self.subs.pop(m)
            rid, self.next_id = self.next_id, self.next_id + 1
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "logsUnsubscribe", "params": [sid]}))

    def note_rate_limit(self, now):
        self.rejects += 1
        self.rate_limit_log.append(now)
        recent = sum(1 for t in self.rate_limit_log if now - t <= self.RATE_LIMIT_WINDOW)
        if self.auto and self.mode == "mints" and self.program and recent >= self.RATE_LIMIT_SWITCH:
            self.mode = "program"
            if self.on_mode:
                try:
                    self.on_mode("program")
                except Exception:  # noqa: BLE001
                    pass
            log.warning("%s: %d Rate-Limit-Abbrüche in %d min, wechsle auf ein einziges Abo auf Programmebene",
                        self.name, recent, self.RATE_LIMIT_WINDOW // 60)

    def deliver(self, m, now=None):
        msgs = self.parser(m)
        if not msgs:
            return
        import time as _t
        now = now or _t.time()
        for msg in msgs:
            if self.mode == "program" and msg.get("mint") not in self.want:
                self._buffer(msg, now)
                continue
            self.on_msg(msg)

    def _buffer(self, msg, now):
        q = self.recent.get(msg.get("mint"))
        if q is None:
            q = self.recent[msg.get("mint")] = deque(maxlen=200)
        msg["_rx"] = now                  # when it arrived; the replay keeps the real timing
        q.append((now, msg))
        self._buffered += 1
        if self._buffered % 2000 == 0:   # drop coins nobody asked for within the buffer time
            cut = now - self.BUFFER_SEC
            for k in [k for k, v in self.recent.items() if not v or v[-1][0] < cut]:
                del self.recent[k]

    def program_complete_since(self):
        """Since when the program-wide subscription delivers every pump.fun trade (None if it does not)."""
        return self._program_since if self.mode == "program" and self.program in self.subs else None

    async def run(self, stop):
        import time as _t
        import websockets
        backoff = 2
        while not stop.is_set():
            up_since = None
            try:
                async with websockets.connect(self.url, ping_interval=20, ping_timeout=20, max_size=2 ** 22,
                                              open_timeout=15) as ws:
                    self.connected, self.subs, self.pending = True, {}, {}
                    self._program_since = None
                    up_since = _t.monotonic()
                    self.reconnects += 1
                    log.info("%s verbunden (%s, %s)", self.name, self.url.split("?")[0],
                             "Programm-Abo" if self.mode == "program" else "Abos je Coin")

                    async def syncer():
                        while True:
                            await self._sync(ws)
                            await asyncio.sleep(0.25)
                    task = asyncio.create_task(syncer())
                    try:
                        while True:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=self.stale_sec or 3600)
                            except asyncio.TimeoutError:
                                if self.stale_sec and self.subs:
                                    log.warning("%s: %d s keine Daten, verbinde neu", self.name, self.stale_sec)
                                    break
                                continue
                            self.msgs += 1
                            try:
                                m = json.loads(raw)
                            except ValueError:
                                continue
                            if "id" in m and m.get("id") in self.pending:
                                acc = self.pending.pop(m["id"])
                                if "result" in m:
                                    self.subs[acc] = m["result"]
                                    if acc == self.program:
                                        self._program_since = _t.time()
                                else:
                                    self.rejects += 1
                                    log.warning("logsSubscribe abgelehnt: %s", str(m.get("error"))[:160])
                                    if self.mode != "program":
                                        self.want.discard(acc)   # want_ts stays: the service sees it as not live
                                continue
                            self.deliver(m)
                            if stop.is_set():
                                break
                    finally:
                        task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                txt = str(e)
                if "1002" in txt and up_since is not None and _t.monotonic() - up_since >= 30:
                    # the public RPC ends long, busy streams with a close frame carrying an invalid status code
                    # (websockets answers 1002); nothing on our side: reconnect quickly, the syncer resubscribes
                    self.server_drops += 1
                    backoff = 2
                    log.info("%s vom RPC getrennt (1002, ohne gültigen Abschluss), verbinde neu", self.name)
                elif "1013" in txt or "Too many subscriptions" in txt:
                    self.note_rate_limit(_t.monotonic())
                    backoff = max(backoff, 15)
                    log.warning("%s: Rate-Limit beim RPC (%d Abos aktiv), neuer Versuch in %d s",
                                self.name, len(self.subs), backoff)
                else:
                    log.warning("%s getrennt (%s), neuer Versuch in %d s", self.name, e, backoff)
            self.connected = False
            if up_since is not None and _t.monotonic() - up_since >= 60:
                backoff = 2   # only a connection that held for a minute resets the back-off
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
