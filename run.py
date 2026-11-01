"""本地启动器：python run.py demo

不安装包也能跑：把 src 加进 sys.path 即可。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from cryptoagent.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())