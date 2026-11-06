"""L3：让 LLM 对持仓管理提出「参数调整 / 动作覆盖」提案。

边界（由代码强制，不靠提示词）：

- LLM 只能**提案**，不能直接改动作或参数；
- `action` 只能是 hold / tighten / close；
- `params` 只能落在调用方给的 `allowed_params` 里（按命中规则限定），
  且必须通过 `BOUNDS`、单轮步长与 L2 回放；
- 置信度低于阈值、JSON 不合规、降级输出，一律忽略；
- 解释与影子复核仍然走原来的 `review_management()`，本模块只负责「进化提案」。
"""
from __future__ import annotations

from ..config import PositionConfig
from ._util import num_or_none
from .llm import LLMClient
from .position_calibration import BOUNDS, DEFAULTS, RULE_LABELS
from .position_manager import PositionDecision

SYSTEM_EVOLVE = (
    "你是持仓管理员的进化提案器。你只依据给定数字判断，不要发明数据、不要建议开新仓。"
    "你的提案会被代码裁决：动作只能更保守（hold→tighten/close、tighten→close），"
    "参数只能在给定范围内小幅调整。输出严格 JSON，中文，理由不超过 60 字。"
)

EVOLVE_SCHEMA = (
    "- action: hold 或 tighten 或 close（你建议的退出动作）\n"
    "- params: 对象，键只能是允许列表里的参数名，值为目标数字（不调就留空对象）\n"
    "- confidence: 0 到 1 之间的数字\n"
    "- reason: 一句话理由"
)


def propose(
    llm: LLMClient,
    cfg: PositionConfig,
    pos: dict,
    decision: PositionDecision,
    summary: dict | None,
    allowed_params: tuple[str, ...] | list[str] | None = None,
) -> dict | None:
    """让 LLM 提一次进化提案；不可用时返回 None。"""
    allowed = tuple(allowed_params or ())
    mock = {
        "action": decision.action,
        "params": {},
        "confidence": 0.0,
        "reason": "规则兜底（模型不可用）",
    }
    label = RULE_LABELS.get(str(decision.rule or ""), str(decision.rule or "未命中"))
    current = {k: float(getattr(cfg, k, DEFAULTS.get(k, 0.0))) for k in allowed}
    perf = summary or {}
    user = (
        f"当前仓位：{pos.get('symbol', '')} {pos.get('side', '')} "
        f"entry={float(pos.get('entry') or 0):.6g} stop={float(pos.get('stop') or 0):.6g} "
        f"qty={float(pos.get('qty') or 0):.6g} 持有 {int(pos.get('bars') or 0)} 根\n"
        f"规则动作：{decision.action}（命中规则：{label}，理由：{decision.reason}）\n"
        f"浮动 R：{float((decision.details or {}).get('floating_r') or 0):+.2f}\n"
        f"允许调整的参数与当前值：{current}\n"
        f"参数硬边界：{ {k: BOUNDS[k] for k in allowed if k in BOUNDS} }\n"
        f"最近规则绩效：{ {k: v for k, v in (perf.get('by_rule') or {}).items() if k in RULE_LABELS} }\n"
        f"样本数：{perf.get('samples')}（少于门槛时不要调参）\n\n"
        "如果你认为规则动作过于保守，可以建议更保守的方向（tighten/close）；"
        "只有在有明确数字依据时才调参数，单轮幅度不要超过 15%。"
    )
    data, resp = llm.json(SYSTEM_EVOLVE, user, EVOLVE_SCHEMA, mock)
    if not isinstance(data, dict):
        return None
    action = str(data.get("action") or decision.action).lower()
    if action not in ("hold", "tighten", "close"):
        action = decision.action
    params_raw = data.get("params")
    params: dict[str, float] = {}
    if isinstance(params_raw, dict):
        for key, value in params_raw.items():
            if key not in allowed or key not in BOUNDS:
                continue
            v = num_or_none(value)
            if v is None:
                continue
            lo, hi = BOUNDS[key]
            params[key] = round(min(hi, max(lo, v)), 6)
    conf = num_or_none(data.get("confidence"))
    conf = 0.0 if conf is None else max(0.0, min(1.0, conf))
    return {
        "action": action,
        "params": params,
        "confidence": round(conf, 3),
        "reason": str(data.get("reason") or "")[:160],
        "degraded": bool(resp.degraded),
        "source": resp.provider,
    }
