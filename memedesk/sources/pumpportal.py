"""PumpPortal live feed (free WebSocket): every new pump.fun token and every graduation, pushed in ~1 s.

Stdlib-only WebSocket client (RFC 6455: handshake, masked client frames, ping/pong,
fragmentation), with automatic reconnect. It only *discovers* mints; prices, safety
and momentum still come from DexScreener / RPC / RugCheck through the normal pipeline.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import socket
import ssl
import struct
import threading
import time
from collections import deque
from urllib.parse import urlparse

log = logging.getLogger("memedesk.pumpportal")

URL = "wss://pumpportal.fun/api/data"


# ---- RFC 6455 framing ------------------------------------------------------
def encode_frame(opcode: int, payload: bytes, mask: bool = True) -> bytes:
    head = bytes([0x80 | opcode])
    n = len(payload)
    mbit = 0x80 if mask else 0
    if n < 126:
        head += bytes([mbit | n])
    elif n < 65536:
        head += bytes([mbit | 126]) + struct.pack(">H", n)
    else:
        head += bytes([mbit | 127]) + struct.pack(">Q", n)
    if not mask:
        return head + payload
    key = os.urandom(4)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


class FrameReader:
    """Reads whole messages from a byte source (recv(n) -> bytes)."""

    def __init__(self, recv):
        self.recv = recv

    def _exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("websocket closed")
            buf += chunk
        return buf

    def frame(self) -> tuple[int, bool, bytes]:
        b1, b2 = self._exact(2)
        fin, opcode = bool(b1 & 0x80), b1 & 0x0F
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._exact(8))[0]
        key = self._exact(4) if b2 & 0x80 else None
        data = self._exact(n)
        if key:
            data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
        return opcode, fin, data

    def message(self, on_control) -> tuple[int, bytes]:
        """Next text/binary message; control frames go to on_control(opcode, payload)."""
        parts, first_op = [], None
        while True:
            op, fin, data = self.frame()
            if op >= 0x8:                      # close / ping / pong
                on_control(op, data)
                continue
            if op != 0:
                first_op = op
            parts.append(data)
            if fin:
                return first_op or 1, b"".join(parts)


# ---- connection --------------------------------------------------------------
def _open_socket(host: str, port: int, timeout: float, tls: bool = True) -> socket.socket:
    proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")) if tls else None
    if proxy:
        p = urlparse(proxy)
        sock = socket.create_connection((p.hostname, p.port or 80), timeout=timeout)
        sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("proxy closed")
            resp += chunk
        status = resp.split(b"\r\n", 1)[0]
        if b" 200" not in status:
            raise ConnectionError(f"proxy refused: {status!r}")
    else:
        sock = socket.create_connection((host, port), timeout=timeout)
    return ssl.create_default_context().wrap_socket(sock, server_hostname=host) if tls else sock


class WebSocket:
    def __init__(self, url: str, timeout: float = 30.0):
        u = urlparse(url)
        self.host, self.path = u.hostname, (u.path or "/") + (f"?{u.query}" if u.query else "")
        tls = u.scheme != "ws"
        self.sock = _open_socket(self.host, u.port or (443 if tls else 80), timeout, tls)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {self.path} HTTP/1.1\r\nHost: {self.host}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                           "User-Agent: memedesk/2.0\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake: connection closed")
            resp += chunk
        head, _, rest = resp.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b" 101" not in status:
            raise ConnectionError(f"handshake refused: {status!r}")
        self._pending = rest
        self.reader = FrameReader(self._recv)
        self.wlock = threading.Lock()            # frames from two threads must never interleave

    def _recv(self, n: int) -> bytes:
        if self._pending:
            out, self._pending = self._pending[:n], self._pending[n:]
            return out
        return self.sock.recv(n)

    def send_text(self, text: str) -> None:
        with self.wlock:
            self.sock.sendall(encode_frame(0x1, text.encode()))

    def ping(self) -> None:
        with self.wlock:
            self.sock.sendall(encode_frame(0x9, b"hb"))

    def _control(self, op: int, data: bytes) -> None:
        if op == 0x9:                              # ping -> pong
            with self.wlock:
                self.sock.sendall(encode_frame(0xA, data))
        elif op == 0x8:
            raise ConnectionError("server closed the websocket")

    def recv_text(self) -> str:
        _op, data = self.reader.message(self._control)
        return data.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.sendall(encode_frame(0x8, b""))
            self.sock.close()
        except OSError:
            pass


# ---- feed --------------------------------------------------------------------
class PumpPortalFeed:
    """Background thread collecting new-token and migration mints."""

    def __init__(self, url: str = URL, maxlen: int = 2000):
        self.url = url
        self.queue: deque[tuple[str, str]] = deque(maxlen=maxlen)
        self.lock = threading.Lock()
        self.connected = False
        self.stats = {"new": 0, "migrated": 0, "reconnects": 0, "last_msg": 0.0, "trades": 0, "insider_alerts": 0}
        self.ws = None
        self.watched: dict[str, str] = {}             # held mint -> creator wallet ("" if unknown)
        self.subscribed: set[str] = set()
        self.alerts: dict[str, tuple[float, str]] = {}  # mint -> (ts, what happened)
        self.big_sell_pct = 1.0

    # ---- held-coin trade stream (insider exits) -----------------------------------------
    def watch(self, mints: dict[str, str]) -> None:
        """Keep a trade subscription on exactly these mints (the coins we hold)."""
        with self.lock:
            self.watched = {m: (c or "") for m, c in mints.items()}
        self._sync()

    def _sync(self) -> None:
        ws = self.ws
        if ws is None:
            return
        with self.lock:
            add = sorted(set(self.watched) - self.subscribed)
            drop = sorted(self.subscribed - set(self.watched))
        try:
            if add or drop:
                if add:
                    ws.send_text(json.dumps({"method": "subscribeTokenTrade", "keys": add}))
                if drop:
                    ws.send_text(json.dumps({"method": "unsubscribeTokenTrade", "keys": drop}))
        except OSError as e:
            log.info("pumpportal subscribe failed: %s", e)
            return
        with self.lock:
            self.subscribed |= set(add)
            self.subscribed -= set(drop)

    def alert_for(self, mint: str, since: float = 0.0) -> str | None:
        with self.lock:
            hit = self.alerts.get(mint)
            if hit and hit[0] >= since:
                del self.alerts[mint]
                return hit[1]
        return None

    def _trade(self, mint: str, tx: str, msg: dict) -> None:
        with self.lock:
            self.stats["trades"] += 1
            self.stats["last_msg"] = time.time()
            if tx != "sell" or mint not in self.watched:
                return
            creator = self.watched[mint]
        trader = msg.get("traderPublicKey") or ""
        try:
            amount = float(msg.get("tokenAmount") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        what = None
        if creator and trader == creator:
            what = f"creator {trader[:6]}… sold {amount:,.0f} tokens"
        elif mint.endswith("pump") and amount >= 1e9 * self.big_sell_pct / 100:   # pump.fun: 1B supply
            what = f"wallet {trader[:6]}… sold {amount / 1e7:.1f}% of supply"
        if what:
            with self.lock:
                self.alerts[mint] = (time.time(), what)
                self.stats["insider_alerts"] += 1

    def handle(self, msg: dict) -> None:
        mint = msg.get("mint")
        if not isinstance(mint, str) or not 32 <= len(mint) <= 44:
            return
        tx = (msg.get("txType") or "").lower()
        if tx in ("buy", "sell"):
            self._trade(mint, tx, msg)
            return
        tag = "pp_migrated" if tx == "migrate" or msg.get("pool") in ("pump-amm", "raydium") and tx != "create" \
            else "pp_new"
        with self.lock:
            self.queue.append((mint, tag))
            self.stats["migrated" if tag == "pp_migrated" else "new"] += 1
            self.stats["last_msg"] = time.time()

    def drain(self) -> list[tuple[str, str]]:
        with self.lock:
            out = list(self.queue)
            self.queue.clear()
        return out

    def run(self, stop: threading.Event) -> None:
        delay = 5.0
        while not stop.is_set():
            ws = None
            try:
                ws = WebSocket(self.url, timeout=90)
                ws.send_text(json.dumps({"method": "subscribeNewToken"}))
                ws.send_text(json.dumps({"method": "subscribeMigration"}))
                with self.lock:
                    self.subscribed = set()               # fresh connection: resubscribe held coins
                self.ws = ws
                self._sync()
                self.connected, delay = True, 5.0
                log.warning("pumpportal live feed connected")
                while not stop.is_set():
                    try:
                        text = ws.recv_text()
                    except socket.timeout:
                        ws.ping()                                     # keep the connection alive
                        continue
                    try:
                        msg = json.loads(text)
                    except ValueError:
                        continue
                    if isinstance(msg, dict):
                        self.handle(msg)
            except (OSError, ConnectionError, ssl.SSLError) as e:
                log.info("pumpportal feed down (%s); reconnecting in %.0fs", e, delay)
            finally:
                self.connected = False
                self.ws = None
                if ws:
                    ws.close()
            self.stats["reconnects"] += 1
            stop.wait(delay)
            delay = min(120.0, delay * 2)

    def start(self, stop: threading.Event) -> threading.Thread:
        t = threading.Thread(target=self.run, args=(stop,), name="pumpportal", daemon=True)
        t.start()
        return t
