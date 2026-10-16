"""Free trade stream from the Solana RPC websocket: one logsSubscribe per watched mint, pump.fun's
TradeEvent decoded from the "Program data:" log line (mint, SOL and token amount, side, trader, curve
reserves). Used when PumpPortal refuses per-token trades without a funded API key. No keys needed."""
import asyncio
import base64
import hashlib
import json
import logging
import struct

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
    def __init__(self, url, on_msg, max_subs=150, parser=None, name="RPC-Log-Datenstrom", stale_sec=None):
        self.url, self.on_msg, self.max_subs, self.stale_sec = url, on_msg, max_subs, stale_sec
        self.parser, self.name = parser or parse_notification, name
        self.want, self.subs = set(), {}       # mint -> subscription id
        self.pending = {}                      # request id -> mint
        self.next_id = 1
        self.connected = False
        self.msgs = 0
        self.ws = None

    def subscribe(self, mints):
        self.want |= set(mints)

    def unsubscribe(self, mints):
        self.want -= set(mints)

    async def _sync(self, ws):
        for m in [m for m in self.want if m not in self.subs and m not in self.pending.values()]:
            if len(self.subs) + len(self.pending) >= self.max_subs:
                break
            rid, self.next_id = self.next_id, self.next_id + 1
            self.pending[rid] = m
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "logsSubscribe",
                                      "params": [{"mentions": [m]}, {"commitment": "processed"}]}))
        for m in [m for m in self.subs if m not in self.want]:
            sid = self.subs.pop(m)
            rid, self.next_id = self.next_id, self.next_id + 1
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "logsUnsubscribe", "params": [sid]}))

    async def run(self, stop):
        import websockets
        backoff = 2
        while not stop.is_set():
            try:
                async with websockets.connect(self.url, ping_interval=20, ping_timeout=20, max_size=2 ** 22,
                                              open_timeout=15) as ws:
                    self.connected, backoff, self.subs, self.pending = True, 2, {}, {}
                    log.info("%s verbunden (%s)", self.name, self.url.split("?")[0])

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
                                mint = self.pending.pop(m["id"])
                                if "result" in m:
                                    self.subs[mint] = m["result"]
                                else:
                                    log.warning("logsSubscribe abgelehnt: %s", str(m.get("error"))[:160])
                                    self.want.discard(mint)
                                continue
                            for msg in self.parser(m):
                                self.on_msg(msg)
                            if stop.is_set():
                                break
                    finally:
                        task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("%s getrennt (%s), neuer Versuch in %d s", self.name, e, backoff)
            self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
