"""行情 WebSocket 实时推送（零第三方依赖，标准库 RFC 6455 最小实现）。

用途：持仓品种的「最新价」不再只靠 4 秒一轮的 REST 轮询，而是由交易所行情流
主动推送（币安 fstream 的 aggTrade、OKX 的 tickers），<1s 内刷新内存价格，
页面 /api/status 每次读到的都是最新快照。

为什么手写而不是引入 websocket-client：
项目依赖刻意保持极简（numpy/pandas/certifi，HTTP 全走 urllib），服务器部署脚本
不装任何额外包。行情流只需要「握手 + 文本帧 + ping/pong」，几十行标准库代码即可，
且 TLS 统一走 netutil.ssl_context（certifi CA），与现有外呼行为一致。

安全边界：只订阅**公开行情**，不传任何 API Key，不做下单。
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
import time
import urllib.parse

from .data import okx_inst
from .netutil import ssl_context

# 币安 U 本位永续行情流（公开）。注意：官方主域名 fstream.binance.com 在某些
# 地区（含韩国机房）会「握手 101 成功但不推数据」——不是连不上，是数据层静默。
# fstream.binancefuture.com 是币安期货行情的备用入口，实测能正常推主网价。
# 因此做成候选列表，逐个尝试；配合首帧超时，静默的端点会被自动跳过。
BINANCE_FSTREAM_HOSTS = ("fstream.binancefuture.com", "fstream.binance.com")
BINANCE_FSTREAM = f"wss://{BINANCE_FSTREAM_HOSTS[0]}"
OKX_PUBLIC = "wss://ws.okx.com/ws/v5/public"          # OKX 公共行情流（8443 已退役，走 443）


def parse_binance_trade(raw: str) -> tuple[str, float] | None:
    """币安 aggTrade 消息 → (symbol, price)，解析失败返回 None。

    兼容两种形态：组合流（``{"stream":..., "data":{...}}``）与单流
    （``{"e":"aggTrade", ...}``，无 data 包裹）。
    """
    try:
        msg = json.loads(raw)
        d = msg.get("data")
        if not isinstance(d, dict):
            d = msg
        sym = str(d.get("s") or "").upper()
        px = float(d.get("p") or 0.0)
        if sym and px > 0:
            return sym, px
    except Exception:
        return None
    return None


def parse_okx_ticker(raw: str, inst_to_sym: dict[str, str]) -> tuple[str, float] | None:
    """OKX tickers 消息 → (symbol, price)，按 instId 反查品种。"""
    try:
        msg = json.loads(raw)
        inst = str((msg.get("arg") or {}).get("instId") or "")
        sym = inst_to_sym.get(inst)
        rows = msg.get("data") or []
        if sym and rows:
            px = float(rows[0].get("last") or 0.0)
            if px > 0:
                return sym, px
    except Exception:
        return None
    return None


class WsError(RuntimeError):
    pass


class WsClient:
    """最小 WebSocket 客户端（RFC 6455：客户端掩码 + 控制帧处理）。

    只用于接收文本行情帧：不做分片发送、不协商压缩。服务端未启用
    permessage-deflate 时，币安/OKX 行情都是单条完整文本帧。
    """

    def __init__(
        self,
        url: str,
        on_message,
        *,
        heartbeat_sec: float = 20.0,
        heartbeat_text: str | None = None,
        timeout: float = 15.0,
        first_data_timeout: float | None = None,
    ):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "wss":
            raise WsError(f"只支持 wss:// 行情端点，收到 {url}")
        self._url = url
        self._host = parts.hostname or ""
        self._port = parts.port or 443
        self._path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        self._on_message = on_message
        self._heartbeat_sec = max(5.0, float(heartbeat_sec))
        self._heartbeat_text = heartbeat_text
        self._timeout = timeout
        self._first_data_timeout = first_data_timeout
        self._sock: socket.socket | None = None
        self._stopped = False

    def connect(self) -> None:
        raw = socket.create_connection((self._host, self._port), timeout=self._timeout)
        try:
            self._sock = ssl_context().wrap_socket(raw, server_hostname=self._host)
        except Exception:
            raw.close()
            raise
        self._sock.settimeout(self._heartbeat_sec)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {self._path} HTTP/1.1\r\n"
            f"Host: {self._host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sock.sendall(req.encode("ascii"))
        resp = self._read_until(b"\r\n\r\n")
        head = resp.split(b"\r\n", 1)[0].decode("ascii", "replace")
        if " 101 " not in head:
            raise WsError(f"WebSocket 握手失败：{head!r}")

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def close(self) -> None:
        self._stopped = True
        if self._sock is not None:
            try:
                self._send_frame(0x8, b"")
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def run_forever(self) -> None:
        """阻塞读帧直到连接关闭。心跳随读超时一并处理，无需额外线程。

        若设置了 first_data_timeout 且期限内一条数据帧都没收到，抛 WsError：
        用于识别「握手 101 但数据层静默」的端点，让上层换下一个候选。
        """
        frag: list[bytes] = []
        got_data = False
        start = time.time()
        ft = self._first_data_timeout
        while not self._stopped and self._sock is not None:
            try:
                fin, opcode, payload = self._read_frame()
            except (socket.timeout, TimeoutError):
                if ft and not got_data and (time.time() - start) > float(ft):
                    raise WsError(f"{float(ft):.0f}s 内未收到行情数据")
                self._heartbeat()
                continue
            except (OSError, WsError):
                break
            if opcode == 0x8:  # close
                try:
                    self._send_frame(0x8, payload[:125])
                except Exception:
                    pass
                break
            if opcode == 0x9:  # ping -> pong
                self._send_frame(0xA, payload)
            elif opcode == 0xA:  # pong
                continue
            elif opcode in (0x1, 0x2):  # text / binary
                got_data = True
                try:
                    self._on_message(payload.decode("utf-8"))
                except Exception:
                    pass
            elif opcode == 0x0:  # continuation（行情不会分片，稳妥起见拼接）
                got_data = True
                frag.append(payload)
                if fin:
                    try:
                        self._on_message(b"".join(frag).decode("utf-8"))
                    except Exception:
                        pass
                    frag = []

    # ---- 内部 ----
    def _heartbeat(self) -> None:
        if self._sock is None:
            return
        try:
            if self._heartbeat_text:
                self.send_text(self._heartbeat_text)
            else:
                self._send_frame(0x9, b"")
        except Exception:
            pass

    def _read_until(self, marker: bytes) -> bytes:
        buf = b""
        while marker not in buf:
            chunk = self._sock.recv(1)
            if not chunk:
                raise WsError("连接在握手期间被关闭")
            buf += chunk
        return buf

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise WsError("连接已关闭")
            buf += chunk
        return buf

    def _read_frame(self) -> tuple[int, int, bytes]:
        hdr = self._recv_exact(2)
        fin = (hdr[0] & 0x80) != 0
        opcode = hdr[0] & 0x0F
        masked = (hdr[1] & 0x80) != 0
        n = hdr[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._recv_exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else None
        payload = self._recv_exact(n) if n else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._sock is None:
            return
        header = bytearray()
        header.append(0x80 | (opcode & 0x0F))
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)


class PriceStream:
    """把一组持仓品种的最新价从交易所行情流推送到 on_price(symbol, price)。

    品种集合与交易所通过 get_symbols / get_exchange 动态读取；断线按指数退避
    重连（1s → 2s → … → 30s 封顶）。空仓时保持静默、不建立连接。
    """

    def __init__(self, exchange: str, get_symbols, on_price, get_exchange=None):
        self._get_exchange = get_exchange or (lambda: exchange)
        self._get_symbols = get_symbols
        self._on_price = on_price
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.alive:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ws-price", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ---- 内部 ----
    def _loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            ex = (self._get_exchange() or "binance").lower()
            syms = [str(s).upper() for s in (self._get_symbols() or [])]
            if not syms:
                self._stop.wait(1.0)
                backoff = 1.0
                continue
            try:
                self._run_once(ex, syms)
                backoff = 1.0
            except Exception:
                backoff = min(backoff * 2.0, 30.0)
            if self._stop.wait(backoff):
                break

    def _run_once(self, exchange: str, syms: list[str]) -> None:
        symset = {s.upper() for s in syms}
        holder: list[WsClient | None] = [None]

        def changed() -> bool:
            now = {str(s).upper() for s in (self._get_symbols() or [])}
            return now != symset

        def on_price(sym: str, px: float) -> None:
            self._on_price(sym, px)
            # 持仓品种集合变了（开仓/平仓）→ 主动断开，下一轮按新集合重连。
            c = holder[0]
            if c is not None and changed():
                c.close()

        if exchange == "okx":
            inst_to_sym = {okx_inst(s): s.upper() for s in syms}

            def handle(raw: str) -> None:
                hit = parse_okx_ticker(raw, inst_to_sym)
                if hit:
                    on_price(*hit)

            client = WsClient(
                OKX_PUBLIC, on_message=handle, heartbeat_sec=25.0, heartbeat_text="ping"
            )
            holder[0] = client
            client.connect()
            client.send_text(json.dumps({
                "op": "subscribe",
                "args": [{"channel": "tickers", "instId": okx_inst(s)} for s in syms],
            }))
            client.run_forever()
            client.close()
            holder[0] = None
            return

        # 币安：主域名可能被地区静默（握手 101 但不推数据），逐个试候选端点，
        # 首帧超时（first_data_timeout）会让静默端点抛 WsError 从而换下一个。
        def handle(raw: str) -> None:
            hit = parse_binance_trade(raw)
            if hit:
                on_price(*hit)

        streams = "/".join(f"{s.lower()}@aggTrade" for s in syms)
        for host in BINANCE_FSTREAM_HOSTS:
            client = WsClient(
                f"wss://{host}/stream?streams={streams}",
                on_message=handle,
                heartbeat_sec=20.0,
                first_data_timeout=8.0,
            )
            holder[0] = client
            try:
                client.connect()
                client.run_forever()
                break
            except (WsError, OSError):
                client.close()
                holder[0] = None
                continue
        holder[0] = None
