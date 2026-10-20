# -*- coding: utf-8 -*-
"""代理（自部署模型网关）识别工具。

数据侧只有实例 create_params 里的 base_url（形如 http://115.120.62.121:8082/v1），
而运营上关心的是「这批请求压在哪台 jumper 上」。两者通过 settings/proxy_name.json
的 IP → 代理名映射接上。

base_url 的写法并不统一：主 Agent 常见 `http://ip:8082`，user_simulator 要求带
`/v1`，还有带尾斜杠的历史数据。这里统一归一化成 `http://ip:port`，否则同一台
代理会被拆成三行。

本模块只做纯字符串处理 + 读一个小 JSON，不碰 DB。
"""
import json
import os
import re
import time
from typing import Optional

from ..core.config import settings

_PROXY_NAME_FILE = os.path.join(settings.SETTINGS_DIR, "proxy_name.json")

# 只认 IPv4 字面量：内网网关都是 IP；不是 IP 时退回 URL 里的 host 段
_IP_RE = re.compile(r"https?://([0-9]{1,3}(?:\.[0-9]{1,3}){3})")
_HOST_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/:]+)")

# 读文件很便宜，但 /api/stats 每 30s 被每个打开看板的客户端拉一次，按 mtime 缓存
_cache = {"mtime": None, "map": {}}


def load_proxy_map() -> dict:
    """IP → 代理名。文件缺失/解析失败/非 dict 一律返回空映射。"""
    try:
        st = os.stat(_PROXY_NAME_FILE)
    except OSError:
        return {}
    if _cache["mtime"] == st.st_mtime:
        return _cache["map"]
    try:
        with open(_PROXY_NAME_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    out = data if isinstance(data, dict) else {}
    _cache["mtime"] = st.st_mtime
    _cache["map"] = out
    return out


def normalize_base_url(url: Optional[str]) -> str:
    """去掉尾斜杠与结尾的 `/v1`，返回 `http://ip:port` 形态。

    只剥一层 /v1：user_simulator 的 base_url 是 `.../v1`，主 Agent 是裸地址，
    两者指向同一台代理，必须归并。路径中间出现的 v1 不动。
    """
    if not url:
        return ""
    text = str(url).strip()
    if not text:
        return ""
    text = text.rstrip("/")
    if text.lower().endswith("/v1"):
        text = text[:-3].rstrip("/")
    return text


def _host_of(url: str) -> str:
    """从 URL 取主机标识：优先 IPv4 字面量，否则退回 host 段（不含端口）。"""
    if not url:
        return ""
    m = _IP_RE.search(url)
    if m:
        return m.group(1)
    m = _HOST_RE.match(url)
    return m.group(1) if m else ""


def proxy_identity(url: Optional[str], proxy_map: Optional[dict] = None) -> dict:
    """base_url → {host, proxy_name, baseurl}。

    proxy_name 未登记时为空串（前端用 IP 兜底显示），不抛错。
    """
    normalized = normalize_base_url(url)
    host = _host_of(normalized)
    proxy_map = proxy_map or {}
    return {
        "host": host,
        "proxy_name": proxy_map.get(host, "") if host else "",
        "baseurl": normalized,
    }
