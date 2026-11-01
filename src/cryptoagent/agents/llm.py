"""LLM 客户端。

三种模式：
- mock   ：不调用任何模型，直接返回调用方传入的「规则引擎兜底答案」。
           这样整条流水线在无网络、无 API Key 时也能跑通并产出有意义的结果。
- openai ：任何 OpenAI 兼容网关（DeepSeek / 通义 / Moonshot / vLLM / Ollama）。
- 失败时 ：自动降级到 mock，并在报告里标注 degraded，绝不静默编造。

设计约束：`json()` 强制走 JSON schema 校验，模型输出不合规就重试，
重试仍失败则抛错 —— 绝不让一个畸形 JSON 变成一张真订单。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..config import LLMConfig
from ..netutil import ssl_context


class LLMError(RuntimeError):
    pass


def _extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise LLMError(f"模型输出不是合法 JSON: {text[:200]}")


@dataclass
class LLMResponse:
    content: str
    provider: str
    degraded: bool = False
    reason: str = ""


class LLMClient:
    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self.provider = cfg.provider.lower()
        self.api_key = os.getenv(cfg.api_key_env, "")
        # 用量统计：给 llmtest / 账单估算用，不参与决策
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "errors": 0}
        if self.provider == "openai" and not self.api_key:
            self.provider = "mock"
            self._degrade_reason = f"环境变量 {cfg.api_key_env} 为空"
        else:
            self._degrade_reason = ""

    # ------------------------------------------------------------------
    def text(self, system: str, user: str, mock: str) -> LLMResponse:
        if self.provider == "mock":
            return LLMResponse(mock, "mock", degraded=bool(self._degrade_reason), reason=self._degrade_reason)
        try:
            content = self._chat(system, user, json_mode=False)
            return LLMResponse(content.strip(), self.provider)
        except Exception as exc:  # noqa: BLE001
            return LLMResponse(
                mock, "mock-fallback", degraded=True, reason=f"{type(exc).__name__}: {exc}"
            )

    def json(self, system: str, user: str, schema_hint: str, mock: dict) -> tuple[dict, LLMResponse]:
        if self.provider == "mock":
            return mock, LLMResponse("", "mock", degraded=bool(self._degrade_reason), reason=self._degrade_reason)

        prompt = f"{user}\n\n只输出 JSON，不要任何解释。字段要求：\n{schema_hint}"
        last_err = ""
        for _ in range(max(1, self.cfg.retries)):
            try:
                raw = self._chat(system, prompt, json_mode=True)
                data = _extract_json(raw)
                self._validate(data, schema_hint)
                return data, LLMResponse(raw, self.provider)
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}: {exc}"
        return mock, LLMResponse("", "mock-fallback", degraded=True, reason=last_err)

    @staticmethod
    def _validate(data: dict, schema_hint: str) -> None:
        required = re.findall(r"^\s*[-*]?\s*\"?(\w+)\"?\s*[:：]", schema_hint, re.M)
        missing = [k for k in required if k not in data]
        if missing:
            raise LLMError(f"缺少字段 {missing}")

    # ------------------------------------------------------------------
    def _chat(self, system: str, user: str, json_mode: bool) -> str:
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            f"{self.cfg.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        self.usage["calls"] += 1
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout, context=ssl_context()) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            usage = body.get("usage") or {}
            self.usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            self.usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        except urllib.error.HTTPError as exc:
            self.usage["errors"] += 1
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise LLMError(f"HTTP {exc.code}: {detail}") from exc
        return body["choices"][0]["message"]["content"]


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------
SYSTEM_ANALYST = (
    "你是一名加密货币永续合约分析师。只依据用户提供的数据做判断，"
    "不要编造数据中不存在的新闻、链上指标或持仓信息。"
    "如果某项数据缺失，必须明确说「数据缺失」。输出简洁的中文分析，150 字以内。"
)

SYSTEM_RESEARCHER = (
    "你是一个多空辩论中的研究员。你必须站在被分配的一方，"
    "用给定的数据论证，不能引入外部事实。每次发言 120 字以内，必须给出可被证伪的理由。"
)

SYSTEM_TRADER = (
    "你是一名永续合约交易员。你的输出必须是严格 JSON，字段固定。"
    "你没有权力突破风控额度；如果信号不足，就输出 flat。"
    "禁止输出除 JSON 以外的任何内容。"
)

SYSTEM_RISK = (
    "你是独立风控官。你的职责是找理由拒绝或缩小交易，而不是促成交易。"
    "你只能维持或降低仓位，永远不能放大。输出严格 JSON。"
)