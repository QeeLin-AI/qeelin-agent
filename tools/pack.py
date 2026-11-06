"""把 QeeLin-Agent 打成一个可拷走的 zip（只用标准库，不需要装任何东西）。

用法：
    python tools/pack.py                # 默认：带行情缓存、不带账本、不带 .env
    python tools/pack.py --with-runs    # 连账本（runs/）一起带走
    python tools/pack.py --no-data      # 不带行情缓存（到新机器自己下）

排除：__pycache__ / *.pyc / .env / docs/.edge* / dist* / *.zip
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 必带：代码、配置、启动脚本、文档
INCLUDE = [
    "src",
    "config",
    "deploy",
    "tests",
    "docs",
    "requirements.txt",
    "pyproject.toml",
    "run.py",
    "run.cmd",
    "run.sh",
    "start.cmd",
    "start-web.cmd",
    "tools",
    ".env.example",
    "README.md",
    "LICENSE",
    ".gitignore",
]

SKIP_DIRS = {"__pycache__", ".git", ".venv", "dist", "runs.synthetic_backup"}
SKIP_SUFFIX = {".pyc", ".pyo", ".zip", ".log"}
SKIP_NAMES = {
    ".env", "engine_state.json",
    # 运行时配置（含本机线上参数）：发布包只带 default.example.json
    "default.json",
    # 内部交接/运维脚本，不进发布包
    "handoff.py", "enable-agent-ssh.ps1", "enable-ssh-oneliner.txt",
    "setup-ssh-windows.ps1", "ssh-config.example", "optimized.rdp",
}


def _skip(path: Path) -> bool:
    if path.name in SKIP_NAMES:
        return True
    if path.suffix in SKIP_SUFFIX:
        return True
    if path.name.startswith(".edge"):
        return True
    if any(part.startswith(".edge") for part in path.parts):
        return True
    return any(part in SKIP_DIRS for part in path.parts)


def collect(include_data: bool, include_runs: bool) -> list[Path]:
    items = list(INCLUDE)
    if include_data:
        items.append("data")
    if include_runs:
        items.append("runs")
    files: list[Path] = []
    for item in items:
        p = ROOT / item
        if not p.exists():
            print(f"  [跳过] {item} 不存在")
            continue
        if p.is_file():
            files.append(p)
            continue
        for f in p.rglob("*"):
            if f.is_file() and not _skip(f.relative_to(ROOT)):
                files.append(f)
    return sorted(set(files))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打包 QeeLin-Agent 以便拷到别的电脑")
    ap.add_argument("--no-data", action="store_true", help="不带 data/ 行情缓存")
    ap.add_argument("--with-runs", action="store_true", help="带上 runs/ 账本与决策记录")
    ap.add_argument("--out", help="输出 zip 路径")
    args = ap.parse_args(argv)

    files = collect(include_data=not args.no_data, include_runs=args.with_runs)
    if not files:
        print("没有可打包的文件")
        return 1

    stamp = time.strftime("%Y%m%d_%H%M")
    out = Path(args.out) if args.out else ROOT / "dist" / f"qeelin-agent-{stamp}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in files:
            rel = f.relative_to(ROOT).as_posix()
            zf.write(f, rel)
            total += f.stat().st_size

    size_mb = out.stat().st_size / 1024 / 1024
    print("=" * 58)
    print(f"  打包完成：{out}")
    print(f"  文件 {len(files)} 个 ｜ 原始 {total / 1024 / 1024:.1f} MB ｜ 压缩后 {size_mb:.1f} MB")
    print("=" * 58)

    by_top: dict[str, int] = {}
    for f in files:
        top = f.relative_to(ROOT).parts[0]
        by_top[top] = by_top.get(top, 0) + 1
    for k in sorted(by_top, key=lambda x: -by_top[x])[:8]:
        print(f"  {k:<24} {by_top[k]:>4} 个文件")

    print("\n下一步：把 zip 拷到目标电脑 → 解压 → 双击 start-web.cmd（Windows）或 ./run.sh（Linux/macOS）")
    print("详细说明见 README.md")
    Path(ROOT / "dist" / "last-pack.json").write_text(
        json.dumps({"zip": str(out), "files": len(files), "at": stamp}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
