"""多通道通知分发器（fail-safe 设计）。

本模块负责在交易事件中向用户发出告警，例如：
- 止盈/止损触发
- 风控拒绝开仓
- 连续亏损触发熔断（kill-switch）
- 其他需要人工关注的异常

设计哲学（与整个 crypto-agent 一致：确定性、绝不崩溃）：
通知投递 MUST NEVER 让交易主循环崩溃。任何通道的失败都被捕获、记录、吞掉，
绝不会向上抛出。即使 webhook / telegram / email 全部配置错误，
console / inapp 仍要保证本地可读。

支持的通道（通过 NotifyConfig.channels 启用子集）：
- console ：直接打到日志（logging.getLogger("notify")），无需任何配置。
- inapp   ：把通知以 JSONL 行追加到 cfg.inapp_path，供 Web UI 读取（默认 runs/notifications.jsonl）。
- webhook ：向 os.getenv(cfg.webhook_url_env) 指向的 URL POST JSON。
            启用方式：设置环境变量 CRYPTOAGENT_WEBHOOK_URL。
- telegram：通过 Telegram Bot API 发消息。
            启用方式：同时设置 CRYPTOAGENT_TG_TOKEN 与 CRYPTOAGENT_TG_CHAT。
- email   ：通过 SMTP 发送纯文本邮件。
            启用方式：设置 CRYPTOAGENT_SMTP_HOST / PORT / USER / PASS / TO。

所有外部通道在无对应环境变量时静默跳过（skip silently）， env 为空不会报错。
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

_log = logging.getLogger("notify")


@dataclass
class NotifyConfig:
    """通知配置。

    各 env 字段仅声明「从哪个环境变量读取」，实际读取在对应适配器内进行，
    这样未启用该通道时完全不会触碰相关环境变量。
    """

    channels: list[str] = field(default_factory=lambda: ["console", "inapp"])
    enabled: bool = True
    webhook_url_env: str = "CRYPTOAGENT_WEBHOOK_URL"
    telegram_token_env: str = "CRYPTOAGENT_TG_TOKEN"
    telegram_chat_env: str = "CRYPTOAGENT_TG_CHAT"
    email_smtp_host_env: str = "CRYPTOAGENT_SMTP_HOST"
    email_smtp_port_env: str = "CRYPTOAGENT_SMTP_PORT"
    email_user_env: str = "CRYPTOAGENT_SMTP_USER"
    email_pass_env: str = "CRYPTOAGENT_SMTP_PASS"
    email_to_env: str = "CRYPTOAGENT_SMTP_TO"
    inapp_path: str = "runs/notifications.jsonl"


@dataclass
class Notification:
    """单条通知。

    level 取值：info | warn | critical。
    ts 为 ISO8601 字符串（含时区）。
    """

    level: str
    title: str
    message: str
    ts: str
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """序列化为普通 dict，供 JSON 存储 / 网络传输。"""
        return {
            "level": self.level,
            "title": self.title,
            "message": self.message,
            "ts": self.ts,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Notification":
        """从 dict 还原；容忍缺失字段，绝不在读取历史记录时崩溃。"""
        return cls(
            level=str(data.get("level", "info")),
            title=str(data.get("title", "")),
            message=str(data.get("message", "")),
            ts=str(data.get("ts", "")),
            meta=dict(data.get("meta", {}) or {}),
        )


class Notifier:
    """多通道通知分发器。

    通过 send() 构建一条 Notification，再依次投递到所有启用的通道适配器。
    每个适配器都在 try/except 中执行，单个通道失败不会影响其它通道。
    """

    def __init__(self, cfg: NotifyConfig) -> None:
        self.cfg = cfg

    def send(
        self,
        level: str,
        title: str,
        message: str,
        meta: dict | None = None,
    ) -> Notification:
        """构建通知并分发到所有启用的通道，返回该通知。"""
        # 主开关关闭：整体静默（getattr 兜底，容忍旧版 NotifyConfig 无 enabled 字段）
        if not getattr(self.cfg, "enabled", True):
            return None
        n = Notification(
            level=level,
            title=title,
            message=message,
            ts=datetime.now(timezone.utc).isoformat(),
            meta=meta or {},
        )
        for channel in self.cfg.channels:
            method = {
                "console": self._console,
                "inapp": self._inapp,
                "webhook": self._webhook,
                "telegram": self._telegram,
                "email": self._email,
            }.get(channel)
            if method is None:
                _log.warning("未知通知通道，已跳过: %s", channel)
                continue
            try:
                method(n)
            except Exception as exc:  # noqa: BLE001 - 单通道失败绝不向上传播
                _log.error("通知通道 %s 投递失败: %s: %s", channel, type(exc).__name__, exc)
        return n

    # ------------------------------------------------------------------
    def _console(self, n: Notification) -> None:
        """打到日志（按 level 选择 INFO/WARNING/ERROR）。"""
        line = f"[{n.level}] {n.title}: {n.message}"
        if n.meta:
            line += f" | meta={n.meta}"
        if n.level == "critical":
            _log.error(line)
        elif n.level == "warn":
            _log.warning(line)
        else:
            _log.info(line)

    def _inapp(self, n: Notification) -> None:
        """追加为 JSONL 行到文件，供 Web UI 读取；创建父目录。"""
        path = self.cfg.inapp_path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(n.to_dict(), ensure_ascii=False) + "\n")

    def _webhook(self, n: Notification) -> None:
        """向 webhook URL POST JSON；env 为空则静默跳过。"""
        url = os.getenv(self.cfg.webhook_url_env, "")
        if not url:
            return
        payload = {
            "text": f"[{n.level}] {n.title}: {n.message}",
            "level": n.level,
            "title": n.title,
            "message": n.message,
            "meta": n.meta,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            _ = resp.read()  # 不关心响应体，只确认不抛错

    def _telegram(self, n: Notification) -> None:
        """通过 Telegram Bot API 发消息；env 缺失则静默跳过。"""
        token = os.getenv(self.cfg.telegram_token_env, "")
        chat = os.getenv(self.cfg.telegram_chat_env, "")
        if not token or not chat:
            return
        text = f"[{n.level}] {n.title}\n{n.message}"
        qs = urllib.parse.urlencode({"chat_id": chat, "text": text})
        url = f"https://api.telegram.org/bot{token}/sendMessage?{qs}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            _ = resp.read()

    def _email(self, n: Notification) -> None:
        """通过 SMTP 发送纯文本邮件；任一必要 env 缺失则静默跳过。"""
        host = os.getenv(self.cfg.email_smtp_host_env, "")
        port_s = os.getenv(self.cfg.email_smtp_port_env, "")
        user = os.getenv(self.cfg.email_user_env, "")
        pwd = os.getenv(self.cfg.email_pass_env, "")
        to_addr = os.getenv(self.cfg.email_to_env, "")
        if not (host and port_s and user and pwd and to_addr):
            return
        try:
            port = int(port_s)
        except ValueError:
            _log.error("SMTP 端口非整数，已跳过邮件发送: %r", port_s)
            return

        body = f"级别: {n.level}\n时间: {n.ts}\n\n{n.message}\n"
        msg = (
            f"From: {user}\r\n"
            f"To: {to_addr}\r\n"
            f"Subject: {n.title}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"\r\n"
            f"{body}"
        ).encode("utf-8")
        with smtplib.SMTP(host, port, timeout=8) as server:
            server.starttls()
            server.login(user, pwd)
            server.sendmail(user, [to_addr], msg)


# --------------------------------------------------------------------------
# 便捷函数
# --------------------------------------------------------------------------
def get_notifier(cfg: NotifyConfig | None = None) -> Notifier:
    """返回 Notifier 实例；未传入 cfg 则使用默认 NotifyConfig。"""
    return Notifier(cfg if cfg is not None else NotifyConfig())


def load_notifications(path: str = "runs/notifications.jsonl", limit: int = 50) -> list[dict]:
    """读取 JSONL 通知文件，返回最后 limit 条（顺序为旧→新）。

    文件不存在返回 []；单行解析失败则跳过该行，不中断。
    """
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    _log.warning("通知文件存在畸形行，已跳过: %s", path)
    except OSError as exc:
        _log.error("读取通知文件失败: %s: %s", path, exc)
        return []
    if limit and limit < len(out):
        out = out[-limit:]
    return out


def clear_notifications(path: str = "runs/notifications.jsonl") -> None:
    """清空通知文件（不存在则创建空文件）。"""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.truncate(0)


# --------------------------------------------------------------------------
# 演示
# --------------------------------------------------------------------------
if __name__ == "__main__":
    # 演示用临时文件中转 inapp 通知，避免污染仓库
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
    tmp.close()
    democfg = NotifyConfig(channels=["console", "inapp"], inapp_path=tmp.name)

    notifier = get_notifier(democfg)
    notifier.send("info", "交易已开仓", "BTCUSDT 多单 @ 68000，仓位 0.5x", meta={"symbol": "BTCUSDT"})
    notifier.send("critical", "止损触发", "ETHUSDT 已触及止损价 -3.2%", meta={"pnl": -0.032})

    print("\n--- 从 inapp JSONL 读回的通知 ---")
    for entry in load_notifications(tmp.name):
        print(json.dumps(entry, ensure_ascii=False))

    # 清理临时文件
    clear_notifications(tmp.name)
    os.remove(tmp.name)
    print("\n演示完成，临时文件已清理。")
