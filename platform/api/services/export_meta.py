# -*- coding: utf-8 -*-
"""导出服务元数据：导出模式列表 + 代理（来源）列表，从远端实时取。

这两份数据的权威源在导出服务（EXPORT_BASE）那边：

  - `GET /export/mode_list`  → 导出模式（id / mode_label / obs_sub / pipeline …）
  - `GET /export/proxy_list` → 代理来源（source / root_id / root_path / ips[]）

平台侧原先各自硬编码/手写本地文件（前端 exportModes 数组、settings/proxy_name.json），
远端新增或改名后平台看不到。现在统一改成远端实时取，本地仅作降级兜底。

缓存：成功 60s、失败 30s（负缓存）。/api/stats 每 30s 被每个打开看板的客户端拉一次，
远端故障时若不做负缓存，每次请求都要吃一个 5s 超时，看板会整体变慢。

本模块只做 HTTP + 进程内缓存，不碰 DB、不读本地文件（兜底在 proxy.py / 各 router）。
"""
from __future__ import annotations

import time
from urllib.parse import quote

import httpx

from ..core.config import settings

# 远端挂掉时不要拖慢看板：导出提交那条链路用 30s，这里只是取个列表，5s 够了
_TIMEOUT = 5.0
_TTL_OK = 60.0
_TTL_ERR = 30.0

# {"modes": (expire_ts, [...]), "proxies": (expire_ts, [...])}
# 失败时也写一条（value 为 []），靠 _TTL_ERR 做负缓存
_cache: dict[str, tuple[float, list[dict]]] = {}


def _configured() -> bool:
    return bool(settings.EXPORT_BASE and settings.EXPORT_ACCESS_KEY)


async def _get_json(path: str) -> dict:
    """GET 远端导出服务，返回 JSON dict。任何异常/非 2xx/非 JSON 一律抛 RuntimeError。

    调用方（_cached）统一按「失败 → 空列表 + 负缓存」处理，不往上抛 HTTP 错误：
    模式/代理列表是辅助数据，远端挂了不该让整个看板或详情页 500。
    """
    base = (settings.EXPORT_BASE or "").rstrip("/")
    url = f"{base}{path}?access-key={quote(settings.EXPORT_ACCESS_KEY)}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            resp = await client.get(url)
    except httpx.RequestError as e:
        raise RuntimeError(f"导出服务不可达: {e}")
    if resp.status_code >= 400:
        raise RuntimeError(f"导出服务返回 {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        raise RuntimeError("导出服务返回非 JSON 响应")
    if not isinstance(body, dict):
        raise RuntimeError("导出服务返回体不是对象")
    return body


async def _cached(key: str, path: str, field: str) -> list[dict]:
    """带 TTL 的列表取数。远端失败/未配置时返回 []，由调用方决定兜底。"""
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1]
    if not _configured():
        return []  # 不写缓存：配置随时可能补上（.env + 重启），没必要再压 30s
    try:
        body = await _get_json(path)
        items = [x for x in (body.get(field) or []) if isinstance(x, dict)]
        _cache[key] = (now + _TTL_OK, items)
        return items
    except RuntimeError:
        _cache[key] = (now + _TTL_ERR, [])
        return []


async def list_modes() -> list[dict]:
    """远端导出模式（已过滤 public=False）。失败返回 []，调用方用本地兜底白名单。

    public 缺省视为 True：远端老版本没有这个字段，缺省当内部模式过滤掉会把下拉清空。
    """
    modes = await _cached("modes", "/export/mode_list", "modes")
    return [m for m in modes if m.get("id") and m.get("public", True)]


async def list_proxies() -> list[dict]:
    """远端代理来源列表。失败返回 []，调用方回退本地 proxy_name.json。"""
    return await _cached("proxies", "/export/proxy_list", "proxies")


def proxy_map_from_proxies(proxies: list[dict]) -> dict:
    """代理列表 → {IP: 代理名}，供 proxy.proxy_identity 查表。

    一个 source 可能挂多个 IP（ips[] 逐个展开）；source 为空或 ips 为空的条目跳过。
    """
    out = {}
    for p in proxies or []:
        source = (p.get("source") or "").strip()
        if not source:
            continue
        for ip in (p.get("ips") or []):
            ip = str(ip).strip()
            if ip:
                out[ip] = source
    return out


def mode_options(modes: list[dict]) -> list[dict]:
    """模式列表 → 前端下拉选项 {value, label, desc}。

    label 用 mode_label（「导出」「合并导出」），desc 用 label（带括号的长说明），
    与导出服务自己页面上的叫法保持一致。
    """
    out = []
    for m in modes or []:
        mode_id = m.get("id")
        if not mode_id:
            continue
        out.append({
            "value": mode_id,
            "label": m.get("mode_label") or m.get("label") or mode_id,
            "desc": m.get("label") or "",
        })
    return out
