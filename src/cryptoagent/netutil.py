"""TLS 上下文：优先用 certifi 的 CA 包，解决 Windows Server 根证书缺失。

为什么需要这个：
Windows Server（尤其是没跑过 Windows Update 的）系统根证书库经常是旧的/空的，
Python 的 ssl 默认会用它，于是访问币安时报
    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate
这跟 API Key 一点关系都没有，纯粹是链验证失败。

装了 certifi 就用它的 certifi.where()（随包发布的 Mozilla CA 集合），
没装则退回系统默认——至少行为不比以前差。
"""
from __future__ import annotations

import ssl
import urllib.request

_CTX: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    global _CTX
    if _CTX is None:
        try:
            import certifi

            _CTX = ssl.create_default_context(cafile=certifi.where())
        except Exception:  # noqa: BLE001 - 没装 certifi 就退回系统库
            _CTX = ssl.create_default_context()
    return _CTX


def open_url(req, timeout: float = 8.0):
    """统一外呼入口：带上 CA 上下文与超时。

    通知 / 新闻源以前直接 `urlopen`，不传 context，在根证书缺失的 Windows
    上会「行情能用、告警全失败」；统一走这里就不会再有第二套 SSL 行为。
    """
    return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())
