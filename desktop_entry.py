"""QeeLin-Agent 桌面版后端入口（供 PyInstaller 打包为 sidecar exe）。

启动本地 web 服务，Electron 桌面壳加载它。用户数据（.env、config/）放在
可执行文件同目录，方便在「设置」页修改并持久化。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# 开发环境：把 src 加进 sys.path；打包后 PyInstaller 已处理 import
_ROOT = Path(__file__).resolve().parent
if (_ROOT / "src").is_dir():
    sys.path.insert(0, str(_ROOT / "src"))


def _app_dir() -> Path:
    """可执行文件所在目录（用户可写，放 .env 与 config）。"""
    if getattr(sys, "frozen", False):  # PyInstaller 打包后
        return Path(sys.executable).resolve().parent
    return _ROOT


def _ensure_user_files(app_dir: Path) -> None:
    """把默认 config / .env 从打包资源复制到 exe 同目录（仅当目标不存在）。"""
    meipass = Path(getattr(sys, "_MEIPASS", _ROOT))
    # config/default.json
    default_cfg = meipass / "config" / "default.json"
    target_cfg = app_dir / "config" / "default.json"
    if default_cfg.exists() and not target_cfg.exists():
        target_cfg.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(default_cfg, target_cfg)
    # .env（从 .env.example 生成）
    env_src = meipass / ".env.example"
    env_target = app_dir / ".env"
    if env_src.exists() and not env_target.exists():
        shutil.copy(env_src, env_target)


def main() -> None:
    # 沙箱因子执行模式：run_factor 用 sys.executable -c _RUNNER 跑因子沙箱。
    # 打包后 sys.executable 是本 exe，若不在这里拦截，-c 会被忽略而重新进入 serve，
    # 导致无限递归 spawn（进程爆炸）。这里手动模拟 python -c 的行为。
    if len(sys.argv) >= 3 and sys.argv[1] == "-c":
        code = sys.argv[2]
        sys.argv = ["-c"] + sys.argv[3:]
        exec(compile(code, "<sandbox>", "exec"), {"__name__": "__main__"})
        return

    app_dir = _app_dir()
    _ensure_user_files(app_dir)
    # 让 .env 和 config 从 exe 同目录读（否则打包后会定位到临时目录）
    os.environ.setdefault("CRYPTOAGENT_ENV_FILE", str(app_dir / ".env"))
    os.environ.setdefault("CRYPTOAGENT_CONFIG", str(app_dir / "config" / "default.json"))

    from cryptoagent.config import load_config, load_dotenv
    from cryptoagent.web.server import serve

    # 显式从 exe 同目录加载 .env 和 config（打包后 __file__ 位于临时解压目录，默认定位会失效）
    load_dotenv(app_dir / ".env")
    cfg = load_config(app_dir / "config" / "default.json")
    port = int(os.getenv("QEELIN_PORT", "8765"))
    print(f"[QeeLin-Agent] 后端服务已启动 http://127.0.0.1:{port}")
    serve(cfg, host="127.0.0.1", port=port, open_browser=False)


if __name__ == "__main__":
    main()
