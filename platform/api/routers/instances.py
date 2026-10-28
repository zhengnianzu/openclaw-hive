import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from omegaconf import OmegaConf

from ..core.config import settings
from ..core.database import get_connection, async_execute, async_query, async_query_one
from ..core.security import get_current_user, require_operator
from ..models.instance import (
    InstanceCreate, InstanceInfo, InstanceOverview, OverviewBatchRequest,
)
from ..services.queue_scheduler import (
    schedule_queue, OCCUPYING_STATUSES,
    slot_condition_sql as _slot_condition_sql,
    slot_condition_params as _slot_condition_params,
)
from ..services.create_params import build_create_params
from . import harness_meta
from . import harness_configs
from . import sandbox_configs
from . import resource_pools

router = APIRouter(prefix="/api/instances", tags=["instances"])

JIUWENSWARM_TYPES = {"jiuwenswarm", "jiuwenswarm-dev", "jiuwenswarm-xiaoyi"}

# 占用队列槽位的状态（见 queue_scheduler.OCCUPYING_STATUSES），拼成 SQL 占位符
_OCCUPYING_STATUSES = OCCUPYING_STATUSES
_OCCUPYING_PLACEHOLDERS = ",".join("?" * len(_OCCUPYING_STATUSES))

# 独立的 key 申请路由（路径 /api/generate-api-key，不带 instances 前缀）
key_router = APIRouter(prefix="/api", tags=["api-key"])

ALLOWED_CONFIG_FILES = harness_meta.ALLOWED_CONFIG_FILES()

_status_cache = {}
_STATUS_CACHE_TTL = 5

# _sync_task_records 每写入这么多行提交一次（见函数内注释）。
# 与 offline/output_worker.py 的 _discover_artifacts 保持同一量级。
_SYNC_BATCH_ROWS = 500

_analyze_cache = {}
_ANALYZE_CACHE_TTL = 10

# 与 _analyze_cache 同一时刻写入：config_path -> error_tree(list)
# 携带异常的一级分类(C/S/T/X)汇总 + 具体错误码明细，供前端折叠展示
_analyze_tree_cache = {}

# 一级分类中文名
_PREFIX_LABEL = {"C": "客户端", "S": "服务端", "T": "任务侧", "X": "未分类"}
# 错误码 -> 描述（镜像 hive.py 的 ERROR_CATALOG；此处复制以免为一处展示拉起 hive 依赖）
_ERROR_CODE_DESC = {
    "C001": "extract code failed", "C002": "upload file failed",
    "C003": "environment creation failed", "C004": "agent config write failed",
    "S001": "gateway start failed", "S002": "gateway startup timeout",
    "S003": "gateway unexpected output", "S004": "sandbox port-update failed",
    "S005": "skill download failed", "S006": "user profile download failed",
    "S007": "agents download failed", "S008": "script execution failed",
    "S009": "upload traj to OBS failed",
    "T001": "Task_Failed", "T002": "达到最大轮次", "T003": "连续3次未收到回复",
    "T004": "AgentExecutionError", "T005": "AssertionError",
    "T006": "API call failed", "T007": "TimeoutError", "T008": "HarnessError",
    "T009": "RuntimeError",
    "T010": "Uncategorized Traceback", "X999": "unclassified exception",
}

# 增量扫描状态: config_path -> {fname: (mtime, size, status, code)}
# 只重读 mtime/size 变化过的 task 日志，避免每次全量遍历上万文件
_analyze_file_state = {}

# 本轮 _compute_analyze 中「实际重读过」（签名变化）的 fname 集合: config_path -> set(fname)
# 供 _sync_task_records 只 UPSERT 变化过的任务行，避免每 8s 全量重写整表。
# None（键不存在）表示该 config 还没被扫过 → 写库需走全量兜底。
_analyze_changed = {}

# 后台预热开关：为 True 时请求线程只读缓存（哪怕已过期），扫描交给后台任务，
# 避免同步端点在请求线程里做重扫把 anyio 线程池打满
_bg_refresh_active = False


def _load_key_gen_config() -> dict:
    """加载 key 申请配置 settings/key_gen.yaml，文件不存在或解析失败时返回空配置。"""
    cfg_path = os.path.join(settings.SETTINGS_DIR, "key_gen.yaml")
    if not os.path.exists(cfg_path):
        return {}
    try:
        return OmegaConf.to_container(OmegaConf.load(cfg_path), resolve=True) or {}
    except Exception:
        return {}


def _resolve_key_gen_rule(base_url: str, cfg: dict) -> dict:
    """
    根据传入 base_url 解析出实际使用的申请规则。
    返回 {base_url, method, endpoint, params, response_key_field}。

    规则匹配：
    - 传入 base_url 命中 endpoints 里某条 match 前缀 -> 用该条的 method/endpoint/params，base_url 用传入的
    - 传入 base_url 但未命中任何 match          -> 用传入 base_url + default 的 method/endpoint/params
    - 未传 base_url                             -> 用 default.base_url（若为 "*" 则视为未指定，由上层报错）
    """
    default = cfg.get("default") or {}
    endpoints = cfg.get("endpoints") or []
    incoming = (base_url or "").strip()

    def _method(rule):
        return str(rule.get("method") or default.get("method") or "GET").upper()

    if incoming:
        for rule in endpoints:
            match = str(rule.get("match", "")).strip()
            if match and match in incoming:
                return {
                    "base_url": incoming,
                    "method": _method(rule),
                    "endpoint": rule.get("endpoint") or default.get("endpoint") or "/api/invite",
                    "params": rule.get("params") or {},
                    "response_key_field": rule.get("response_key_field")
                        or default.get("response_key_field") or "api_key",
                }
        # 未命中：用传入 base_url + default 的其余设置（default.base_url 为 "*" 即沿用传入）
        return {
            "base_url": incoming,
            "method": str(default.get("method") or "GET").upper(),
            "endpoint": default.get("endpoint") or "/api/invite",
            "params": default.get("params") or {},
            "response_key_field": default.get("response_key_field") or "api_key",
        }

    # 未传 base_url：走 default；若 default.base_url 是通配 "*" 则无从确定，返回空由上层报错
    default_base = str(default.get("base_url", "")).strip()
    if default_base == "*":
        default_base = ""
    return {
        "base_url": default_base,
        "method": str(default.get("method") or "GET").upper(),
        "endpoint": default.get("endpoint") or "/api/invite",
        "params": default.get("params") or {},
        "response_key_field": default.get("response_key_field") or "api_key",
    }


