# -*- coding: utf-8 -*-
"""导出服务元数据端点：给前端下拉框喂「导出方式」「代理来源」列表。

数据来自导出服务（.env 的 EXPORT_BASE / EXPORT_ACCESS_KEY）的两个实时接口，
平台只做转发 + 字段裁剪，不缓存业务语义（缓存在 services/export_meta.py，60s）。

为什么单独开一个 router 而不是挂在 /api/instances 下：instances.py 有
`GET /{instance_id}` 这条通配路由，任何 /api/instances/xxx 都会被它先匹配走。

access-key 只在服务端拼，前端拿不到。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends

from ..core.database import async_query
from ..core.security import get_current_user
from ..services import export_meta as export_meta_service
from ..services import proxy as proxy_service
from .batch_output import _DEFAULT_MODE, _FALLBACK_EXPORT_MODES

router = APIRouter(prefix="/api/export-meta", tags=["export-meta"])

# 聚合 base_url 候选时回看的实例数。覆盖全部在用端点即可（实测近 3000 条
# 实例里归一化后只有 20 个端点），没必要全表扫 create_params。
_BASE_URL_SCAN_LIMIT = 3000


@router.get("/modes")
async def get_export_modes(user: dict = Depends(get_current_user)):
    """导出方式下拉选项。

    返回 {modes: [{value, label, desc}], default, source}。
    source=remote 表示取到了远端实时列表；fallback 表示远端不可达（或未配
    EXPORT_BASE），用的是本地兜底白名单——前端可据此提示「列表可能过期」。
    """
    modes = await export_meta_service.list_modes()
    source = "remote" if modes else "fallback"
    if not modes:
        modes = _FALLBACK_EXPORT_MODES
    return {
        "modes": export_meta_service.mode_options(modes),
        "default": _DEFAULT_MODE,
        "source": source,
    }


@router.get("/base-urls")
async def get_base_url_options(user: dict = Depends(get_current_user)):
    """新建任务页「模型 Base URL」下拉候选。

    返回 {options: [{value, proxy_name, host, used}], source}。
    前端是 filterable + allow-create 的下拉，这里只负责给候选，不做校验——
    下拉里没有的地址（新代理、外部网关）用户仍可直接手填。

    候选以历史用量为主、已登记代理为辅，原因见 proxy.build_base_url_options。
    """
    rows = await async_query(
        "SELECT create_params FROM task_instances "
        "WHERE create_params IS NOT NULL "
        "ORDER BY rowid DESC LIMIT ?",
        (_BASE_URL_SCAN_LIMIT,),
    )
    # 主 Agent 与各 agent 的 base_url 混在一起聚合：它们指向同一批代理，
    # 分开统计会让用户模拟那边的下拉少掉一半候选。
    raw_urls = []
    for r in rows:
        try:
            params = json.loads(r["create_params"])
        except (ValueError, TypeError):
            continue
        if not isinstance(params, dict):
            continue
        raw_urls.append(params.get("model_base_url"))
        raw_urls.append(params.get("user_proxy_base_url"))
        for a in (params.get("agents") or []):
            if isinstance(a, dict):
                raw_urls.append(a.get("base_url"))

    proxies = await export_meta_service.list_proxies()
    proxy_map = export_meta_service.proxy_map_from_proxies(proxies)
    source = "remote" if proxy_map else "fallback"
    if not proxy_map:
        proxy_map = proxy_service.load_proxy_map()
    return {
        "options": proxy_service.build_base_url_options(
            raw_urls, proxy_map, proxy_ips=proxy_map
        ),
        "source": source,
    }


@router.get("/proxies")
async def get_export_proxies(user: dict = Depends(get_current_user)):
    """代理（src 来源）列表：source / root_id / ips。

    root_path 不外露（是导出服务那边的内部挂载路径，前端用不到）。
    """
    proxies = await export_meta_service.list_proxies()
    return {
        "proxies": [
            {
                "source": p.get("source") or "",
                "root_id": p.get("root_id") or "",
                "ips": [str(ip) for ip in (p.get("ips") or [])],
            }
            for p in proxies
        ],
        "source": "remote" if proxies else "fallback",
    }
