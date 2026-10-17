"""资源池总量 —— 只读配置，供首页看板算「已用 / 总量」。

资源池的唯一标识就是沙箱配置里的 `sandbox_namespace`（同一个 namespace 下的
沙箱跑在同一个集群里，共享同一份配额）；`global.controlplane_base_url` 只用来
把沙箱提交到对应控制面，不参与归属判断。

总量放在 settings/sandbox/resource_pools.yaml：

    default: omni-env-default-worker   # 历史实例归入的默认池
    pools:
      omni-env-default-worker:
        total_cpu: 256          # 核
        total_memory_gb: 1024   # GiB

`default` 缺失时回退到文件里的第一个池（再没有就用 DEFAULT_NAMESPACE）。
文件缺失、缺池或格式错误一律降级为「总量 0」，前端显示「未配置」而不是报错。
"""
import os
from typing import Optional

from omegaconf import OmegaConf
from fastapi import APIRouter, Depends

from ..core.config import settings
from ..core.security import get_current_user

router = APIRouter(prefix="/api/resource-pools", tags=["resource-pools"])

POOLS_FILE = os.path.join(settings.SETTINGS_DIR, "sandbox", "resource_pools.yaml")

# 配置文件完全读不到时的最后兜底（正常情况下以文件里的 default 为准）
DEFAULT_NAMESPACE = "omni-env-default-worker"


def _num(v, default: float = 0.0) -> float:
    """兼容 "256" / 256 / null 三种写法。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _load_cfg() -> dict:
    if not os.path.isfile(POOLS_FILE):
        return {}
    try:
        return OmegaConf.to_container(OmegaConf.load(POOLS_FILE), resolve=True) or {}
    except Exception:
        return {}


def load_pools() -> dict:
    """{namespace: {"total_cpu": float, "total_memory_gb": float}}，读不到返回 {}。"""
    pools = _load_cfg().get("pools") or {}
    out = {}
    for name, spec in pools.items():
        spec = spec or {}
        out[str(name)] = {
            "total_cpu": _num(spec.get("total_cpu")),
            "total_memory_gb": _num(spec.get("total_memory_gb")),
        }
    return out


def default_namespace() -> str:
    """历史实例（快照里没有 sandbox_namespace）归入的池。

    以 resource_pools.yaml 的 `default` 为准；没写就取第一个池；
    文件整个缺失时退回 DEFAULT_NAMESPACE。
    """
    cfg = _load_cfg()
    name = str(cfg.get("default") or "").strip()
    if name:
        return name
    pools = list((cfg.get("pools") or {}).keys())
    return str(pools[0]) if pools else DEFAULT_NAMESPACE


@router.get("")
def list_resource_pools(user: dict = Depends(get_current_user)):
    pools = load_pools()
    items = [
        {"namespace": name, **vals}
        for name, vals in pools.items()
    ]
    return {
        "pools": items,
        # 全局总量：各池之和。未归属任何池的实例只计入「已用」，不进分母。
        "total_cpu": sum(p["total_cpu"] for p in items),
        "total_memory_gb": sum(p["total_memory_gb"] for p in items),
        "default_namespace": default_namespace(),
    }
