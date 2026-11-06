"""智能体内部的无状态小工具。

只放「多个模块都要用、且没有业务语义」的纯函数，避免每个文件各抄一份。
"""
from __future__ import annotations

from typing import Any


def num_or_none(value: Any) -> float | None:
    """转成 float；失败或 NaN 返回 None。

    NaN 必须显式排除：`nan` 参与比较时恒为 False，会让「是否越界 / 是否改善」
    这类判断静默失效，比抛错更难查。
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None
