"""因子执行沙箱。

LLM 会写代码，代码会跑在你的机器上。这里做三层防护：

1. **AST 白名单**：只允许数学/序列运算和控制流，禁止 import、class、with、
   下划线开头的属性（挡住 `().__class__.__bases__` 这类逃逸）。
2. **受限内置函数**：子进程里 `__builtins__` 替换成白名单字典，没有 open/eval/exec。
3. **独立子进程 + 超时**：代码在单独进程里跑，30 秒不返回就杀掉，
   崩溃/死循环不会拖垮主进程。

⚠️ 诚实说明：这不是安全边界。Python 沙箱历史上被绕过过很多次，
真正跑不可信代码请放进容器（Docker/gVisor）并断网。这里的定位是
「防止模型写出危险代码」而不是「防御恶意攻击者」。
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

MAX_CODE_BYTES = 16_000
DEFAULT_TIMEOUT = 30

BANNED_NAMES = {
    "__import__", "open", "exec", "eval", "compile", "globals", "locals",
    "getattr", "setattr", "delattr", "vars", "dir", "input", "breakpoint",
    "memoryview", "exit", "quit", "help", "super", "type", "object",
}

BANNED_ATTRS = {
    "read_csv", "read_pickle", "read_parquet", "read_json", "read_sql",
    "read_html", "read_table", "to_csv", "to_pickle", "to_sql", "to_json",
    "eval", "query", "system", "popen", "load", "loads", "dump", "dumps",
    "save", "savetxt", "savez", "fromfile", "memmap", "open",
}

# 允许的语法节点：够写因子，不够搞事情
ALLOWED_NODES = {
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return,
    ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Expr, ast.If, ast.For,
    ast.While, ast.Break, ast.Continue, ast.Pass, ast.Try, ast.ExceptHandler,
    ast.Raise, ast.Lambda, ast.IfExp, ast.BoolOp, ast.BinOp, ast.UnaryOp,
    ast.Compare, ast.Call, ast.Attribute, ast.Subscript, ast.Slice,
    ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp,
    ast.Tuple, ast.List, ast.Dict, ast.Set, ast.Starred, ast.keyword,
    ast.JoinedStr, ast.FormattedValue, ast.Constant, ast.Name, ast.Load,
    ast.Store, ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd, ast.Invert,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.MatMult, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Is, ast.IsNot, ast.In, ast.NotIn, ast.BitAnd, ast.BitOr, ast.BitXor,
    ast.LShift, ast.RShift, ast.Expr, ast.alias,
}


class FactorError(RuntimeError):
    """因子代码在沙箱中执行失败。"""


def validate_factor_code(code: str) -> None:
    """静态检查。通过则返回 None，否则抛 FactorError。"""
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise FactorError(f"代码过长（>{MAX_CODE_BYTES} 字节），拒绝执行")
    try:
        tree = ast.parse(code, filename="<factor>")
    except SyntaxError as exc:
        raise FactorError(f"语法错误: {exc}") from exc

    has_factor = False
    for node in ast.walk(tree):
        if type(node) not in ALLOWED_NODES:
            raise FactorError(f"禁止的语法: {type(node).__name__} (line {getattr(node, 'lineno', '?')})")
        if isinstance(node, ast.FunctionDef) and node.name == "factor":
            has_factor = True
        if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            raise FactorError(f"禁止使用: {node.id}")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise FactorError(f"禁止访问私有属性: {node.attr}")
            if node.attr in BANNED_ATTRS:
                raise FactorError(f"禁止调用: .{node.attr}")
        if isinstance(node, ast.alias):
            raise FactorError("禁止 import")
    if not has_factor:
        raise FactorError("必须定义函数 `def factor(df) -> pd.Series`")


_RUNNER = r'''
import json, sys, traceback
import numpy as np
import pandas as pd

SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "divmod": divmod, "enumerate": enumerate, "filter": filter, "float": float,
    "int": int, "isinstance": isinstance, "len": len, "list": list, "map": map,
    "max": max, "min": min, "pow": pow, "range": range, "reversed": reversed,
    "round": round, "set": set, "slice": slice, "sorted": sorted, "str": str,
    "sum": sum, "tuple": tuple, "zip": zip, "True": True, "False": False, "None": None,
}

def main():
    code_path, csv_path, out_path, meta_path, checks = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])
    df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
    src = open(code_path, encoding="utf-8").read()
    ns = {"__builtins__": SAFE_BUILTINS, "np": np, "pd": pd}
    exec(compile(src, "<factor>", "exec"), ns)
    fn = ns.get("factor")
    if not callable(fn):
        raise RuntimeError("没有找到 factor 函数")

    def compute(frame):
        res = fn(frame)
        if isinstance(res, pd.DataFrame) and res.shape[1] == 1:
            res = res.iloc[:, 0]
        res = pd.Series(np.asarray(res, dtype=float), index=frame.index) if not isinstance(res, pd.Series) else res.astype(float)
        if len(res) != len(frame):
            raise RuntimeError("返回值长度与输入不一致")
        return res

    full = compute(df)
    full.to_csv(out_path)

    lookahead = []
    if checks > 0:
        n = len(df)
        for frac in [0.45, 0.6, 0.75, 0.9][:checks]:
            k = max(60, int(n * frac))
            part = compute(df.iloc[:k])
            a, b = float(full.iloc[k - 1]), float(part.iloc[-1])
            same = (np.isnan(a) and np.isnan(b)) or abs(a - b) <= 1e-6 * max(1.0, abs(a))
            lookahead.append({"k": k, "full": a, "truncated": b, "consistent": bool(same)})
    json.dump({"ok": True, "lookahead": lookahead}, open(meta_path, "w", encoding="utf-8"))

if __name__ == "__main__":
    try:
        main()
    except Exception:
        err = traceback.format_exc()
        try:
            json.dump({"ok": False, "error": err}, open(sys.argv[4], "w", encoding="utf-8"))
        except Exception:
            pass
        sys.stderr.write(err)
        sys.exit(1)
'''


def _write_frame(df: pd.DataFrame, path: Path) -> None:
    cols = [c for c in ["open", "high", "low", "close", "volume", "funding_rate"] if c in df.columns]
    df[cols].to_csv(path, index_label="ts")


def run_factor(
    code: str,
    df: pd.DataFrame,
    timeout: int = DEFAULT_TIMEOUT,
    lookahead_checks: int = 4,
) -> tuple[pd.Series, dict]:
    """在子进程里执行因子代码，返回 (因子序列, 元信息)。

    元信息包含 lookahead 检查结果：用前 k 根 K 线重算因子，
    若最后一个值与全量数据算出的值不同，说明代码偷看了未来。
    """
    validate_factor_code(code)
    with tempfile.TemporaryDirectory(prefix="factor_") as tmp:
        tmpdir = Path(tmp)
        code_path = tmpdir / "factor.py"
        csv_path = tmpdir / "data.csv"
        out_path = tmpdir / "out.csv"
        meta_path = tmpdir / "meta.json"
        code_path.write_text(code, encoding="utf-8")
        _write_frame(df, csv_path)

        cmd = [
            sys.executable, "-c", _RUNNER,
            str(code_path), str(csv_path), str(out_path), str(meta_path),
            str(lookahead_checks),
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise FactorError(f"因子执行超时（>{timeout}s），可能存在死循环或 O(n²) 写法") from exc

        if not meta_path.exists():
            raise FactorError(f"子进程异常退出: {proc.stderr[-800:]}")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not meta.get("ok"):
            raise FactorError(_short_traceback(meta.get("error", "")))

        values = pd.read_csv(out_path, index_col=0, parse_dates=True).iloc[:, 0]
        values.index = df.index[: len(values)]
        return values.astype(float), meta


def _short_traceback(tb: str) -> str:
    lines = [ln for ln in tb.strip().splitlines() if ln.strip()]
    return "\n".join(lines[-6:])[:1200]


def check_lookahead(meta: dict) -> list[str]:
    """把子进程返回的 lookahead 结果转成人话。"""
    problems = []
    for item in meta.get("lookahead", []):
        if not item["consistent"]:
            problems.append(
                f"截断到 {item['k']} 根时结果从 {item['full']:.6g} 变成 {item['truncated']:.6g}"
            )
    return problems