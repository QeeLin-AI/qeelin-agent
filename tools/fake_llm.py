"""假的 OpenAI 兼容网关，只为验证客户端链路（文本/JSON/用量统计）。

    python tools/fake_llm.py 8123
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TEXT = "价格在均线上方，动量偏中性，建议观望；数据缺失项：资金费率。"
JSON_ANALYST = {"stance": 0.1, "summary": "假网关：技术面中性", "data_flags": []}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        user = body.get("messages", [{}])[-1].get("content", "")
        wants_json = body.get("response_format", {}).get("type") == "json_object"

        if wants_json:
            if "stance" in user:
                content = json.dumps(JSON_ANALYST, ensure_ascii=False)
            elif "size_multiplier" in user or "verdict" in user:
                content = json.dumps(
                    {"verdict": "approve", "size_multiplier": 0.8, "reasons": ["假网关：无额外风险"]},
                    ensure_ascii=False,
                )
            else:
                content = json.dumps(
                    {
                        "action": "flat",
                        "size_pct": 0.0,
                        "leverage": 1.0,
                        "stop_loss_pct": 0.02,
                        "take_profit_pct": 0.04,
                        "confidence": 0.3,
                        "rationale": "假网关：信号不足，观望",
                    },
                    ensure_ascii=False,
                )
        else:
            content = TEXT

        payload = {
            "id": "fake",
            "object": "chat.completion",
            "model": body.get("model", "fake"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": len(user) // 2,
                "completion_tokens": len(content) // 2,
                "total_tokens": (len(user) + len(content)) // 2,
            },
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_a) -> None:  # 安静点
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8123
    print(f"fake llm on http://127.0.0.1:{port}/v1")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