async def _request_api_key(base_url: str, invite_code: str, name: str) -> str:
    cfg = _load_key_gen_config()
    rule = _resolve_key_gen_rule(base_url, cfg)

    effective_base = rule["base_url"]
    if not effective_base:
        raise ValueError("未配置 key 申请的 base_url（key_gen.yaml.default.base_url 或传入 model_base_url）")

    # 去掉尾部 /v1
    effective_base = effective_base.rstrip("/")
    if effective_base.endswith("/v1"):
        effective_base = effective_base[:-3]

    api_url = effective_base + "/" + str(rule["endpoint"]).lstrip("/")
    method = rule["method"]

    # invite_code + name + 规则里的额外固定参数（如 token）
    payload = {"invite_code": invite_code, "name": name}
    extra = rule["params"]
    if isinstance(extra, dict):
        for k, v in extra.items():
            if v is not None and str(v) != "":
                payload[str(k)] = v

    key_field = str(rule["response_key_field"]) or "api_key"
    timeout = cfg.get("timeout", 15)

    async with httpx.AsyncClient(timeout=timeout) as client:
        if method == "POST":
            # 部分服务要求带 X-Requested-With 才返回 JSON
            resp = await client.post(
                api_url, json=payload,
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        else:
            resp = await client.get(api_url, params=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get(key_field, "")


@key_router.post("/generate-api-key")
async def generate_api_key(
    invite_code: str,
    name: str,
    base_url: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """
    独立的 API Key 申请接口（供前端"新建KEY"按钮调用）。
    复用 _request_api_key：按 key_gen.yaml 依据 base_url 匹配对应服务，
    命中的服务会自动附带其配置的固定参数（如 8082 的 token）。
    """
    if not invite_code:
        raise HTTPException(status_code=400, detail="invite_code 不能为空")
    try:
        api_key = await _request_api_key(base_url or "", invite_code, name)
    except httpx.HTTPStatusError as e:
        # 把上游服务的真实报错透出来（如 "Token not provided"）
        detail = ""
        try:
            detail = e.response.json().get("message") or e.response.text
        except Exception:
            detail = e.response.text
        raise HTTPException(status_code=502, detail=f"申请 key 失败：{detail}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"申请 key 失败：{e}")

    if not api_key:
        raise HTTPException(status_code=502, detail="上游服务未返回 api_key")
    return {"api_key": api_key}


def _get_instance_dir(instance_id: str) -> str:
    return os.path.join(settings.INSTANCE_DIR, instance_id)


def _get_output_dir(config_path: str) -> str:
    config_basename = Path(config_path).stem
    instance_dir = str(Path(config_path).parent)
    return os.path.join(instance_dir, "outputs", config_basename)


def _count_lines(file_path: str) -> int:
    if not os.path.exists(file_path):
        return 0
    count = 0
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            count += chunk.count(b"\n")
    return count


def _is_pid_running(pid: int) -> bool:
    """判断进程是否真的还活着。

    只用 os.kill(pid, 0) 不够：僵尸进程（已退出、父进程还没回收）的进程表项仍在，
    kill 会返回成功，于是被误判为「运行中」。任务因此永远卡在 running ——
    pid_alive 恒 True、total_tasks 又可能为 0，all_done 永远不成立，
    后台 refresher 也就永远不把它判成终态，同队列后面的任务全被堵死。
    这里额外检查进程状态，Z（zombie）视为已结束。
    """
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # 僵尸/已死进程：读 /proc/<pid>/stat 取第 3 个字段（状态）
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
        # 进程名可能含空格/括号，取最后一个 ')' 之后再分割
        state = data[data.rindex(b")") + 2:].split(b" ", 1)[0].decode()
        if state == "Z":
            return False
    except (OSError, ValueError, IndexError):
        # 读不到 /proc（非 Linux 等）时退回 kill 的判断
        pass
    return True


def _sync_instance_status(instance: dict, persist: bool = False) -> dict:
    """同步 running 实例的 completed/failed 计数与状态。

    persist=False（默认，所有 GET 请求路径）：只算内存计数 + 读写内存缓存，**绝不写库**，
    避免读请求在 GET 路径里争用 SQLite 写锁（这是 "database is locked" 的根因）。
    persist=True（仅后台刷新任务 _refresh_running_once）：把最新计数/状态 UPDATE 落库。
    """
    inst = dict(instance)

    if inst["status"] not in ("running", "preparing"):
        return inst

    if inst["status"] == "preparing":
        return inst

    output_dir = _get_output_dir(inst["config_path"])

    now = time.time()
    cache_key = inst["id"]
    cached = _status_cache.get(cache_key)
    # 本进程没扫过：先尝试复用 leader 落盘的快照（_count_lines 要整读 NFS 上的 jsonl）
    if cached is None:
        _load_shared_caches()
        cached = _status_cache.get(cache_key)
    if cached and (now - cached[1]) < _STATUS_CACHE_TTL:
        inst["completed_tasks"] = cached[0]["completed_tasks"]
        inst["failed_tasks"] = cached[0]["failed_tasks"]
        inst["status"] = cached[0]["status"]
        return inst

    # 后台预热启用时，请求线程只读缓存（哪怕已过期），把扫描/写库都留给后台任务，
    # 从根本上避免读请求触发文件扫描与写锁争用
    if not persist and _bg_refresh_active and cached:
        inst["completed_tasks"] = cached[0]["completed_tasks"]
        inst["failed_tasks"] = cached[0]["failed_tasks"]
        inst["status"] = cached[0]["status"]
        return inst

    completed = _count_lines(os.path.join(output_dir, "complete.jsonl"))
    failed = _count_lines(os.path.join(output_dir, "failed.jsonl"))
    inst["completed_tasks"] = completed
    inst["failed_tasks"] = failed

    pid_alive = _is_pid_running(inst.get("pid"))
    all_done = inst["total_tasks"] > 0 and (completed + failed) >= inst["total_tasks"]
    # 进程已结束：即使 total_tasks 为 0（例如统计任务数时目录里没有输入文件），
    # 也必须判终态。否则「pid 已死 + total_tasks=0」的实例会永远卡在 running，
    # 同队列后续任务全部被堵死。
    if not pid_alive or all_done:
        inst["status"] = "completed" if failed == 0 else "finished"

    # 只有后台刷新任务才写库；请求线程仅更新内存缓存，避免写锁争用
    #
    # 两条 UPDATE 都带 status IN ('running','preparing')：本函数的输入是读库时刻的快照，
    # 期间用户可能已经「停止」（status='stopped'），或调度器已把队列里的下一个任务
    # 领成 preparing。无条件写会把 stopped/preparing 覆盖成 completed，前者让停止失效、
    # 后者让被领走的任务和另一个任务同时占槽位（串行被破坏）。
    if persist:
        if not pid_alive or all_done:
            with get_connection() as conn:
                conn.execute(
                    "UPDATE task_instances SET status=?, completed_tasks=?, failed_tasks=?, stopped_at=? "
                    "WHERE id=? AND status IN ('running','preparing')",
                    (inst["status"], completed, failed, datetime.now().isoformat(), inst["id"]),
                )
        else:
            with get_connection() as conn:
                conn.execute(
                    "UPDATE task_instances SET completed_tasks=?, failed_tasks=? "
                    "WHERE id=? AND status IN ('running','preparing')",
                    (completed, failed, inst["id"]),
                )

    _status_cache[cache_key] = ({"completed_tasks": completed, "failed_tasks": failed, "status": inst["status"]}, now)
    return inst


# ============================================================================
# CRUD
# ============================================================================

def _resolve_suffix_api_keys(d: dict) -> bool:
    """该实例是否开启了 key 沙箱后缀。

    只读 DB 的 suffix_api_keys 列（创建/迁移时写入一次），不做任何快照解析
    或磁盘 IO —— 列表接口 10s 一轮，历史实例再多也不增加开销。
    """
    return bool(d.get("suffix_api_keys"))


def _with_harness_type(d: dict) -> dict:
    params = {}
    if d.get("create_params"):
        try:
            params = json.loads(d["create_params"])
        except Exception:
            params = {}
    d["harness_type"] = params.get("harness_type", "openclaw")
    # 列表/详情页的标识用：该实例创建时是否开启 key 沙箱后缀
    d["suffix_api_keys"] = _resolve_suffix_api_keys(d)
    return d


# ============================================================================
# 沙箱资源：从实例的 config_snapshot 解析单沙箱核数/内存
# ============================================================================

# snapshot 是定值字符串，同一实例每次轮询都一样，按 hash 缓存解析结果，
# 避免 10s 一次的 /instances 反复走 OmegaConf。
# 单沙箱资源解析缓存。键是 (id, snapshot hash, create_params hash)，快照内容不变
# 则永远命中。用 LRU 而不是「满了就整体 clear()」：实例总数约 1.1k、上限 2000，
# 全清一次会让整表 1.1k 份 YAML 同时重新解析（实测约 8s），首页随之卡死。
_RES_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_RES_CACHE_MAX = 4000


def _res_digest(text: str) -> str:
    """快照/参数的稳定摘要。

    不能用内置 hash()：CPython 对 str 的哈希带进程级随机盐（PYTHONHASHSEED），
    多 worker 下同一份快照算出的键各不相同，落盘分片就永远互相命不中。
    """
    return hashlib.md5(text.encode("utf-8", "ignore")).hexdigest()[:16]


# 解析结果按实例落盘成每 worker 一个分片，其它 worker 撞冷缓存时直接读，不必重解析
# YAML。快照一经创建不再变化（create_params 变了 digest 也会变），所以分片无需 TTL。
_RES_SHARD_LAST_CHECK = [0.0]
_RES_SHARD_LAST_SAVE = [0.0]
_RES_SHARD_SAVE_INTERVAL = 30.0
# 只按时间节流会让分片长期停在「刚解析几条」的状态：一次 /stats 请求解析 300+ 条
# 全落在节流窗口内，另一个 worker 冷启动时读到的还是空分片。新增条数到量就立即落盘。
_RES_SHARD_SAVE_MIN_NEW = 64
_RES_SHARD_SAVED_COUNT = [0]


def _res_shard_dir() -> str:
    return os.path.dirname(settings.DB_PATH)


def _res_shard_path(pid: int) -> str:
    return os.path.join(_res_shard_dir(), f".res_cache.{pid}.json")


def _save_res_shard(force: bool = False) -> None:
    """把本进程最近访问的解析结果写到自己的分片。

    时间节流之外再看「新增条数」：批量解析一次就能把冷启动的结果共享出去。
    """
    now = time.time()
    new_items = len(_RES_CACHE) - _RES_SHARD_SAVED_COUNT[0]
    if not force and new_items < _RES_SHARD_SAVE_MIN_NEW \
            and now - _RES_SHARD_LAST_SAVE[0] < _RES_SHARD_SAVE_INTERVAL:
        return
    _RES_SHARD_LAST_SAVE[0] = now
    _RES_SHARD_SAVED_COUNT[0] = len(_RES_CACHE)
    try:
        payload = {
            f"{k[0]}|{k[1]}|{k[2]}": v
            for k, v in list(_RES_CACHE.items())[-_SHARED_CACHE_ENTRY_CAP:]
        }
        path = _res_shard_path(os.getpid())
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _load_res_shards() -> None:
    """把其它 worker 的分片灌进本进程缓存。按 2s 节流，命中路径开销可忽略。"""
    now = time.time()
    if now - _RES_SHARD_LAST_CHECK[0] < 2:
        return
    _RES_SHARD_LAST_CHECK[0] = now
    try:
        d = _res_shard_dir()
        me = os.getpid()
        for name in os.listdir(d):
            if not (name.startswith(".res_cache.") and name.endswith(".json")):
                continue
            try:
                pid = int(name[len(".res_cache."):-len(".json")])
            except ValueError:
                continue
            if pid == me:
                continue
            try:
                with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception:
                continue
            for key, val in (payload or {}).items():
                parts = key.split("|")
                if len(parts) != 3:
                    continue
                cache_key = (parts[0], parts[1], parts[2])
                if cache_key not in _RES_CACHE:
                    _RES_CACHE[cache_key] = val
        while len(_RES_CACHE) > _RES_CACHE_MAX:
            _RES_CACHE.popitem(last=False)
    except Exception:
        pass

# 解析不出来时的兜底，与沙箱配置表单默认值一致
_RES_FALLBACK = {"cpu_request": 2.0, "cpu_limit": 3.0, "memory_request": 6.0, "memory_limit": 8.0}


def _gib(v) -> float:
    """K8s 内存量 → GiB。复用沙箱配置的 _to_quantity（Gi→1024、Mi→1）。"""
    return round(sandbox_configs._to_quantity(v) / 1024, 4)


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_resources(cfg: dict) -> dict:
    """按两套 config 形态解析单沙箱资源。"""
    key = cfg.get("sandbox_active_key")
    if key:
        sb = (cfg.get("sandbox") or {}).get(key) or {}
        res = (sb.get("sandbox") or {}).get("resources") or {}
        if "requests" in res or "limits" in res:
            # Windows 沙箱写法
            req, lim = res.get("requests") or {}, res.get("limits") or {}
            return {
                "cpu_request": _num(req.get("cpu"), 2),
                "cpu_limit": _num(lim.get("cpu"), 3),
                "memory_request": _gib(req.get("memory") or "6Gi"),
                "memory_limit": _gib(lim.get("memory") or "8Gi"),
            }
        # Linux 沙箱写法
        return {
            "cpu_request": _num(res.get("cpu_request"), 2),
            "cpu_limit": _num(res.get("cpu_limit"), 3),
            "memory_request": _gib(res.get("memory_request") or "6Gi"),
            "memory_limit": _gib(res.get("memory_limit") or "8Gi"),
        }

    # 历史形态：env_make.args.resources
    env_make = cfg.get("env_make") or {}
    res = (env_make.get("args") or {}).get("resources") or {}
    if res:
        return {
            "cpu_request": _num(res.get("cpu_request"), 2),
            "cpu_limit": _num(res.get("cpu_limit"), 3),
            "memory_request": _gib(res.get("memory_request") or "6Gi"),
            "memory_limit": _gib(res.get("memory_limit") or "8Gi"),
        }
    return dict(_RES_FALLBACK)


# sandbox_active_key → assert 的沙箱 OS。模板键见 sandbox_configs._TEMPLATES
_ACTIVE_KEY_TO_TYPE = {key: t for t, (_fn, key) in sandbox_configs._TEMPLATES.items()}


def _derive_sandbox_type(active_key: Optional[str], config_type: Optional[str]) -> tuple:
    """沙箱 OS（linux/windows）。返回 (type, inferred)。

    优先级：快照的 sandbox_active_key（最贴近实际配置）→ 引用的沙箱配置类型
    （create_params.sandbox_config_id）→ 兜底 linux + inferred。
    """
    if active_key:
        if active_key in _ACTIVE_KEY_TO_TYPE:
            return _ACTIVE_KEY_TO_TYPE[active_key], False
        return ("windows" if "windows" in active_key.lower() else "linux"), True
    if config_type in ("linux", "windows"):
        return config_type, False
    return "linux", True


def _sandbox_resources(row: dict) -> dict:
    """实例的单沙箱资源 + 所在资源池（= sandbox_namespace）+ 沙箱 OS。"""
    snapshot = row.get("config_snapshot") or ""
    create_params = row.get("create_params") or ""
    # create_params 也参与缓存键：重选沙箱配置后 sandbox_type 会变，只 hash 快照会命中旧值。
    # 用 md5 而不是内置 hash()：后者带进程级随机盐，跨 worker 落盘的分片对不上。
    cache_key = (row.get("id"), _res_digest(snapshot), _res_digest(create_params))
    hit = _RES_CACHE.get(cache_key)
    if hit is None:
        # 本进程没算过：先看其它 worker 落盘的分片，能命中就不必自己解析 YAML
        _load_res_shards()
        hit = _RES_CACHE.get(cache_key)
    if hit is not None:
        _RES_CACHE.move_to_end(cache_key)
        return hit

    out = None
    active_key = None
    config_type = None
    if snapshot:
        try:
            cfg = OmegaConf.to_container(OmegaConf.create(snapshot), resolve=True) or {}
            out = _parse_resources(cfg)
            out["sandbox_namespace"] = cfg.get("sandbox_namespace") or ""
            active_key = sandbox_configs.active_sandbox_key(cfg)
        except Exception:
            out = None

    if out is None:
        # snapshot 缺失/损坏：回退到该实例引用的沙箱配置文件
        out = dict(_RES_FALLBACK)
        out["sandbox_namespace"] = ""
        try:
            params = json.loads(create_params or "{}")
            sb_id = params.get("sandbox_config_id")
            if sb_id:
                with get_connection() as conn:
                    sb_row = conn.execute(
                        "SELECT * FROM sandbox_configs WHERE id = ?", (sb_id,)
                    ).fetchone()
                if sb_row:
                    sb_row = dict(sb_row)
                    config_type = sb_row.get("sandbox_type")
                    summary = sandbox_configs._extract_summary(
                        sandbox_configs._load_yaml(sandbox_configs._file_for(sb_row)),
                        sb_row["sandbox_key"],
                    )
                    out = {
                        "cpu_request": _num(summary.get("cpu_request"), 2),
                        "cpu_limit": _num(summary.get("cpu_limit"), 3),
                        "memory_request": _gib(summary.get("memory_request") or "6Gi"),
                        "memory_limit": _gib(summary.get("memory_limit") or "8Gi"),
                        "sandbox_namespace": summary.get("sandbox_namespace") or "",
                    }
                    active_key = sb_row.get("sandbox_key")
        except Exception:
            pass

    # 历史实例（形态 B 的 config，或快照里没写 sandbox_namespace）归入默认池，
    # 否则它们的占用会落在所有池之外，看板上「已用 > 各池之和」。
    if not out.get("sandbox_namespace"):
        out["sandbox_namespace"] = resource_pools.default_namespace()

    sb_type, inferred = _derive_sandbox_type(active_key, config_type)
    out["sandbox_type"] = sb_type
    out["sandbox_type_inferred"] = inferred

    _RES_CACHE[cache_key] = out
    _RES_CACHE.move_to_end(cache_key)
    while len(_RES_CACHE) > _RES_CACHE_MAX:
        _RES_CACHE.popitem(last=False)
    _save_res_shard()
    return out


def _with_resources(d: dict) -> dict:
    d.update(_sandbox_resources(d))
    return d


def _describe_error(e: Exception) -> str:
    """把异常转成给用户看的一句话失败原因。

    HTTPException 的 str() 是 "500: 具体原因"，前缀是状态码，对用户没有意义；
    这里只取 detail，其余异常保留原始文本。
    """
    detail = getattr(e, "detail", None)
    text = detail if isinstance(detail, str) else str(e)
    return (text or e.__class__.__name__).strip()[:500]


@router.get("", response_model=list[InstanceInfo])
def list_instances(user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM task_instances ORDER BY created_at DESC").fetchall()
    return [InstanceInfo(**_with_resources(_with_harness_type(_sync_instance_status(dict(r))))) for r in rows]


@router.get("/{instance_id}", response_model=InstanceInfo)
def get_instance(instance_id: str, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")
    return InstanceInfo(**_with_resources(_with_harness_type(_sync_instance_status(dict(row)))))


async def _async_subprocess_run(cmd, *, cwd=None, timeout=600):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise HTTPException(status_code=500, detail=f"命令超时({timeout}s): {' '.join(cmd[:3])}")
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


@router.post("", response_model=InstanceInfo)
async def create_instance(req: InstanceCreate, user: dict = Depends(require_operator)):
    """创建实例：只生成配置文件并入库，不做 OBS 下载，秒级返回。"""
    timestamp = datetime.now().strftime("%y%m%d%H%M")
    short_id = uuid.uuid4().hex[:4]
    instance_id = f"{timestamp}-{req.task_name}-{short_id}"
    instance_dir = _get_instance_dir(instance_id)
    os.makedirs(instance_dir, exist_ok=True)

    # --- 1. 生成 config.yaml ---
    # 目录解析统一走 harness_configs.resolve_settings_dir：
    # 优先具体版本（必须已发布），仅传方案时取最新已发布版本，都没有则用扁平 settings/。
    harness_version_row = None
    harness_version_label = ""
    with get_connection() as conn:
        harness_settings_dir, harness_version_row, harness_version_label = \
            harness_configs.resolve_settings_dir(
                conn,
                req.harness_type,
                getattr(req, "harness_config_version_id", None),
                req.harness_config_id,
            )

    config_template = os.path.join(harness_settings_dir, "config.yaml")
    if not os.path.exists(config_template):
        config_template = settings.CONFIG_TEMPLATE
    template_path = config_template
    if not os.path.exists(template_path):
        raise HTTPException(status_code=500, detail=f"模板配置文件不存在: {template_path}")

    base = OmegaConf.load(template_path)
    base.sandbox_id_prefix = req.task_name
    base.run_config.harness_type = req.harness_type
    base.run_config.concurrent_num = req.concurrent_num
    base.run_config.start_index = req.start_index
    base.run_config.total_num = req.total_num
    # 任务级开关：hive 上传配置前给模型 key 追加 _<env_id uuid> 后缀。
    # 模板里没有该键，这里显式写入；关闭时写 False，语义与缺失一致。
    base.run_config.task.suffix_api_keys = bool(req.suffix_api_keys)

    # --- 沙箱配置：把派生文件的沙箱段整段替换进 config.yaml ---
    # 派生文件在 settings/sandbox/<type>_<id>.yaml，其 global/sandbox/sandbox_namespace/s3
    # 整段覆盖到 base 上。未选沙箱配置时只重排顺序，内容用模板原样。
    if req.sandbox_config_id:
        with get_connection() as conn:
            sb_row = conn.execute(
                "SELECT * FROM sandbox_configs WHERE id = ?", (req.sandbox_config_id,)
            ).fetchone()
        if not sb_row:
            raise HTTPException(status_code=400, detail=f"沙箱配置不存在: {req.sandbox_config_id}")
        sandbox_configs.apply_sandbox_to_config(base, dict(sb_row))
    else:
        # 未选沙箱配置：模板原样，只统一顶层顺序
        key = sandbox_configs.active_sandbox_key(base)
        if key:
            base.sandbox_active_key = key
        sandbox_configs.rewrite_config_sections(base)

    # --- 镜像：独立于沙箱配置，只写当前实例 ---
    # 无论有没有选沙箱配置，都按生效的 sandbox key 改写本实例 config.yaml，
    # 不动任何模板/派生文件（选沙箱配置时该 key 由派生文件决定，否则取模板自带的）。
    sandbox_configs.apply_image(base, req.image_name or "")

    if req.skill_dir:
        base.run_config.obs.skill_download_path = req.skill_dir
    if req.default_skill_dir:
        base.run_config.obs.default_skill_download_path = req.default_skill_dir
    if req.default_skills:
        base.run_config.obs.default_skills = [s.strip() for s in req.default_skills.split(",") if s.strip()]
    if req.agent_dir:
        base.run_config.obs.agents_download_path = req.agent_dir
    if req.user_config_dir:
        base.run_config.obs.user_config_download_path = req.user_config_dir
    if req.user_profile_dir:
        base.run_config.obs.user_profile_download_path = req.user_profile_dir
    effective_traj_save_path = (req.traj_save_path or "").strip()
    if not effective_traj_save_path:
        traj_prefix = harness_meta.traj_prefix(req.harness_type)
        effective_traj_save_path = f"{traj_prefix}/traj_{req.task_name}"
    base.run_config.obs.traj_save_path = effective_traj_save_path

    # harness md：从版本目录 md/ 拷到实例 workspace/，config 写实例内绝对路径。
    # 没有 md 的旧版本也要写空列表——下游读不到键和读到空列表是两回事。
    _md_src = os.path.join(harness_settings_dir, "md")
    _md_paths = []
    if os.path.isdir(_md_src):
        _ws_dir = os.path.join(instance_dir, "workspace")
        for _f in sorted(os.listdir(_md_src)):
            _fp = os.path.join(_md_src, _f)
            if _f.lower().endswith(".md") and os.path.isfile(_fp):
                os.makedirs(_ws_dir, exist_ok=True)
                shutil.copy2(_fp, os.path.join(_ws_dir, _f))
                _md_paths.append(os.path.join(_ws_dir, _f))
    base.run_config.harness_md = _md_paths

    # harness 配置文件路径: 由 harness_config.json 的 agent_config 派生。
    # 下方各 harness 配置生成分支仍按原变量名引用这些局部变量, 故在此集中定义。
    hermes_config_path = os.path.join(instance_dir, "hermes_config.yaml")
    cc_settings_path = os.path.join(instance_dir, "cc_settings.json")
    openjiuwen_path = os.path.join(instance_dir, "openjiuwen.json")
    opencode_path = os.path.join(instance_dir, "opencode.json")
    codex_path = os.path.join(instance_dir, "config.toml")
    pi_path = os.path.join(instance_dir, "models.json")
    grok_path = os.path.join(instance_dir, "grok_config.toml")
    dsh_path = os.path.join(instance_dir, "dsh_settings.yaml")
    user_proxy_path = os.path.join(instance_dir, "user_proxy_model.json")

    # harness_local_config_file: 直接用 agent_config 文件名拼路径, 兜底 openclaw.json
    _agent_cfg = harness_meta.agent_config(req.harness_type) or "openclaw.json"
    # jiuwenswarm 系: 实例内文件名跟随 agent_config(jw_dev_config.yaml / jw_xiaoyi_config.yaml),
    # 不要硬编码成 jw_config.yaml —— 旧标识 jiuwenswarm 的 agent_config 恰好就是它, 兼容。
    jw_config_path = os.path.join(instance_dir, _agent_cfg)
    openclaw_path = os.path.join(instance_dir, _agent_cfg)
    base.run_config.sandbox.harness_local_config_file = os.path.join(instance_dir, _agent_cfg)
    base.run_config.sandbox.user_proxy_model_local_file = user_proxy_path

    base.run_config.task.task_output_path = os.path.join(instance_dir, "outputs")
    base.run_config.task.task_download_path = os.path.join(instance_dir, "downloads")

    # 代码仓：只记录到配置，不做下载
    if req.code_repo_id:
        with get_connection() as conn:
            repo_row = conn.execute("SELECT * FROM code_repos WHERE id = ?", (req.code_repo_id,)).fetchone()
        if repo_row:
            repo = dict(repo_row)
            code_tar_dir = os.path.join(settings.HIVE_ROOT, "platform", "code", "tar", repo["name"], repo["version"])
            tar_path = os.path.join(code_tar_dir, "openclaw-task.tar")
            base.run_config.task.main_code_tar = tar_path
            base.run_config.task.main_code_dir = ""
            if repo.get("main_python_file"):
                base.run_config.task.main_python_file = repo["main_python_file"]

    config_path = os.path.join(instance_dir, "config.yaml")
    OmegaConf.save(base, config_path)

    # --- 2. 生成 harness 配置文件 ---
    if req.harness_type == "hermes":
        hermes_template = os.path.join(harness_settings_dir, "hermes_config.yaml")
        if not os.path.exists(hermes_template):
            hermes_template = os.path.join(settings.SETTINGS_DIR, "hermes_config.yaml")
        hermes_omega = OmegaConf.load(hermes_template)

        if req.model_id:
            hermes_omega.model.default = req.model_id
            hermes_omega.model.model = req.model_id
        if req.model_base_url:
            hermes_omega.model.base_url = req.model_base_url
        if req.model_api_key:
            hermes_omega.model.api_key = req.model_api_key

        OmegaConf.save(hermes_omega, hermes_config_path)
    elif req.harness_type == "claude-code":
        cc_template = os.path.join(harness_settings_dir, "cc_settings.json")
        if not os.path.exists(cc_template):
            cc_template = os.path.join(settings.SETTINGS_DIR, "cc_settings.json")
        with open(cc_template, "r", encoding="utf-8") as f:
            cc_cfg = json.load(f)

        if req.model_base_url:
            cc_cfg["env"]["ANTHROPIC_BASE_URL"] = req.model_base_url
        if req.model_api_key:
            cc_cfg["env"]["ANTHROPIC_AUTH_TOKEN"] = req.model_api_key
        if req.model_id:
            cc_cfg["env"]["ANTHROPIC_MODEL"] = req.model_id

        with open(cc_settings_path, "w", encoding="utf-8") as f:
            json.dump(cc_cfg, f, indent=2, ensure_ascii=False)
    elif req.harness_type == "openjiuwen":
        openjiuwen_template = os.path.join(harness_settings_dir, "openjiuwen.json")
        if not os.path.exists(openjiuwen_template):
            openjiuwen_template = os.path.join(settings.SETTINGS_DIR, "openjiuwen.json")
        with open(openjiuwen_template, "r", encoding="utf-8") as f:
            openjiuwen_cfg = json.load(f)

        openjiuwen_cfg.setdefault("default", {})
        if req.model_id:
            openjiuwen_cfg["default"]["model"] = req.model_id
        if req.model_base_url:
            openjiuwen_cfg["default"]["base_url"] = req.model_base_url
        if req.model_api_key:
            openjiuwen_cfg["default"]["api_key"] = req.model_api_key
        # provider 复用 model_api_type 传入；tokenfly 网关走 anthropic-messages，默认 Anthropic，不能回退 OpenAI
        openjiuwen_cfg["default"]["provider"] = req.model_api_type or openjiuwen_cfg["default"].get("provider") or "Anthropic"

        with open(openjiuwen_path, "w", encoding="utf-8") as f:
            json.dump(openjiuwen_cfg, f, indent=2, ensure_ascii=False)
    elif req.harness_type == "opencode":
        opencode_template = os.path.join(harness_settings_dir, "opencode.json")
        if not os.path.exists(opencode_template):
            opencode_template = os.path.join(settings.SETTINGS_DIR, "opencode.json")
        with open(opencode_template, "r", encoding="utf-8") as f:
            opencode_cfg = json.load(f)

        # opencode.json 结构: provider.<name>.{npm,options.{baseURL,apiKey},models} + agent.main.model
        # provider key 固定为 "local-provider"（与模板一致），npm 根据 API 类型选择:
        #   anthropic-messages -> @ai-sdk/anthropic
        #   openai-completions -> @ai-sdk/openai-compatible
        providers = opencode_cfg.setdefault("provider", {})
        main_provider_key = "local-provider"
        # 根据 model_api_type 决定 npm 包名；缺省时保留模板中的值
        if req.model_api_type == "anthropic-messages":
            npm_pkg = "@ai-sdk/anthropic"
        elif req.model_api_type == "openai-completions":
            npm_pkg = "@ai-sdk/openai-compatible"
        else:
            npm_pkg = None  # 保留模板原有值

        if main_provider_key not in providers:
            providers[main_provider_key] = {
                "name": main_provider_key,
                "npm": npm_pkg or "@ai-sdk/anthropic",
                "options": {"baseURL": "", "apiKey": ""},
                "models": {},
            }
        elif npm_pkg:
            providers[main_provider_key]["npm"] = npm_pkg

        main_opts = providers[main_provider_key].setdefault("options", {})
        if req.model_base_url:
            # opencode 要求 baseURL 带 /v1 后缀
            base_url = req.model_base_url.rstrip("/")
            if not base_url.endswith("/v1"):
                base_url += "/v1"
            main_opts["baseURL"] = base_url
        if req.model_api_key:
            main_opts["apiKey"] = req.model_api_key

        # model_id 注入: 写入 provider.models 并更新 agent.main.model
        if req.model_id:
            models_map = providers[main_provider_key].setdefault("models", {})
            models_map[req.model_id] = {"name": req.model_id}
            agents_section = opencode_cfg.setdefault("agent", {})
            main_agent = agents_section.setdefault("main", {})
            main_agent["model"] = f"{main_provider_key}/{req.model_id}"

        with open(opencode_path, "w", encoding="utf-8") as f:
            json.dump(opencode_cfg, f, indent=2, ensure_ascii=False)
    elif req.harness_type == "codex":
        # config.toml 结构:
        #   顶层: model / model_provider / model_reasoning_effort / disable_response_storage / sandbox_mode
        #   [model_providers.<name>]: name / base_url / experimental_bearer_token / wire_api
        import tomllib
        import tomli_w
        codex_template = os.path.join(harness_settings_dir, "config.toml")
        if not os.path.exists(codex_template):
            codex_template = os.path.join(settings.SETTINGS_DIR, "config.toml")
        with open(codex_template, "rb") as f:
            codex_cfg = tomllib.load(f)

        # 找到顶层 model_provider 指向的 provider 段(默认 "custom");不存在则自建
        main_provider_key = codex_cfg.get("model_provider") or "custom"
        providers = codex_cfg.setdefault("model_providers", {})
        prov = providers.setdefault(main_provider_key, {
            "name": main_provider_key,
            "base_url": "",
            "experimental_bearer_token": "",
            "wire_api": "responses",
        })

        if req.model_id:
            codex_cfg["model"] = req.model_id
        if req.model_base_url:
            # codex 要求 base_url 带 /v1 后缀
            base_url = req.model_base_url.rstrip("/")
            if not base_url.endswith("/v1"):
                base_url += "/v1"
            prov["base_url"] = base_url
        if req.model_api_key:
            prov["experimental_bearer_token"] = req.model_api_key

        with open(codex_path, "wb") as f:
            tomli_w.dump(codex_cfg, f)
    elif req.harness_type == "pi":
        # models.json 结构: providers.<name>.{baseUrl, api, apiKey, models: [{id, name, ...}]}
        pi_template = os.path.join(harness_settings_dir, "models.json")
        if not os.path.exists(pi_template):
            pi_template = os.path.join(settings.SETTINGS_DIR, "models.json")
        with open(pi_template, "r", encoding="utf-8") as f:
            pi_cfg = json.load(f)

        providers = pi_cfg.setdefault("providers", {})
        main_provider_key = "custom"
        if main_provider_key not in providers:
            providers[main_provider_key] = {
                "baseUrl": "",
                "api": "openai-completions",
                "apiKey": "",
                "models": [],
            }

        prov = providers[main_provider_key]
        if req.model_api_key:
            prov["apiKey"] = req.model_api_key
        if req.model_api_type:
            prov["api"] = req.model_api_type
        if req.model_base_url:
            # pi 要求 openai-completions 带 /v1 后缀, anthropic-messages 不带
            base_url = req.model_base_url.rstrip("/")
            if prov["api"] == "openai-completions" and not base_url.endswith("/v1"):
                base_url += "/v1"
            if prov["api"] == "anthropic-messages" and base_url.endswith("/v1"):
                base_url -= "v1"
            prov["baseUrl"] = base_url
        
        if req.model_id:
            models_list = prov.setdefault("models", [])
            if models_list:
                models_list[0]["id"] = req.model_id
                models_list[0]["name"] = req.model_id
            else:
                models_list.append({"id": req.model_id, "name": req.model_id})

        with open(pi_path, "w", encoding="utf-8") as f:
            json.dump(pi_cfg, f, indent=2, ensure_ascii=False)
    elif req.harness_type == "grok":
        import tomllib
        import tomli_w
        grok_template = os.path.join(harness_settings_dir, "grok_config.toml")
        if not os.path.exists(grok_template):
            grok_template = os.path.join(settings.SETTINGS_DIR, "grok_config.toml")
        with open(grok_template, "rb") as f:
            grok_cfg = tomllib.load(f)

        # 新版 grok_config.toml 将模型配置放在 [model.default-model] 下
        model_section = grok_cfg.setdefault("model", {}).setdefault("default-model", {})
        if req.model_id:
            model_section["model"] = req.model_id
            model_section["name"] = req.model_id
        if req.model_base_url:
            # grok 要求 base_url 带 /v1 后缀
            base_url = req.model_base_url.rstrip("/")
            if not base_url.endswith("/v1"):
                base_url += "/v1"
            model_section["base_url"] = base_url
        if req.model_api_key:
            model_section["api_key"] = req.model_api_key

        with open(grok_path, "wb") as f:
            tomli_w.dump(grok_cfg, f)
    elif req.harness_type == "dsh":
        # dsh_settings.yaml 结构
        dsh_template = os.path.join(harness_settings_dir, "dsh_settings.yaml")
        if not os.path.exists(dsh_template):
            dsh_template = os.path.join(settings.SETTINGS_DIR, "dsh_settings.yaml")
        dsh_omega = OmegaConf.load(dsh_template)

        # 顶层 provider key 由 agent-default-model.provider 指向（默认 custom）
        default_model = dsh_omega.get("agent-default-model") or OmegaConf.create({})
        main_provider_key = str(default_model.get("provider") or "custom")

        providers = dsh_omega.setdefault("llm-pi-ai", OmegaConf.create({})).setdefault("providers", OmegaConf.create({}))
        if main_provider_key not in providers:
            providers[main_provider_key] = OmegaConf.create({
                "displayName": main_provider_key,
                "apiKeyEnv": main_provider_key.replace("-", "_").upper() + "_API_KEY",
                "api": req.model_api_type,
                "baseURL": "",
                "models": [],
            })
        prov = providers[main_provider_key]
        api_key_env = str(prov.get("apiKeyEnv") or (main_provider_key.replace("-", "_").upper() + "_API_KEY"))

        if req.model_api_type:
            prov["api"] = req.model_api_type
        if req.model_base_url:
            # dsh 要求 openai-completions 带 /v1 后缀, anthropic-messages 不带
            base_url = req.model_base_url.rstrip("/")
            if prov["api"] == "openai-completions" and not base_url.endswith("/v1"):
                base_url += "/v1"
            if prov["api"] == "anthropic-messages" and base_url.endswith("/v1"):
                base_url -= "v1"
            prov["baseURL"] = base_url
        if req.model_id:
            models_list = prov.get("models")
            if models_list and len(models_list) > 0:
                models_list[0]["id"] = req.model_id
            else:
                prov["models"] = [OmegaConf.create({"id": req.model_id})]
            # 同步 agent-default-model.model
            dsh_omega.setdefault("agent-default-model", OmegaConf.create({}))["model"] = req.model_id
            dsh_omega["agent-default-model"].setdefault("provider", main_provider_key)

        if req.model_api_key:
            credentials = dsh_omega.setdefault("credentials", OmegaConf.create({}))
            credentials[api_key_env] = req.model_api_key

        OmegaConf.save(dsh_omega, dsh_path)
    elif req.harness_type in JIUWENSWARM_TYPES:
        # jw 系配置: 模型真源是 models.defaults 列表, 默认模型由 is_default: true 标记。
        # 注意该文件大量使用 ${VAR:-default} 占位符(jiuwenswarm 自己的 env 模板语法),
        # OmegaConf 会当 interpolation 解析而抛 UnsupportedInterpolationType,
        # 故用 pyyaml 原文往返(与 prepare_config.py 的 jiuwenswarm 分支一致)。
        # 模板名 = agent_config(jw_dev_config.yaml / jw_xiaoyi_config.yaml / jw_config.yaml),
        # 版本目录 → 扁平 settings → .example 逐级兜底。
        _jw_name = _agent_cfg
        jw_template = os.path.join(harness_settings_dir, _jw_name)
        if not os.path.exists(jw_template):
            jw_template = os.path.join(settings.SETTINGS_DIR, _jw_name)
        if not os.path.exists(jw_template):
            jw_template = os.path.join(settings.SETTINGS_DIR, _jw_name + ".example")
        if not os.path.exists(jw_template):
            raise HTTPException(
                status_code=500,
                detail=f"jiuwenswarm 配置模板不存在: {_jw_name}(也未找到 .example)",
            )
        import yaml
        with open(jw_template, "r", encoding="utf-8") as f:
            jw_cfg = yaml.safe_load(f) or {}

        defaults = jw_cfg.setdefault("models", {}).setdefault("defaults", [])
        if not isinstance(defaults, list):
            defaults = [defaults]
            jw_cfg["models"]["defaults"] = defaults
        # 主模型条目: 优先 is_default: true, 否则取第一条, 列表为空则新建
        default_entry = next(
            (e for e in defaults if isinstance(e, dict) and e.get("is_default")), None
        )
        if default_entry is None:
            default_entry = defaults[0] if defaults and isinstance(defaults[0], dict) else None
        if default_entry is None:
            default_entry = {"is_default": True, "model_client_config": {}}
            defaults.insert(0, default_entry)

        mcc = default_entry.setdefault("model_client_config", {})

        if req.model_id:
            mcc["model_name"] = req.model_id
        if req.model_api_key:
            mcc["api_key"] = req.model_api_key
        if req.model_api_type:
            # 透传语义: anthropic-messages → Anthropic, openai-completions → OpenAI
            # (与 prepare_config.py 的 client_provider 派生规则保持一致)
            mcc["client_provider"] = (
                "Anthropic" if "anthropic" in req.model_api_type.lower() else "OpenAI"
            )
        if req.model_base_url:
            # jiuwenswarm 按 client_provider 归一化: openai 补 /v1, anthropic 去 /v1
            base_url = req.model_base_url.rstrip("/")
            if str(mcc.get("client_provider") or "").lower() == "anthropic":
                if base_url.endswith("/v1"):
                    base_url = base_url[:-3]
            elif not base_url.endswith("/v1"):
                base_url += "/v1"
            mcc["api_base"] = base_url

        with open(jw_config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(jw_cfg, f, allow_unicode=True, sort_keys=False)
    else:
        # openclaw 与 openclaw-tui 使用相同原生 schema，但模板文件彼此独立。
        openclaw_template = os.path.join(harness_settings_dir, _agent_cfg)
        if not os.path.exists(openclaw_template):
            openclaw_template = os.path.join(settings.SETTINGS_DIR, _agent_cfg)
        with open(openclaw_template, "r", encoding="utf-8") as f:
            openclaw_cfg = json.load(f)

        if req.model_api_key:
            openclaw_cfg["models"]["providers"]["local"]["apiKey"] = req.model_api_key
        if req.model_base_url:
            openclaw_cfg["models"]["providers"]["local"]["baseUrl"] = req.model_base_url
        if req.model_api_type:
            openclaw_cfg["models"]["providers"]["local"]["api"] = req.model_api_type
            models_list = openclaw_cfg["models"]["providers"]["local"]["models"]
            if models_list:
                models_list[0]["api"] = req.model_api_type
        if req.model_id:
            models_list = openclaw_cfg["models"]["providers"]["local"]["models"]
            if models_list:
                models_list[0]["id"] = req.model_id
                models_list[0]["name"] = req.model_id
            openclaw_cfg["agents"]["defaults"]["model"]["primary"] = f"local/{req.model_id}"
            openclaw_cfg["agents"]["defaults"]["models"] = {f"local/{req.model_id}": {}}

        with open(openclaw_path, "w", encoding="utf-8") as f:
            json.dump(openclaw_cfg, f, indent=2, ensure_ascii=False)

    # --- 3. 自动申请 API Key (invite_code) ---
    # base_url 可来自 req.model_base_url，或回退到 key_gen.yaml 的默认配置
    if req.invite_code and not req.model_api_key:
        try:
            key_name = f"{user['username']}_{datetime.now().strftime('%y%m%d%H%M')}_{req.name}"
            req.model_api_key = await _request_api_key(req.model_base_url or "", req.invite_code, key_name)
            if not req.model_api_key:
                raise ValueError("申请接口未返回 api_key，请检查 key_gen.yaml 配置与响应字段")
            if req.harness_type == "hermes" and os.path.exists(hermes_config_path):
                hermes_omega = OmegaConf.load(hermes_config_path)
                hermes_omega.model.api_key = req.model_api_key
                OmegaConf.save(hermes_omega, hermes_config_path)
            elif req.harness_type == "claude-code" and os.path.exists(cc_settings_path):
                with open(cc_settings_path, "r", encoding="utf-8") as f:
                    cc_cfg = json.load(f)
                cc_cfg["env"]["ANTHROPIC_AUTH_TOKEN"] = req.model_api_key
                with open(cc_settings_path, "w", encoding="utf-8") as f:
                    json.dump(cc_cfg, f, indent=2, ensure_ascii=False)
            elif req.harness_type == "openjiuwen" and os.path.exists(openjiuwen_path):
                with open(openjiuwen_path, "r", encoding="utf-8") as f:
                    openjiuwen_cfg = json.load(f)
                openjiuwen_cfg.setdefault("default", {})["api_key"] = req.model_api_key
                with open(openjiuwen_path, "w", encoding="utf-8") as f:
                    json.dump(openjiuwen_cfg, f, indent=2, ensure_ascii=False)
            elif req.harness_type == "opencode" and os.path.exists(opencode_path):
                with open(opencode_path, "r", encoding="utf-8") as f:
                    opencode_cfg = json.load(f)
                opencode_cfg.setdefault("provider", {}).setdefault("local-provider", {}).setdefault("options", {})["apiKey"] = req.model_api_key
                with open(opencode_path, "w", encoding="utf-8") as f:
                    json.dump(opencode_cfg, f, indent=2, ensure_ascii=False)
            elif req.harness_type == "codex" and os.path.exists(codex_path):
                # codex config.toml: 用 tomllib(读) + tomli_w(写) 正式解析。
                # 与前面 codex 分支保持一致,避免正则替换在多段/嵌套 table 上的边界问题。
                import tomllib
                import tomli_w
                with open(codex_path, "rb") as f:
                    codex_cfg = tomllib.load(f)
                main_provider_key = codex_cfg.get("model_provider") or "custom"
                prov = codex_cfg.setdefault("model_providers", {}).setdefault(
                    main_provider_key,
                    {"name": main_provider_key, "base_url": "",
                     "experimental_bearer_token": "", "wire_api": "responses"},
                )
                prov["experimental_bearer_token"] = req.model_api_key
                with open(codex_path, "wb") as f:
                    tomli_w.dump(codex_cfg, f)
            elif req.harness_type == "pi" and os.path.exists(pi_path):
                with open(pi_path, "r", encoding="utf-8") as f:
                    pi_cfg = json.load(f)
                pi_cfg.setdefault("providers", {}).setdefault("custom", {})["apiKey"] = req.model_api_key
                with open(pi_path, "w", encoding="utf-8") as f:
                    json.dump(pi_cfg, f, indent=2, ensure_ascii=False)
            elif req.harness_type == "grok" and os.path.exists(grok_path):
                import tomllib
                import tomli_w
                with open(grok_path, "rb") as f:
                    grok_cfg = tomllib.load(f)
                grok_cfg.setdefault("model", {}).setdefault("default-model", {})["api_key"] = req.model_api_key
                with open(grok_path, "wb") as f:
                    tomli_w.dump(grok_cfg, f)
            elif req.harness_type == "dsh" and os.path.exists(dsh_path):
                dsh_omega = OmegaConf.load(dsh_path)
                default_model = dsh_omega.get("agent-default-model") or OmegaConf.create({})
                main_provider_key = str(default_model.get("provider") or "custom")
                providers = dsh_omega.setdefault("llm-pi-ai", OmegaConf.create({})).setdefault("providers", OmegaConf.create({}))
                prov = providers.get(main_provider_key)
                api_key_env = str((prov and prov.get("apiKeyEnv")) or (main_provider_key.replace("-", "_").upper() + "_API_KEY"))
                credentials = dsh_omega.setdefault("credentials", OmegaConf.create({}))
                credentials[api_key_env] = req.model_api_key
                OmegaConf.save(dsh_omega, dsh_path)
            elif req.harness_type in JIUWENSWARM_TYPES and os.path.exists(jw_config_path):
                import yaml
                with open(jw_config_path, "r", encoding="utf-8") as f:
                    jw_cfg = yaml.safe_load(f) or {}
                defaults = jw_cfg.setdefault("models", {}).setdefault("defaults", [])
                if not isinstance(defaults, list):
                    defaults = [defaults]
                    jw_cfg["models"]["defaults"] = defaults
                default_entry = next(
                    (e for e in defaults if isinstance(e, dict) and e.get("is_default")), None
                )
                if default_entry is None and defaults and isinstance(defaults[0], dict):
                    default_entry = defaults[0]
                if default_entry is None:
                    default_entry = {"is_default": True, "model_client_config": {}}
                    defaults.insert(0, default_entry)
                default_entry.setdefault("model_client_config", {})["api_key"] = req.model_api_key
                with open(jw_config_path, "w", encoding="utf-8") as f:
                    yaml.safe_dump(jw_cfg, f, allow_unicode=True, sort_keys=False)
            elif os.path.exists(openclaw_path):
                with open(openclaw_path, "r", encoding="utf-8") as f:
                    openclaw_cfg = json.load(f)
                openclaw_cfg["models"]["providers"]["local"]["apiKey"] = req.model_api_key
                with open(openclaw_path, "w", encoding="utf-8") as f:
                    json.dump(openclaw_cfg, f, indent=2, ensure_ascii=False)
        except Exception as e:
            # 申请失败不阻断实例创建，但记录原因，避免像以前那样被静默吞掉
            print(f"[api_key] 自动申请失败 (instance name={req.name}): {e}")

    # --- 4. 生成 user_proxy_model.json ---
    user_proxy_template = os.path.join(harness_settings_dir, "user_proxy_model.json")
    if not os.path.exists(user_proxy_template):
        user_proxy_template = os.path.join(settings.SETTINGS_DIR, "user_proxy_model.json")
    with open(user_proxy_template, "r", encoding="utf-8") as f:
        user_proxy_cfg = json.load(f)

    if req.agents:
        user_proxy_cfg = {}
        for ag in req.agents:
            entry = {}
            if ag.model:
                entry["model"] = ag.model
            if ag.provider:
                entry["provider"] = ag.provider
            if ag.base_url:
                entry["base_url"] = ag.base_url
            if ag.api_key:
                entry["api_key"] = ag.api_key
            if ag.api:
                entry["api"] = ag.api
            user_proxy_cfg[ag.name] = entry
    elif req.user_proxy_model_name or req.user_proxy_api_key or req.user_proxy_base_url:
        sim = user_proxy_cfg.get("user_simulator", {})
        if req.user_proxy_model_name:
            sim["model"] = req.user_proxy_model_name
        if req.user_proxy_api_key:
            sim["api_key"] = req.user_proxy_api_key
        if req.user_proxy_base_url:
            sim["base_url"] = req.user_proxy_base_url
        user_proxy_cfg["user_simulator"] = sim

    with open(user_proxy_path, "w", encoding="utf-8") as f:
        json.dump(user_proxy_cfg, f, indent=2, ensure_ascii=False)

    # --- 5. 入库（不统计任务数，留到启动时统计） ---
    # 队列支持：如果指定了 queue_id，计算 queue_position
    queue_position = 0
    if req.queue_id:
        # 验证队列是否存在
        with get_connection() as conn:
            queue_exists = conn.execute(
                "SELECT id FROM task_queues WHERE id = ?", (req.queue_id,)
            ).fetchone()

        if not queue_exists:
            raise HTTPException(status_code=400, detail=f"队列 '{req.queue_id}' 不存在")

        # 查询队列中最大 queue_position
        with get_connection() as conn:
            max_pos = conn.execute(
                "SELECT MAX(queue_position) as max_pos FROM task_instances WHERE queue_id = ?",
                (req.queue_id,)
            ).fetchone()

        queue_position = (max_pos['max_pos'] or 0) + 1

    with get_connection() as conn:
        # create_params 里补上解析出的方案/版本标识，详情页可直接展示，
        # 不必依赖后来可能被删掉的版本行。
        _create_params = req.model_dump()
        # 用户留空时 traj_save_path 由后端生成；create_params 必须保存最终生效值，
        # 否则详情页/输出页只会拿到请求中的空值，导致轨迹路径不展示。
        _create_params["traj_save_path"] = effective_traj_save_path
        _create_params["traj_save_bucket"] = str(
            base.run_config.obs.get("traj_save_bucket") or ""
        )
        if harness_version_row:
            _create_params["harness_config_version_id"] = harness_version_row["id"]
            _create_params["harness_config_version"] = harness_version_row["version"]
        _create_params["harness_config_label"] = harness_version_label
        conn.execute(
            """INSERT INTO task_instances
               (id, name, config_path, status, created_by, total_tasks, concurrent_num,
                config_snapshot, create_params, created_at, queue_id, queue_position,
                suffix_api_keys)
               VALUES (?, ?, ?, 'created', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (instance_id, req.name, config_path, user["username"], 0,
             req.concurrent_num, OmegaConf.to_yaml(base),
             json.dumps(_create_params, ensure_ascii=False),
             datetime.now().isoformat(), req.queue_id, queue_position,
             1 if req.suffix_api_keys else 0),
        )
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()

    # --- 6. 重跑失败: 搬源实例 complete.jsonl 到新实例 output 目录 ---
    if req.copy_complete_from:
        with get_connection() as conn:
            src_row = conn.execute(
                "SELECT config_path FROM task_instances WHERE id=?", (req.copy_complete_from,)
            ).fetchone()
        if src_row:
            src_output_dir = _get_output_dir(src_row["config_path"])
            src_complete = os.path.join(src_output_dir, "complete.jsonl")
            if os.path.exists(src_complete):
                new_output_dir = _get_output_dir(config_path)
                os.makedirs(new_output_dir, exist_ok=True)
                shutil.copy2(src_complete, os.path.join(new_output_dir, "complete.jsonl"))

    return InstanceInfo(**dict(row))


# ============================================================================
# 配置查看
# ============================================================================

@router.get("/{instance_id}/configs")
def list_instance_configs(instance_id: str, user: dict = Depends(get_current_user)):
    instance_dir = _get_instance_dir(instance_id)
    if not os.path.isdir(instance_dir):
        raise HTTPException(status_code=404, detail="实例目录不存在")

    files = []
    for name in ALLOWED_CONFIG_FILES:
        fpath = os.path.join(instance_dir, name)
        if os.path.exists(fpath):
            files.append({
                "name": name,
                "size": os.path.getsize(fpath),
                "modified_at": datetime.fromtimestamp(os.path.getmtime(fpath)).isoformat(),
            })
    return {"instance_id": instance_id, "files": files}


@router.get("/{instance_id}/configs/{filename}")
def get_instance_config(instance_id: str, filename: str, user: dict = Depends(get_current_user)):
    if filename not in ALLOWED_CONFIG_FILES:
        raise HTTPException(status_code=400, detail=f"不允许访问的文件: {filename}")

    fpath = os.path.join(_get_instance_dir(instance_id), filename)
    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail="配置文件不存在")

    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()

    file_type = "yaml" if filename.endswith(".yaml") else "json"
    return {"filename": filename, "type": file_type, "content": content}


@router.get("/{instance_id}/create-params")
def get_create_params(instance_id: str, user: dict = Depends(get_current_user)):
    """详情页「配置信息 → 创建参数」卡片。

    组装逻辑与任务导出共用 api.services.create_params.build_create_params，
    保证「详情页看到什么，导出就是什么」。
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT create_params, config_snapshot, suffix_api_keys "
            "FROM task_instances WHERE id=?",
            (instance_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")
    if not row["create_params"]:
        raise HTTPException(status_code=404, detail="该实例无创建参数记录")
    params, _ = build_create_params(dict(row))
    return params


# ============================================================================
# 启动准备：OBS 下载、打包、任务数统计
# ============================================================================


async def _prepare_instance(instance_id: str, inst: dict):
    """启动前的准备工作：下载用户配置、下载代码仓、打包、统计任务数。"""
    config_path = inst["config_path"]
    instance_dir = str(Path(config_path).parent)
    base = OmegaConf.load(config_path)

    create_params = json.loads(inst["create_params"]) if inst.get("create_params") else {}

    # --- 下载用户配置（全局缓存，相同路径不重复下载） ---
    user_config_dir = create_params.get("user_config_dir", "")
    if user_config_dir:
        # 使用全局共享目录缓存 OBS 下载，路径作为 key
        cache_key_path = user_config_dir.strip("/").replace("/", "__")
        global_configs_cache = os.path.join(settings.HIVE_ROOT, "platform", "downloads", "configs", cache_key_path)
        configs_dir = os.path.join(instance_dir, "configs")

        if os.path.isdir(global_configs_cache) and os.listdir(global_configs_cache):
            # 已有缓存，直接软链接到实例目录
            if os.path.exists(configs_dir):
                if os.path.islink(configs_dir):
                    os.unlink(configs_dir)
                else:
                    shutil.rmtree(configs_dir)
            os.symlink(global_configs_cache, configs_dir)
        else:
            # 首次下载到全局缓存目录
            os.makedirs(global_configs_cache, exist_ok=True)
            obs_src = f"{base.s3.bucket_name}/{user_config_dir}"
            if not obs_src.endswith("/"):
                obs_src += "/"
            returncode, stdout, stderr = await _async_subprocess_run(
                [settings.OBSUTIL_PATH, "cp", obs_src, global_configs_cache, "-r", "-f"],
                timeout=600,
            )
            if returncode != 0:
                shutil.rmtree(global_configs_cache, ignore_errors=True)
                # obsutil 把「NoSuchBucket」这类诊断信息打在 stdout 上，stderr 往往为空。
                # 只截 stderr 会让用户看到一句没有下文的「OBS下载用户配置失败: 」。
                detail = (stderr or stdout or "").strip()[:500]
                raise HTTPException(
                    status_code=500,
                    detail=f"OBS下载用户配置失败（{obs_src}，exit={returncode}）: {detail}",
                )

            # 软链接到实例目录
            if os.path.exists(configs_dir):
                if os.path.islink(configs_dir):
                    os.unlink(configs_dir)
                else:
                    shutil.rmtree(configs_dir)
            os.symlink(global_configs_cache, configs_dir)

        # 找到实际目录（穿透单层嵌套）
        actual_dir = configs_dir
        while True:
            entries = os.listdir(actual_dir)
            if len(entries) == 1 and os.path.isdir(os.path.join(actual_dir, entries[0])):
                actual_dir = os.path.join(actual_dir, entries[0])
            else:
                break

        # 更新配置中的 task_input_path
        base.run_config.task.task_input_path = actual_dir
        base.run_config.obs.user_config_download_path = ""
        OmegaConf.save(base, config_path)

    # --- 下载代码仓并打包 ---
    code_repo_id = create_params.get("code_repo_id")
    if code_repo_id:
        with get_connection() as conn:
            repo_row = conn.execute("SELECT * FROM code_repos WHERE id = ?", (code_repo_id,)).fetchone()
        if repo_row:
            repo = dict(repo_row)
            code_src_dir = os.path.join(settings.HIVE_ROOT, "platform", "code", "src", repo["name"], repo["version"])
            code_tar_dir = os.path.join(settings.HIVE_ROOT, "platform", "code", "tar", repo["name"], repo["version"])
            tar_path = os.path.join(code_tar_dir, "openclaw-task.tar")

            if not os.path.isfile(tar_path):
                if not (os.path.isdir(code_src_dir) and os.listdir(code_src_dir)):
                    os.makedirs(code_src_dir, exist_ok=True)
                    obs_src = repo["obs_path"]
                    if not obs_src.endswith("/"):
                        obs_src += "/"
                    returncode, _, stderr = await _async_subprocess_run(
                        [settings.OBSUTIL_PATH, "cp", obs_src, code_src_dir, "-r", "-f"],
                        timeout=600,
                    )
                    if returncode != 0:
                        raise HTTPException(status_code=500, detail=f"代码仓下载失败: {stderr[:500]}")

                obs_dir_name = repo["obs_path"].rstrip("/").split("/")[-1]
                os.makedirs(code_tar_dir, exist_ok=True)
                returncode, _, stderr = await _async_subprocess_run(
                    ["tar", "cf", tar_path, obs_dir_name],
                    cwd=code_src_dir,
                    timeout=120,
                )
                if returncode != 0:
                    raise HTTPException(status_code=500, detail=f"打包失败: {stderr[:500]}")

    # --- 统计任务数 ---
    base = OmegaConf.load(config_path)
    task_input = str(base.run_config.task.task_input_path)
    total_tasks = 0
    if os.path.isdir(task_input):
        file_count = len([f for f in os.listdir(task_input) if os.path.isfile(os.path.join(task_input, f))])
        start_index = int(base.run_config.start_index) if hasattr(base.run_config, 'start_index') else 0
        total_num = int(base.run_config.total_num) if hasattr(base.run_config, 'total_num') else 0
        actual_start = min(start_index, file_count)
        available = file_count - actual_start
        total_tasks = min(total_num, available) if total_num > 0 else available

    with get_connection() as conn:
        conn.execute(
            "UPDATE task_instances SET total_tasks=? WHERE id=?",
            (total_tasks, instance_id),
        )

    return total_tasks


# ============================================================================
# 启动 / 停止 / 重跑
# ============================================================================

@router.post("/{instance_id}/start")
async def start_instance(instance_id: str, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")

    inst = dict(row)
    if inst["status"] == "running" and _is_pid_running(inst.get("pid")):
        raise HTTPException(status_code=400, detail="实例正在运行中")
    if inst["status"] == "preparing":
        raise HTTPException(status_code=400, detail="实例正在准备中，请稍候")

    # 队列支持：如果任务在队列中，判断是否可以立即运行
    queue_id = inst.get("queue_id")
    if queue_id:
        # 「启动」= 重新加入队列排队：排到队尾。停止后再启动若沿用旧的 queue_position，
        # 会插回原来的位置，而不是按设计图「重新加入队列排队」。
        # 重排与抢占放在同一事务里，避免并发启动拿到相同的 position。
        with get_connection() as conn:
            max_pos = conn.execute(
                "SELECT MAX(queue_position) AS m FROM task_instances WHERE queue_id = ?",
                (queue_id,)
            ).fetchone()
            new_pos = (max_pos["m"] or 0) + 1
            conn.execute(
                "UPDATE task_instances SET queue_position=? WHERE id=?",
                (new_pos, instance_id)
            )

            # 原子抢占：把「队列是否有可用槽位」写成 UPDATE 的 WHERE 条件。
            # 条件本身由 queue_scheduler.slot_condition_sql 生成，与自动调度完全同源，
            # 避免两处规则分叉。拆成「查占用 → 判断 → UPDATE」时，两个并发请求会同时通过检查。
            claimed = conn.execute(
                f"""UPDATE task_instances
                       SET status='preparing', pid=NULL, started_at=NULL,
                           error_summary=NULL, prepare_started_at=datetime('now')
                     WHERE id=?
                       AND {_slot_condition_sql(_OCCUPYING_PLACEHOLDERS)}
                 RETURNING id""",
                (instance_id, *_slot_condition_params())
            ).fetchone()
        inst["queue_position"] = new_pos
        _status_cache.pop(instance_id, None)

        if not claimed:
            # 队列槽位被占用，进入排队状态
            with get_connection() as conn:
                conn.execute(
                    "UPDATE task_instances SET status='queued' WHERE id=?",
                    (instance_id,)
                )
            _status_cache.pop(instance_id, None)

            queue_position = inst.get("queue_position", 0)
            return {
                "message": f"任务已加入队列（位置: {queue_position}），等待前方任务完成",
                "status": "queued",
                "queue_position": queue_position
            }

        asyncio.create_task(_prepare_and_launch(instance_id, inst))
        return {"message": "实例准备中，正在后台下载配置并启动", "status": "preparing"}

    # 无队列或队列空闲，可以直接启动
    # 标记为 preparing 状态。
    # 同时清空 pid/started_at：recover_orphan_preparing 用「两者都为 NULL」识别
    # 「准备中后端重启」的孤儿实例。若沿用上一次运行留下的 started_at（非 NULL），
    # 重启后该实例不会被回收，会永久卡在 preparing 堵死整条队列。
    # 一并清空 error_summary：上一次失败的说明不该挂在这次重试上。
    with get_connection() as conn:
        conn.execute(
            "UPDATE task_instances SET status='preparing', pid=NULL, started_at=NULL, "
            "error_summary=NULL, prepare_started_at=datetime('now') WHERE id=?",
            (instance_id,),
        )
    _status_cache.pop(instance_id, None)

    # 准备（OBS下载、打包、统计任务数）+ 启动，全部放到后台执行，
    # 立即返回，避免大目录下载/处理长时间占用 worker 阻塞其他请求。
    asyncio.create_task(_prepare_and_launch(instance_id, inst))

    return {"message": "实例准备中，正在后台下载配置并启动", "status": "preparing"}


def _launch_mock_task(output_dir: str, log_file: str):
    """空跑任务（MOCK_TASK_RUN=1 时替代真实 hive.py）。

    刻意用「真实子进程」而不是进程内 sleep：这样 pid 是真的、stop 的 killpg 是真的、
    完成检测也是真的（写真的 complete.jsonl 让后台 refresher 自己发现），
    于是 pid 记录 / 停止 / 完成判定 / 队列续跑全部走生产路径，只有任务本身是空的。

    用 flush 保证 python 自己 sleep，父进程退出后继续跑。
    """
    seconds = max(1, int(settings.MOCK_TASK_SECONDS))
    complete_file = os.path.join(output_dir, "complete.jsonl")
    script = (
        "import time, os, sys\n"
        f"sys.stdout.reconfigure(line_buffering=True)\n"
        f"print('[MOCK] 空跑 {seconds}s -> {complete_file}')\n"
        f"time.sleep({seconds})\n"
        f"with open({complete_file!r}, 'a') as f:\n"
        f"    f.write('{{\"mock\": true}}\\n')\n"
        f"print('[MOCK] 已写入 complete.jsonl')\n"
    )
    with open(log_file, "a") as lf:
        return subprocess.Popen(
            ["python", "-c", script],
            stdout=lf, stderr=subprocess.STDOUT,
            start_new_session=True,
        )


async def _prepare_and_launch(instance_id: str, inst: dict):
    """后台执行准备与启动。准备失败时置 failed（区别于用户未启动的 created）。"""
    try:
        await _prepare_instance(instance_id, inst)
    except Exception as e:
        with get_connection() as conn:
            # 只在仍处于 preparing 时回退。准备期间用户可能已点「停止」，
            # 无条件改写会把 stopped 覆盖掉，导致 stop 看起来无效。
            #
            # 失败置为 failed 而不是 created：created 表示「用户还没启动」，
            # 两者混在一起会让用户点了启动、看到状态变回「待启动」，却完全
            # 不知道是配置错误（如 OBS 路径不存在），只能反复点、反复失败。
            conn.execute(
                "UPDATE task_instances SET status='failed', error_summary=? "
                "WHERE id=? AND status='preparing'",
                (_describe_error(e), instance_id),
            )
        _status_cache.pop(instance_id, None)
        print(f"[start] 实例 {instance_id} 准备失败: {e}")
        # 队列任务准备失败会卡住整个队列，调度下一个排队任务
        queue_id = inst.get("queue_id")
        if queue_id:
            asyncio.create_task(schedule_queue(queue_id))
        return

    # 准备期间可能已被用户停止/删除：此时这次启动已过期，不应再把状态写成 running。
    with get_connection() as conn:
        cur_row = conn.execute(
            "SELECT status FROM task_instances WHERE id=?", (instance_id,)
        ).fetchone()
    if not cur_row or cur_row["status"] != "preparing":
        print(f"[start] 实例 {instance_id} 准备完成后状态已变为 "
              f"{cur_row['status'] if cur_row else 'deleted'}，放弃启动")
        return

    # 启动 hive
    try:
        config_path = inst["config_path"]
        output_dir = _get_output_dir(config_path)
        os.makedirs(output_dir, exist_ok=True)
        log_file = os.path.join(output_dir, "nohup.log")
        clean_log_file = os.path.join(output_dir, "nohup_clean.log")

        env = os.environ.copy()
        env["RLXF_CLEAN_LOG_PATH"] = clean_log_file

        if settings.MOCK_TASK_RUN:
            proc = _launch_mock_task(output_dir, log_file)
        else:
            create_params = json.loads(inst["create_params"]) if inst.get("create_params") else {}
            model_api_key = (create_params.get("model_api_key") or "").strip()
            hive_cmd = ["python", os.path.join(settings.HIVE_ROOT, "hive.py"), "--config", config_path]
            if model_api_key:
                hive_cmd += ["--model-api-key", model_api_key]
            with open(log_file, "a") as lf:
                proc = subprocess.Popen(
                    hive_cmd,
                    stdout=lf, stderr=lf,
                    cwd=settings.HIVE_ROOT,
                    env=env,
                    start_new_session=True,
                )

        with get_connection() as conn:
            # 带状态保护：准备期间若用户点了「停止」，此刻状态已是 stopped，
            # 这次启动作废 —— 不能把它写回 running（否则 stop 无效，且会与
            # 已放行的下一个任务同时占队列槽位，破坏串行）。
            cur = conn.execute(
                "UPDATE task_instances SET status='running', pid=?, started_at=? "
                "WHERE id=? AND status='preparing'",
                (proc.pid, datetime.now().isoformat(), instance_id),
            )
        if cur.rowcount == 0:
            # 启动已过期：把刚拉起的进程组杀掉，避免留下孤儿进程继续跑
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            _status_cache.pop(instance_id, None)
            print(f"[start] 实例 {instance_id} 启动已过期（已被停止），已终止进程 {proc.pid}")
            return
        _status_cache.pop(instance_id, None)

        # 任务启动成功后，触发队列调度（如果在队列中）
        queue_id = inst.get("queue_id")
        if queue_id:
            asyncio.create_task(schedule_queue(queue_id))
    except Exception as e:
        with get_connection() as conn:
            conn.execute(
                "UPDATE task_instances SET status='failed', error_summary=? "
                "WHERE id=? AND status='preparing'",
                (_describe_error(e), instance_id),
            )
        _status_cache.pop(instance_id, None)
        print(f"[start] 实例 {instance_id} 启动失败: {e}")
        # 同上：队列任务启动失败也要放行后面的排队任务
        queue_id = inst.get("queue_id")
        if queue_id:
            asyncio.create_task(schedule_queue(queue_id))


@router.post("/{instance_id}/stop")
async def stop_instance(instance_id: str, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")

    inst = dict(row)
    pid = inst.get("pid")
    if pid and _is_pid_running(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass

    config_path = inst["config_path"]
    subprocess.Popen(
        ["python", os.path.join(settings.HIVE_ROOT, "run_clear.py"), "--config", config_path],
        cwd=settings.HIVE_ROOT,
        start_new_session=True,
    )

    _status_cache.pop(instance_id, None)

    # 停止的语义（设计要求）：退出当前队列，不再参与排队。
    # 保留 queue_id 作为「队列归属」（用户不必重新选队列），但 stopped 状态既不占用
    # 槽位、也不作为排队候选，因此不会再挡后面的任务；下一次「启动」时重新排到队尾。
    #
    # 带状态条件：本请求读到的是快照，期间 refresher 可能已把实例判成 completed/finished
    # （例如进程恰好在此刻退出）。不加条件会把终态改回 stopped，并触发一次多余的调度。
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE task_instances SET status='stopped', stopped_at=?, pid=NULL, prepare_started_at=NULL "
            "WHERE id=? AND status NOT IN ('completed','finished')",
            (datetime.now().isoformat(), instance_id),
        )
        stopped = cur.rowcount > 0

    # 任务停止后，触发队列调度：它腾出了槽位，让下一个排队任务继续
    # （即使本次未改写状态，也调度一次——若实例刚自然跑完，同样需要放行下一个）
    queue_id = inst.get("queue_id")
    if queue_id:
        asyncio.create_task(schedule_queue(queue_id))

    if not stopped:
        return {"message": "实例已结束，无需停止"}
    return {"message": "实例已停止，已退出队列排队"}


@router.get("/{instance_id}/overview", response_model=InstanceOverview)
def get_instance_overview(instance_id: str, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")

    inst = _sync_instance_status(dict(row))
    completed = inst["completed_tasks"]
    failed = inst["failed_tasks"]
    total = inst["total_tasks"]
    finished = completed + failed
    running_pods = 0

    if inst["status"] == "running":
        running_pods = min(inst["concurrent_num"], total - finished) if total > finished else 0

    pending = max(0, total - finished - running_pods)
    rate = (completed / finished * 100) if finished > 0 else 0.0

    error_breakdown = _analyze_task_status(inst["config_path"], total, inst["status"])
    # error_tree 与 error_breakdown 在 _compute_analyze 中同刻写入，缓存生命周期一致
    error_tree = _analyze_tree_cache.get(inst["config_path"], [])
    time_est = _estimate_remaining_time(total, finished, inst.get("started_at"), inst["status"])

    elapsed_seconds = None
    if inst.get("started_at"):
        end = inst.get("stopped_at") or datetime.now().isoformat()
        try:
            elapsed_seconds = round((datetime.fromisoformat(end) - datetime.fromisoformat(inst["started_at"])).total_seconds(), 0)
        except (ValueError, TypeError):
            pass

    return InstanceOverview(
        total=total, completed=completed, failed=failed,
        running=running_pods, pending=pending,
        success_rate=round(rate, 1), error_breakdown=error_breakdown,
        error_tree=error_tree,
        elapsed_seconds=elapsed_seconds,
        **time_est,
    )


def _count_config_files(root: str) -> int:
    """数已下载的 config 文件数：穿透单层嵌套目录后，统计其中的普通文件。
    下载中文件实时落盘，据此可给出「已下载 N 个」的准备进度。"""
    if not root or not os.path.isdir(root):
        return 0
    actual = root
    # 穿透单层嵌套（与 _prepare_instance 的 actual_dir 逻辑一致）
    for _ in range(5):
        try:
            entries = os.listdir(actual)
        except OSError:
            return 0
        if len(entries) == 1 and os.path.isdir(os.path.join(actual, entries[0])):
            actual = os.path.join(actual, entries[0])
        else:
            break
    try:
        with os.scandir(actual) as it:
            return sum(1 for e in it if e.is_file())
    except OSError:
        return 0


@router.post("/overview-batch")
def get_overview_batch(payload: OverviewBatchRequest, user: dict = Depends(get_current_user)):
    """批量返回首页需要的概览字段。

    首页原先对每个 running 实例单独发一次 /overview（当前 30+ 个），每 10s 一轮，
    单客户端就是 30+ 个并发请求；多个客户端叠加后 overview 峰值约 18 次/秒，
    每个都要走 _analyze_task_status 的目录扫描，把 worker 单核打满。
    这里一次性处理，扫描结果在 _analyze_cache 里共享，重复 id 自动去重。
    """
    ids = list(dict.fromkeys(payload.ids or []))
    if len(ids) > 500:
        raise HTTPException(status_code=400, detail="单次最多查询 500 个实例")

    out: dict = {"overviews": {}, "prepare_progress": {}}
    if not ids:
        return out

    placeholders = ",".join("?" * len(ids))
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM task_instances WHERE id IN ({placeholders})", tuple(ids)
        ).fetchall()
    by_id = {dict(r)["id"]: dict(r) for r in rows}

    for iid in ids:
        row = by_id.get(iid)
        if not row:
            continue
        inst = _sync_instance_status(row)
        total = inst["total_tasks"]
        finished = inst["completed_tasks"] + inst["failed_tasks"]
        running_pods = 0
        if inst["status"] == "running" and total > finished:
            running_pods = min(inst["concurrent_num"], total - finished)
        # overview 与 _sync_instance_status 共用缓存，无需在这里重复扫描
        _analyze_task_status(inst["config_path"], total, inst["status"])
        error_tree = _analyze_tree_cache.get(inst["config_path"], [])
        out["overviews"][iid] = {
            "total": total,
            "completed": inst["completed_tasks"],
            "failed": inst["failed_tasks"],
            "running": running_pods,
            "pending": max(0, total - finished - running_pods),
            "success_rate": round(inst["completed_tasks"] / finished * 100, 1) if finished else 0.0,
            "error_breakdown": _analyze_cache.get(inst["config_path"], ({}, 0))[0],
            "error_tree": error_tree,
            **_estimate_remaining_time(total, finished, inst.get("started_at"), inst["status"]),
        }
        if inst["status"] == "preparing":
            out["prepare_progress"][iid] = {
                "status": inst["status"],
                "downloading": True,
                "downloaded_configs": _count_config_files(
                    os.path.join(_get_instance_dir(iid), "configs")
                ),
                "total_tasks": total or 0,
            }
    return out


@router.get("/{instance_id}/prepare-progress")
def get_prepare_progress(instance_id: str, user: dict = Depends(get_current_user)):
    """准备中实例的下载进度：返回已下载到实例 configs 目录的 config 文件数。

    obsutil 整目录下载不逐文件回报，但文件会实时落到磁盘，据此数出「已下载 N 个」。
    下载完成前无法预知总数，故只报已下载数（单调递增）；实例非 preparing 时 downloading=false。
    只读文件系统、不写库，多 worker 安全。
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT status, total_tasks FROM task_instances WHERE id=?", (instance_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")

    configs_dir = os.path.join(_get_instance_dir(instance_id), "configs")
    downloaded = _count_config_files(configs_dir)
    return {
        "status": row["status"],
        "downloading": row["status"] == "preparing",
        "downloaded_configs": downloaded,
        "total_tasks": row["total_tasks"] or 0,
    }


def _estimate_remaining_time(
    total: int, finished: int, started_at: str, status: str,
) -> dict:
    result = {"avg_task_seconds": None, "estimated_remaining_seconds": None, "estimated_finish_time": None}
    if status != "running" or total == 0 or not started_at:
        return result

    try:
        elapsed = (datetime.now() - datetime.fromisoformat(started_at)).total_seconds()
    except (ValueError, TypeError):
        return result

    if elapsed <= 0:
        return result

    done = max(finished, 1)
    avg = elapsed / done
    total_est = total / done * elapsed
    remaining = max(0, total_est - elapsed)
    finish_time = (datetime.now() + timedelta(seconds=remaining)).isoformat(timespec="seconds")

    return {
        "avg_task_seconds": round(avg, 1),
        "estimated_remaining_seconds": round(remaining, 0),
        "estimated_finish_time": finish_time,
    }


_STATUS_RE = re.compile(
    r"任务执行状态=(?P<status>任务成功|任务失败|任务异常)\s+error_code=(?P<code>\S+)"
)
# 状态行里的 config=<文件名> 字段，用于把 task_idx 映射到具体配置名（进而查 eval 缓存）
_CONFIG_RE = re.compile(r"config=(?P<config>\S+)")


def _scan_task_file(fpath: str):
    """读取单个 task 日志，返回 (last_status, last_code, last_config)；异常返回 (None, None, None)。"""
    last_status = None
    last_code = None
    last_config = None
    try:
        with open(fpath, "r", errors="replace") as f:
            for line in f:
                m = _STATUS_RE.search(line)
                if m:
                    last_status = m.group("status")
                    last_code = m.group("code")
                    mc = _CONFIG_RE.search(line)
                    if mc:
                        last_config = mc.group("config")
    except OSError:
        return None, None, None
    return last_status, last_code, last_config


def _compute_analyze(config_path: str, total_tasks: int) -> dict:
    """
    增量扫描：只重读 mtime/size 变化过的 task 日志。
    已完成的任务日志不再变化，后续轮询直接复用上次结果，
    使耗时正比于「本轮新增/变化的文件数」而非日志总数。
    """
    # 后台刷新器直接调本函数，不经过 _analyze_task_status；重启后第一次扫描前
    # 必须先把落盘的签名灌进来，否则 11.7 万份日志会被整篇重读（实测 129s）。
    _load_file_state()
    output_dir = _get_output_dir(config_path)
    logs_dir = os.path.join(output_dir, "logs")

    prev_state = _analyze_file_state.get(config_path, {})
    new_state = {}
    changed = set()  # 本轮实际重读过（签名变化 / 新增）的 fname，供增量写库

    if os.path.isdir(logs_dir):
        try:
            entries = os.scandir(logs_dir)
        except OSError:
            entries = []
        to_read = []
        for entry in entries:
            fname = entry.name
            if not (fname.startswith("task-") and fname.endswith(".log")):
                continue
            try:
                st = entry.stat()
                sig = (st.st_mtime, st.st_size)
            except OSError:
                continue

            cached = prev_state.get(fname)
            if cached is not None and cached[0] == sig[0] and cached[1] == sig[1]:
                # 文件未变化，复用上次解析结果，跳过读取
                new_state[fname] = cached
            else:
                to_read.append((fname, entry.path, sig))
                changed.add(fname)

        # 冷启动（首次部署 / 状态文件丢失）要读上万份小文件。这些是 NFS 上的
        # 独立读取，串行时全被网络往返拖住；并发能显著压缩这段一次性耗时。
        if to_read:
            if len(to_read) < 64:
                results = [(f, p, s, _scan_task_file(p)) for f, p, s in to_read]
            else:
                import concurrent.futures as _cf
                with _cf.ThreadPoolExecutor(max_workers=16) as pool:
                    scanned = list(pool.map(lambda t: _scan_task_file(t[1]), to_read))
                results = [(f, p, s, r) for (f, p, s), r in zip(to_read, scanned)]
            for fname, _path, sig, (status, code, cfg) in results:
                new_state[fname] = (sig[0], sig[1], status, code, cfg)

    _analyze_file_state[config_path] = new_state
    _analyze_changed[config_path] = changed

    succeeded = 0
    failed = 0
    abnormal_by_prefix = {"C": 0, "S": 0, "T": 0, "X": 0}
    # 具体错误码计数：{prefix: {code: count}}
    abnormal_by_code = {"C": {}, "S": {}, "T": {}, "X": {}}

    for _mtime, _size, last_status, last_code, _cfg in new_state.values():
        if last_status == "任务成功":
            succeeded += 1
        elif last_status == "任务失败":
            failed += 1
        elif last_status == "任务异常":
            code = last_code or ""
            prefix = code[:1]
            if prefix not in abnormal_by_prefix:
                prefix = "X"
                code = code or "X999"
            abnormal_by_prefix[prefix] += 1
            abnormal_by_code[prefix][code] = abnormal_by_code[prefix].get(code, 0) + 1

    abnormal = sum(abnormal_by_prefix.values())
    executed = succeeded + failed + abnormal
    not_executed = max(0, total_tasks - executed)

    result = {}
    if succeeded:
        result["任务成功"] = succeeded
    if failed:
        result["任务失败"] = failed
    if abnormal:
        result["任务异常"] = abnormal
        prefix_label = {
            "C": "└ C 客户端",
            "S": "└ S 服务端",
            "T": "└ T 任务侧",
            "X": "└ X 未分类",
        }
        for p in ("C", "S", "T", "X"):
            if abnormal_by_prefix[p]:
                result[prefix_label[p]] = abnormal_by_prefix[p]
    if not_executed:
        result["未执行"] = not_executed

    # 构建折叠树：一级分类汇总 + 具体错误码明细（按码计数降序）
    error_tree = []
    for p in ("C", "S", "T", "X"):
        if not abnormal_by_prefix[p]:
            continue
        codes = [
            {"code": code, "desc": _ERROR_CODE_DESC.get(code, ""), "count": cnt}
            for code, cnt in sorted(
                abnormal_by_code[p].items(), key=lambda kv: (-kv[1], kv[0])
            )
        ]
        error_tree.append({
            "prefix": p,
            "label": _PREFIX_LABEL[p],
            "count": abnormal_by_prefix[p],
            "codes": codes,
        })

    _analyze_cache[config_path] = (result, time.time())
    _analyze_tree_cache[config_path] = error_tree
    return result


# ---- per-task 明细：把增量扫描结果 + evaluator 评分聚合进 task_records 表 ----

def _compute_task_rows(config_path: str, only_fnames=None) -> list[dict]:
    """基于 _analyze_file_state 的增量扫描结果，产出每任务行（不额外读盘）。
    依赖调用前 _compute_analyze 已刷新过该 config_path 的 file_state。
    only_fnames 非 None 时只产出这些 fname 对应的行（增量写库用）；None 则全量。
    config= 尚未解析出（config_name 为 None）的任务直接跳过——其 status 也为 None、
    不贡献任何状态，若用文件名兜底会与后续真实 config_name 形成两行（phantom）。"""
    state = _analyze_file_state.get(config_path, {})
    if only_fnames is not None:
        items = ((f, state[f]) for f in only_fnames if f in state)
    else:
        items = state.items()
    rows = []
    for fname, tup in items:
        # tup = (mtime, size, status, code, config_name)
        _mtime, _size, status, code, config_name = tup
        if not config_name:
            # config= 未解析出：跳过，避免用文件名兜底制造 phantom 行
            continue
        m = re.search(r"task-(\d+)\.log", fname)
        task_idx = int(m.group(1)) if m else None
        category = None
        if status == "任务异常":
            prefix = (code or "")[:1]
            category = prefix if prefix in ("C", "S", "T") else "X"
        rows.append({
            "task_idx": task_idx,
            "config_name": config_name,
            "status": status,
            "error_code": code,
            "error_category": category,
        })
    return rows


def _load_eval_cache(instance_id: str) -> dict:
    """读取 instances/<id>/_eval_score.json（键为 config stem）；不存在返回 {}。不触发 OBS 下载。"""
    cache_file = os.path.join(_get_instance_dir(instance_id), "_eval_score.json")
    if not os.path.exists(cache_file):
        return {}
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_traj_cache(instance_id: str) -> dict:
    """读取 instances/<id>/_traj_score.json（键为 config stem / OBS task_dir）；不存在返回 {}。不触发 OBS 下载。"""
    cache_file = os.path.join(_get_instance_dir(instance_id), "_traj_score.json")
    if not os.path.exists(cache_file):
        return {}
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _sync_task_records(instance_id: str, config_path: str, full: bool = False) -> None:
    """把 per-task 扫描结果 + eval / traj 缓存 UPSERT 进 task_records（跑在执行器线程里，off-event-loop）。
    eval/traj 各列用 COALESCE：本地缓存尚未刷新时保留上次已写入的值。

    增量：默认只 UPSERT 本轮 _compute_analyze 中签名变化过的任务行（_analyze_changed），
    避免每 8s 对整个实例的数万行整表重写。full=True 或该 config 尚未被扫过时走全量。
    """
    changed = _analyze_changed.get(config_path)
    if full or changed is None:
        only_fnames = None  # 全量：首次填充 / eval 分数刷新 / 兜底
    elif not changed:
        return  # 本轮无文件变化，零写入
    else:
        only_fnames = changed

    rows = _compute_task_rows(config_path, only_fnames=only_fnames)
    if not rows:
        return
    eval_map = _load_eval_cache(instance_id)
    traj_map = _load_traj_cache(instance_id)
    # 分批提交：全量模式下 rows = 该实例全部任务行，实测最大实例 63,967 行。
    # 原来整轮只在连接退出时 commit 一次，等于长时间独占 SQLite 的唯一写锁，
    # 调度器/心跳/队列领取全被卡住（5s busy_timeout 一到就报 database is locked）。
    # 每 _SYNC_BATCH_ROWS 行提交一次，把写锁持有时间从「数十秒」压到毫秒级。
    # 同一口径见 offline/output_worker.py 的 _discover_artifacts（每 500 行提交）。
    with get_connection() as conn:
        pending = 0
        for r in rows:
            stem = Path(r["config_name"]).stem
            ev = eval_map.get(stem) or {}
            tj = traj_map.get(stem) or {}
            conn.execute(
                """
                INSERT INTO task_records
                    (instance_id, task_idx, config_name, status, error_code, error_category,
                     eval_score, eval_completion, gate, traj_level, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(instance_id, config_name) DO UPDATE SET
                    task_idx        = excluded.task_idx,
                    status          = excluded.status,
                    error_code      = excluded.error_code,
                    error_category  = excluded.error_category,
                    eval_score      = COALESCE(excluded.eval_score, task_records.eval_score),
                    eval_completion = COALESCE(excluded.eval_completion, task_records.eval_completion),
                    gate            = COALESCE(excluded.gate, task_records.gate),
                    traj_level      = COALESCE(excluded.traj_level, task_records.traj_level),
                    updated_at      = CURRENT_TIMESTAMP
                """,
                (
                    instance_id, r["task_idx"], r["config_name"], r["status"],
                    r["error_code"], r["error_category"],
                    ev.get("score"), ev.get("completion"), ev.get("gate"),
                    tj.get("level"),
                ),
            )
            pending += 1
            if pending >= _SYNC_BATCH_ROWS:
                conn.commit()
                pending = 0


def recover_orphan_preparing() -> int:
    """启动时回收「孤儿 preparing」实例。

    _prepare_and_launch 是挂在事件循环上的后台协程；若后端在「准备中」重启，
    该协程随旧进程一起消失，实例会永远卡在 preparing（pid/started_at 均为空）。
    这里在 startup 时把这些孤儿一律退回 created，让用户重新点启动（配置已就绪，
    重启动会重新数 task 数并拉起进程）。

    必须带时间阈值：`preparing AND pid IS NULL AND started_at IS NULL` 也正是
    「刚进入 preparing、还没拉起进程」的样子。多 worker 部署时，晚启动的 worker
    会把其它 worker 正在 OBS 下载的实例一起打回 created（下载白跑）。
    只回收超过 ORPHAN_GRACE_SECONDS 仍未拿到 pid 的实例。

    返回回退的实例数。
    """
    grace = int(settings.ORPHAN_PREPARING_GRACE_SECONDS)
    try:
        with get_connection() as conn:
            cur = conn.execute(
                """
                UPDATE task_instances
                   SET status='created',
                       error_summary='准备中后端重启被中断，已自动退回 created，可重新启动'
                 WHERE status='preparing' AND pid IS NULL AND started_at IS NULL
                   AND julianday(COALESCE(prepare_started_at, created_at))
                       < julianday('now', ?)
                """,
                (f"-{grace} seconds",),
            )
            n = cur.rowcount or 0
        if n:
            print(f"[startup] 回收孤儿 preparing 实例 {n} 个 -> created")
        return n
    except Exception as e:
        print(f"[startup] 回收孤儿 preparing 失败: {e}")
        return 0


def _refresh_running_once():
    """后台线程：预热 running/preparing 实例的状态与分析缓存。

    含终态(completed/finished)最后一次同步：在线同步只在 running 期间跑，实例完成
    那一刻 `if inst["status"] == "running"` 的分支会错过最后一次写入；且运行窗口内若
    后台没跑到该实例，task_records 会永久缺行。用 task_done_sync 标记兜底：
    终态且 task_done_sync=0 的实例，每轮重扫直至写入成功后置位，缺行实例据此补齐。

    Returns:
        set[str]：本轮「running → 终态」的实例所属队列 id，供调用方触发队列调度。
        （任务的完成是在这里被发现的，所以队列续跑必须挂在这个返回上。）
    """
    queues_to_schedule = set()
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM task_instances "
                "WHERE status IN ('running','preparing') "
                "OR (status IN ('completed','finished') AND task_done_sync=0)"
            ).fetchall()
    except Exception:
        return queues_to_schedule

    for row in rows:
        inst = dict(row)
        try:
            prev_status = inst["status"]
            inst = _sync_instance_status(inst, persist=True)
            if inst["status"] == "running":
                _compute_analyze(inst["config_path"], inst["total_tasks"])
                # 复用刚刷新的 file_state，把 per-task 明细写进 task_records
                try:
                    _sync_task_records(inst["id"], inst["config_path"])
                except Exception:
                    pass
                # 长尾跳过：任务进入尾部窗口时不会产生任何状态变化事件，而调度是
                # 事件驱动的。这里主动把该队列交给调度器，让它判定是否可以提前放行
                # 下一个任务（_try_claim_next 内部有完整条件，重复触发是幂等的）。
                if inst.get("queue_id"):
                    queues_to_schedule.add(inst["queue_id"])
            elif inst["status"] in ("completed", "finished"):
                # 队列续跑：本实例刚跑完，放行同队列的下一排队任务
                if prev_status in ("running", "preparing") and inst.get("queue_id"):
                    queues_to_schedule.add(inst["queue_id"])
                # 终态最后同步：在线同步只在 running 期间跑，完成那一刻/运行窗口内漏跑的
                # 实例 task_records 会永久缺行。task_done_sync=0 的终态实例在此补网——
                # 仅当 task_records 整体为空(0 行)时才需要 full 重扫；行已完整的实例
                # 直接置位，避免对海量历史终态实例全量重扫。写库成功才置位，否则下轮
                # refresher 重试（任务日志已定稿，增量扫描 changed 为空集，故 full=True
                # 全量写一次，ON CONFLICT 幂等）。
                gap = 0
                with get_connection() as conn:
                    gap = conn.execute(
                        "SELECT COUNT(*) FROM task_records WHERE instance_id=?",
                        (inst["id"],),
                    ).fetchone()[0]
                if gap == 0:
                    _compute_analyze(inst["config_path"], inst["total_tasks"])
                    _sync_task_records(inst["id"], inst["config_path"], True)
                with get_connection() as conn:
                    conn.execute(
                        "UPDATE task_instances SET task_done_sync=1 WHERE id=?",
                        (inst["id"],),
                    )
        except Exception:
            continue

    return queues_to_schedule


def _acquire_refresher_leader():
    """多 worker 下只让一个进程跑后台刷新。

    uvicorn --workers 4 时每个 worker 都会执行 startup hook，原先 4 份
    background_cache_refresher 各自扫描同一批实例、重复写同一张表，
    CPU 直接翻 4 倍。这里用文件上的非阻塞 flock 做 leader 选举：
    拿到锁的进程才跑刷新，其余进程只标记 _bg_refresh_active 后返回。

    锁句柄必须一直持有到进程退出（进程死掉内核自动释放，新的 leader 下一轮启动时接上）。
    """
    try:
        import fcntl
        lock_path = os.path.join(os.path.dirname(settings.DB_PATH), ".refresher.lock")
        fh = open(lock_path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return None
        # 故意不关闭 fh：关闭即释放锁
        return fh
    except Exception:
        # 拿不到文件锁（只读目录等）时退化为「单 worker 假设」，保持旧行为
        return True


async def background_cache_refresher(interval: float = 8.0):
    """
    周期性在线程池里刷新缓存，使 overview/list 请求线程只读缓存、永不触发扫描，
    从根本上避免多客户端并发把 anyio 同步线程池打满。
    """
    global _bg_refresh_active
    leader = _acquire_refresher_leader()
    if leader is None:
        # 非 leader 的 worker：不扫描、不写库，请求线程读 leader 落盘的快照
        _load_shared_caches()
        _bg_refresh_active = True
        return
    # 先同步预热一轮，再开启「请求只读缓存」模式，避免首轮请求撞冷扫描
    try:
        for qid in (await async_execute(_refresh_running_once) or ()):
            asyncio.create_task(schedule_queue(qid))
    except Exception:
        pass
    _save_shared_caches()
    _save_file_state(force=True)
    _bg_refresh_active = True
    while True:
        await asyncio.sleep(interval)
        try:
            for qid in (await async_execute(_refresh_running_once) or ()):
                asyncio.create_task(schedule_queue(qid))
        except Exception:
            pass
        _save_shared_caches()
        _save_file_state()
        # 卡住的 preparing 看门狗：协程中途消失时实例会永久占着队列槽位，
        # 这里超过阈值仍未拿到 pid 的标记为 failed 并放行同队列下一个任务。
        try:
            from ..services.queue_scheduler import recover_stuck_preparing
            for qid in (await async_execute(recover_stuck_preparing) or ()):
                asyncio.create_task(schedule_queue(qid))
        except Exception:
            pass
        # 兜底扫描：有排队任务但队列内无占用者的队列，重新交给调度器。
        # 调度是事件驱动的——某次调度丢失（SQLite busy、协程崩溃）或容量限制解除后，
        # 没有任何事件会再唤醒这条队列，任务会静默卡在 queued。
        try:
            from ..services.queue_scheduler import find_schedulable_queues
            for qid in (await async_execute(find_schedulable_queues) or ()):
                asyncio.create_task(schedule_queue(qid))
        except Exception:
            pass


def _shared_cache_path() -> str:
    return os.path.join(os.path.dirname(settings.DB_PATH), ".analyze_snapshot.json")


_shared_loaded_mtime = [0.0]
_shared_last_check = [0.0]
_SHARED_CACHE_ENTRY_CAP = 500

# 每文件扫描签名（mtime/size + 解析结果）持久化文件。
# 不持久化的话，进程重启后 _analyze_file_state 是空的，_compute_analyze 会把
# 所有 task 日志整篇重读一遍——实测 37 个 running 实例共 11.7 万份日志、136 秒。
# 落盘后重启只需 stat 一遍核对签名（约 5 秒），未变化的文件直接复用结果。
_file_state_last_save = [0.0]
_FILE_STATE_SAVE_INTERVAL = 120.0
_FILE_STATE_LOADED = [False]


def _save_shared_caches() -> None:
    """leader 把扫描结果落到磁盘，供其它 worker 直接读取。

    uvicorn --workers 4 下每个进程的 _analyze_cache 是独立的。没有这个快照时，
    非 leader 的 worker 收到 overview 请求会各自把上万份 task 日志重扫一遍
    （实测 36 个运行中实例冷扫合计 ~115s），造成「时快时卡」。
    """
    try:
        # 只带最近用到的条目：缓存按访问顺序滚动，超出上限的老实例不会再有请求
        def _tail(d: dict) -> dict:
            items = list(d.items())[-_SHARED_CACHE_ENTRY_CAP:]
            return dict(items)

        payload = {
            "analyze": {k: [v[0], v[1]] for k, v in _tail(_analyze_cache).items()},
            "tree": _tail(_analyze_tree_cache),
            "status": {k: [v[0], v[1]] for k, v in _tail(_status_cache).items()},
        }
        path = _shared_cache_path()
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _load_shared_caches() -> None:
    """把 leader 的扫描快照灌进本进程缓存。按 mtime 节流（2s），命中间隔可忽略。"""
    try:
        now = time.time()
        if now - _shared_last_check[0] < 2:
            return
        _shared_last_check[0] = now
        path = _shared_cache_path()
        st = os.stat(path)
        if st.st_mtime <= _shared_loaded_mtime[0]:
            return
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        for k, v in (payload.get("analyze") or {}).items():
            _analyze_cache[k] = (v[0], v[1])
        _analyze_tree_cache.update(payload.get("tree") or {})
        for k, v in (payload.get("status") or {}).items():
            _status_cache[k] = (v[0], v[1])
        _shared_loaded_mtime[0] = st.st_mtime
    except Exception:
        pass


def _file_state_path() -> str:
    return os.path.join(os.path.dirname(settings.DB_PATH), "analyze_file_state.json.gz")


def _load_file_state() -> None:
    """启动时灌入上次的 per-file 扫描签名，避免重启后把 10 万+ 份日志重读一遍。

    只加载一次；之后由 leader 每 2 分钟覆盖写回。签名按 (mtime, size) 核对，
    日志有任何追加都会自然失效并重读，不存在读到陈旧内容的风险。
    """
    if _FILE_STATE_LOADED[0]:
        return
    _FILE_STATE_LOADED[0] = True
    try:
        import gzip
        with gzip.open(_file_state_path(), "rt", encoding="utf-8") as f:
            payload = json.load(f)
        for k, v in (payload or {}).items():
            _analyze_file_state[k] = {
                fname: tuple(rec) for fname, rec in v.items()
            }
    except Exception:
        pass


def _save_file_state(force: bool = False) -> None:
    """把 per-file 签名落盘（leader 调用）。节流 2 分钟，11 万条约几 MB gzip。"""
    now = time.time()
    if not force and now - _file_state_last_save[0] < _FILE_STATE_SAVE_INTERVAL:
        return
    _file_state_last_save[0] = now
    try:
        import gzip
        payload = {k: v for k, v in _analyze_file_state.items()}
        path = _file_state_path()
        tmp = f"{path}.{os.getpid()}.tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception:
        pass


def _analyze_task_status(config_path: str, total_tasks: int, instance_status: str = "running") -> dict:
    _load_file_state()
    now = time.time()
    cache_key = config_path
    cached = _analyze_cache.get(cache_key)

    # 本进程没扫过：先看 leader 落盘的快照，能命中就不必自己扫
    if cached is None:
        _load_shared_caches()
        cached = _analyze_cache.get(cache_key)

    # 非 running：状态已定，命中缓存直接返回，永不重扫
    if instance_status not in ("running",):
        if cached:
            return cached[0]

    if cached and (now - cached[1]) < _ANALYZE_CACHE_TTL:
        return cached[0]

    # 后台预热启用时，请求线程只读缓存（哪怕已过期），把扫描留给后台任务，
    # 避免多客户端并发把 anyio 同步线程池打满
    if _bg_refresh_active and cached:
        return cached[0]

    return _compute_analyze(config_path, total_tasks)


# task_records 允许的排序列白名单（防 SQL 注入）
_TASK_RECORDS_SORT = {
    "task_idx", "config_name", "status", "error_code",
    "error_category", "eval_score", "eval_completion", "gate", "updated_at",
}


@router.get("/{instance_id}/task-records")
async def list_task_records(
    instance_id: str,
    category: str | None = Query(None, description="C/S/T/X 或 success"),
    status: str | None = Query(None, description="任务成功/任务失败/任务异常"),
    min_score: float | None = Query(None, description="eval_score 下限"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    sort_by: str = Query("task_idx"),
    sort_dir: str = Query("asc"),
    user: dict = Depends(get_current_user),
):
    """按实例查询 per-task 明细，支持按错误分类/状态/分数筛选、分页、排序。"""
    row = await async_query_one(
        "SELECT * FROM task_instances WHERE id=?", (instance_id,)
    )
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")

    config_path = row["config_path"]

    # 确保 task_records 已填充（running 刷新 / 老实例 0 行兜底）
    await _ensure_task_records(instance_id, config_path, row["status"], row["total_tasks"])

    # 组装筛选条件
    where = ["instance_id = ?"]
    params: list = [instance_id]
    if category:
        if category == "success":
            where.append("status = ?")
            params.append("任务成功")
        else:
            where.append("error_category = ?")
            params.append(category)
    if status:
        where.append("status = ?")
        params.append(status)
    if min_score is not None:
        where.append("eval_score >= ?")
        params.append(min_score)
    where_sql = " AND ".join(where)

    # 排序：列白名单 + 方向白名单
    col = sort_by if sort_by in _TASK_RECORDS_SORT else "task_idx"
    direction = "DESC" if str(sort_dir).lower() == "desc" else "ASC"

    total_row = await async_query_one(
        f"SELECT COUNT(*) AS n FROM task_records WHERE {where_sql}", tuple(params)
    )
    total = total_row["n"] if total_row else 0

    offset = (page - 1) * page_size
    tasks = await async_query(
        f"SELECT task_idx, config_name, status, error_code, error_category, "
        f"eval_score, eval_completion, gate, traj_level "
        f"FROM task_records WHERE {where_sql} "
        f"ORDER BY {col} {direction} LIMIT ? OFFSET ?",
        tuple(params) + (page_size, offset),
    )

    return {"total": total, "page": page, "page_size": page_size, "tasks": tasks}


async def _ensure_task_records(instance_id: str, config_path: str, status: str, total_tasks: int):
    """确保 task_records 已填充：running 每次刷新；非 running 且 0 行时兜底同步一次。"""
    if status == "running":
        try:
            await async_execute(_sync_task_records, instance_id, config_path)
        except Exception:
            pass
        return
    existing = await async_query_one(
        "SELECT COUNT(*) AS n FROM task_records WHERE instance_id=?", (instance_id,)
    )
    if not existing or existing["n"] == 0:
        try:
            await async_execute(_compute_analyze, config_path, total_tasks)
            # 兜底填充：非 running 且 0 行，需全量写入（此时 changed 可能为空集）
            await async_execute(_sync_task_records, instance_id, config_path, True)
        except Exception:
            pass


@router.get("/{instance_id}/task-status-map")
async def task_status_map(instance_id: str, user: dict = Depends(get_current_user)):
    """返回全量 {task_idx: {status, category, code}}，供日志页下拉框着色（精简、无分页）。"""
    row = await async_query_one(
        "SELECT * FROM task_instances WHERE id=?", (instance_id,)
    )
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")

    await _ensure_task_records(instance_id, row["config_path"], row["status"], row["total_tasks"])

    rows = await async_query(
        "SELECT task_idx, status, error_code, error_category "
        "FROM task_records WHERE instance_id=? AND task_idx IS NOT NULL",
        (instance_id,),
    )
    mapping = {
        r["task_idx"]: {
            "status": r["status"],
            "category": r["error_category"],
            "code": r["error_code"],
        }
        for r in rows
    }
    return {"map": mapping}


@router.delete("/{instance_id}")
def delete_instance(instance_id: str, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")

    inst = dict(row)
    if inst["status"] == "running" and _is_pid_running(inst.get("pid")):
        raise HTTPException(status_code=400, detail="实例正在运行中，请先停止")

    instance_dir = _get_instance_dir(instance_id)
    if os.path.isdir(instance_dir):
        shutil.rmtree(instance_dir)

    _status_cache.pop(instance_id, None)
    _analyze_cache.pop(inst.get("config_path"), None)
    _analyze_tree_cache.pop(inst.get("config_path"), None)
    _analyze_file_state.pop(inst.get("config_path"), None)
    _analyze_changed.pop(inst.get("config_path"), None)

    with get_connection() as conn:
        conn.execute("DELETE FROM task_records WHERE instance_id=?", (instance_id,))
        conn.execute("DELETE FROM task_instances WHERE id=?", (instance_id,))

    return {"message": "实例已删除"}
