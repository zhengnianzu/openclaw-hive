"""资源池总量 —— 只读配置，供首页看板算「已用 / 总量」。

资源池的唯一标识就是沙箱配置里的 `sandbox_namespace`（同一个 namespace 下的
沙箱跑在同一个集群里，共享同一份配额）；`global.controlplane_base_url` 只用来
把沙箱提交到对应控制面，不参与归属判断。

容量放在 settings/sandbox/resource_pools.yaml，两种写法：

**按机器组（推荐）** —— 一个池由若干配置不同的机器组构成，总量与沙箱并发由本模块派生：

    default: omni-env-default-worker   # 历史实例归入的默认池
    pools:
      omni-env-default-worker:
        groups:
          - name: group1
            nodes: 38                  # 该组机器台数
            cpu_per_node: 384          # 单机核数
            memory_gb_per_node: 1536   # 单机内存，GiB
            max_sandboxes_per_node: 256  # 单机最多能塞多少沙箱（声明上限）

单机实际可用沙箱数 = min(max_sandboxes_per_node, cpu_per_node)：上限只是声明，
核数不够时会被核数压住（64 核的机器即使声明 256 也只能开 64 个）。派生：

    total_cpu       = Σ nodes × cpu_per_node
    total_memory_gb = Σ nodes × memory_gb_per_node
    max_sandboxes   = Σ nodes × 单机实际可用沙箱数
    frames          = Σ nodes（机器总台数）

**直接写总数（老格式，仍兼容）** —— 无法得知沙箱并发，`max_sandboxes` 记为 0（看板显示「—」）：

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


def usable_sandboxes_per_node(cpu_per_node, max_sandboxes_per_node) -> float:
    """单机实际能开多少个沙箱。

    `max_sandboxes_per_node` 只是声明上限，不是保证——机器核数不够时会被核数压住
    （64 核的机器即使声明 256，也只能开 64 个）。上限没写时只受核数约束。
    """
    cpu = _num(cpu_per_node)
    cap = _num(max_sandboxes_per_node)
    if cap <= 0:
        return cpu
    return min(cap, cpu)


def _derive_group(raw: dict, index: int) -> dict:
    """单个机器组 → 带派生量的 dict。缺 name 时用 group{index+1}。"""
    raw = raw or {}
    name = str(raw.get("name") or f"group{index + 1}")
    nodes = _num(raw.get("nodes"))
    cpu_per_node = _num(raw.get("cpu_per_node"))
    memory_gb_per_node = _num(raw.get("memory_gb_per_node"))
    max_per_node = _num(raw.get("max_sandboxes_per_node"))
    per_node = usable_sandboxes_per_node(cpu_per_node, max_per_node)
    return {
        "name": name,
        "nodes": nodes,
        "cpu_per_node": cpu_per_node,
        "memory_gb_per_node": memory_gb_per_node,
        "max_sandboxes_per_node": max_per_node,
        # 核数不够时这个值会小于声明上限，界面可据此提示「被核数限制」
        "usable_sandboxes_per_node": per_node,
        "total_cpu": nodes * cpu_per_node,
        "total_memory_gb": nodes * memory_gb_per_node,
        "max_sandboxes": nodes * per_node,
    }


def derive_groups(groups) -> dict:
    """机器组列表 → 池级派生量。空列表时全部为 0。"""
    items = [_derive_group(g, i) for i, g in enumerate(groups or [])]
    return {
        "groups": items,
        "frames": sum(g["nodes"] for g in items),
        "total_cpu": sum(g["total_cpu"] for g in items),
        "total_memory_gb": sum(g["total_memory_gb"] for g in items),
        "max_sandboxes": sum(g["max_sandboxes"] for g in items),
    }


def _load_cfg() -> dict:
    if not os.path.isfile(POOLS_FILE):
        return {}
    try:
        return OmegaConf.to_container(OmegaConf.load(POOLS_FILE), resolve=True) or {}
    except Exception:
        return {}


def _derive_pool(spec) -> dict:
    """单个池的容量。有 groups 走派生，否则按老格式读总数。

    `configured` 表示这个池登记过容量；`grouped` 表示沙箱并发是否可知
    （老格式拿不到，`max_sandboxes` 保持 0，前端显示「—」）。
    """
    spec = spec or {}
    groups = spec.get("groups")
    if groups:
        derived = derive_groups(groups)
        return {
            "total_cpu": derived["total_cpu"],
            "total_memory_gb": derived["total_memory_gb"],
            "max_sandboxes": derived["max_sandboxes"],
            "frames": derived["frames"],
            "groups": derived["groups"],
            "configured": True,
            "grouped": True,
        }
    return {
        "total_cpu": _num(spec.get("total_cpu")),
        "total_memory_gb": _num(spec.get("total_memory_gb")),
        "max_sandboxes": 0.0,
        "frames": 0.0,
        "groups": [],
        "configured": True,
        "grouped": False,
    }


def load_pools() -> dict:
    """{namespace: 池容量 dict}，读不到返回 {}。

    池容量字段见 `_derive_pool`：total_cpu / total_memory_gb / max_sandboxes /
    frames / groups / configured / grouped。
    """
    pools = _load_cfg().get("pools") or {}
    return {str(name): _derive_pool(spec) for name, spec in pools.items()}


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
        # 沙箱并发上限 = Σ 各池「节点数 × 单机可用沙箱数」；老格式池拿不到，计 0
        "max_sandboxes": sum(p["max_sandboxes"] for p in items),
        "frames": sum(p["frames"] for p in items),
        "default_namespace": default_namespace(),
    }
