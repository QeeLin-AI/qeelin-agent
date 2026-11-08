"""行情 WebSocket 模块离线单测：帧编解码、掩码、消息解析（不连外网）。"""
from __future__ import annotations

import socket

from cryptoagent.wsfeed import (
    WsClient,
    parse_binance_trade,
    parse_okx_trade,
)


def _paired_client() -> tuple[WsClient, socket.socket]:
    a, b = socket.socketpair()
    c = WsClient("wss://fstream.binance.com/stream?streams=btcusdt@aggTrade", on_message=lambda *_: None)
    c._sock = a  # 绕过真实 TLS 握手，只测帧层
    return c, b


def test_send_frame_client_masked():
    c, b = _paired_client()
    c._send_frame(0x1, b"hello")
    hdr = b.recv(2)
    assert hdr[0] == 0x81            # FIN + opcode text
    assert hdr[1] & 0x80             # 客户端必须掩码
    n = hdr[1] & 0x7F
    assert n == 5
    mask = b.recv(4)
    payload = b.recv(n)
    assert bytes(x ^ mask[i % 4] for i, x in enumerate(payload)) == b"hello"
    c.close()
    b.close()


def test_send_frame_16bit_length():
    c, b = _paired_client()
    payload = b"x" * 200
    c._send_frame(0x1, payload)
    hdr = b.recv(2)
    assert hdr[1] & 0x7F == 126       # 126 表示随后 2 字节为真实长度
    ext = b.recv(2)
    n = int.from_bytes(ext, "big")
    assert n == 200
    mask = b.recv(4)
    got = b.recv(n)
    assert bytes(x ^ mask[i % 4] for i, x in enumerate(got)) == payload
    c.close()
    b.close()


def test_read_frame_server_unmasked_text():
    c, b = _paired_client()
    b.sendall(b"\x81\x02hi")
    fin, op, payload = c._read_frame()
    assert fin and op == 0x1 and payload == b"hi"
    c.close()
    b.close()


def test_read_frame_server_ping():
    c, b = _paired_client()
    b.sendall(b"\x89\x00")
    fin, op, payload = c._read_frame()
    assert op == 0x9 and payload == b""
    c.close()
    b.close()


def test_read_frame_server_16bit_length():
    c, b = _paired_client()
    payload = b"y" * 300
    frame = b"\x81\x7e" + len(payload).to_bytes(2, "big") + payload
    b.sendall(frame)
    _, op, got = c._read_frame()
    assert op == 0x1 and got == payload
    c.close()
    b.close()


def test_parse_binance_trade_ok():
    raw = '{"stream":"btcusdt@aggTrade","data":{"e":"aggTrade","s":"BTCUSDT","p":"75000.10","q":"0.001","T":1700000000000,"m":false}}'
    assert parse_binance_trade(raw) == ("BTCUSDT", 75000.10)


def test_parse_binance_trade_single_stream():
    # 单流 /ws/<stream> 的消息没有 stream/data 包裹，直接是 aggTrade 内容
    raw = '{"e":"aggTrade","s":"BTCUSDT","p":"75887.0","q":"0.002","T":1700000000000,"m":false}'
    assert parse_binance_trade(raw) == ("BTCUSDT", 75887.0)


def test_parse_binance_trade_bad():
    assert parse_binance_trade("not json") is None
    assert parse_binance_trade('{"data":{"s":"BTCUSDT","p":"0"}}') is None  # 非正价
    assert parse_binance_trade('{"data":{}}') is None


def test_parse_okx_trade_ok():
    # trades 频道（逐笔成交）：价格在 data[].px，取最近一笔
    raw = ('{"arg":{"channel":"trades","instId":"BTC-USDT-SWAP"},'
           '"data":[{"instId":"BTC-USDT-SWAP","tradeId":"1","px":"75000.1",'
           '"sz":"1","side":"buy","ts":"1700000000000"}]}')
    mapping = {"BTC-USDT-SWAP": "BTCUSDT"}
    assert parse_okx_trade(raw, mapping) == ("BTCUSDT", 75000.1)


def test_parse_okx_trade_takes_latest():
    raw = ('{"arg":{"channel":"trades","instId":"BTC-USDT-SWAP"},'
           '"data":[{"px":"75000.1"},{"px":"75001.5"}]}')
    mapping = {"BTC-USDT-SWAP": "BTCUSDT"}
    assert parse_okx_trade(raw, mapping) == ("BTCUSDT", 75001.5)


def test_parse_okx_trade_unknown_inst():
    raw = '{"arg":{"channel":"trades","instId":"ETH-USDT-SWAP"},"data":[{"px":"3000.0"}]}'
    assert parse_okx_trade(raw, {"BTC-USDT-SWAP": "BTCUSDT"}) is None


def test_parse_okx_subscribe_ack_ignored():
    raw = '{"event":"subscribe","arg":{"channel":"trades","instId":"BTC-USDT-SWAP"}}'
    assert parse_okx_trade(raw, {"BTC-USDT-SWAP": "BTCUSDT"}) is None
