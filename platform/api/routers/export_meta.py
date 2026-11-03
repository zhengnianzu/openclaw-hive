# -*- coding: utf-8 -*-
"""导出服务元数据端点：给前端下拉框喂「导出方式」「代理来源」列表。

数据来自导出服务（.env 的 EXPORT_BASE / EXPORT_ACCESS_KEY）的两个实时接口，
平台只做转发 + 字段裁剪，不缓存业务语义（缓存在 services/export_meta.py，60s）。

为什么单独开一个 router 而不是挂在 /api/instances 下：instances.py 有
`GET /{instance_id}` 这条通配路由，任何 /api/instances/xxx 都会被它先匹配走。

access-key 只在服务端拼，前端拿不到。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..core.security import get_current_user
from ..services import export_meta as export_meta_service
from .batch_output import _DEFAULT_MODE, _FALLBACK_EXPORT_MODES

router = APIRouter(prefix="/api/export-meta", tags=["export-meta"])


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
