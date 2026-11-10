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
from ..services.secrets import instance_create_params, mask_create_params, redact_text
from . import harness_meta
from . import harness_configs
from . import sandbox_configs
from . import resource_pools

router = APIRouter(prefix="/api/instances", tags=["instances"])

JIUWENSWARM_TYPES = {"jiuwenswarm", "jiuwenswarm-dev", "jiuwenswarm-xiaoyi"}
DEFAULT_MAX_TTL_SECOND = 36000
MAX_MAX_TTL_SECOND = 36000  # 10 小时, 与前端 el-input-number 的 :max 保持一致
MAIN_TIMEOUT_MARGIN_SECONDS = 600
QUERY_TIMEOUT_MARGIN_SECONDS = 1200


def _apply_timeout_settings(base, max_ttl_second: int) -> tuple[int, int, int]:
    """按沙箱 max_ttl_second 逐级下扣，派生三层超时。

    用户只填最外层的沙箱存活上限，另两层从它减固定余量得到：
        沙箱 TTL  >  主程序超时 (−600s)  >  单 query 超时 (−1200s)
    这个顺序保证内层先触发、外层还活着 —— query 卡死时 harness 还有 600s 收尾
    落盘，主程序超时退出后沙箱还有 600s 供 OBS 上传轨迹。反过来（沙箱先消失）
    会把日志和轨迹一起带走，失败原因就查不到了。

    queries[].timeout 本身不在这里改写：由 hive.py 的 ``_upload_data_config``
    读 run_config.task.query_timeout 后在上传副本里注入，原始任务配置保持原样。
    """
    # 下限保证最内层的 query 超时至少为 1s，避免填太小导出非正数；
    # 上限 10 小时，和前端输入框一致，防止直接打 API 绕过界面约束。
    max_ttl_second = max(QUERY_TIMEOUT_MARGIN_SECONDS + 1, int(max_ttl_second))
    max_ttl_second = min(MAX_MAX_TTL_SECOND, max_ttl_second)
    main_timeout = max_ttl_second - MAIN_TIMEOUT_MARGIN_SECONDS
    query_timeout = max_ttl_second - QUERY_TIMEOUT_MARGIN_SECONDS
    base.run_config.task.main_python_timeout = main_timeout
    base.run_config.task.query_timeout = query_timeout
    sandbox_key = sandbox_configs.active_sandbox_key(base)
    if sandbox_key and base.sandbox.get(sandbox_key) is not None:
        # 模板/派生配置里可能没有这个键(或写成了别的形式), 此时直接赋值会抛
        # ConfigAttributeError。用 force_add 保证缺失时补上, 存在时覆盖。
        OmegaConf.update(
            base, f"sandbox.{sandbox_key}.max_ttl_second", max_ttl_second, force_add=True
        )
    return max_ttl_second, main_timeout, query_timeout

# 占用队列槽位的状态（见 queue_scheduler.OCCUPYING_STATUSES），拼成 SQL 占位符
_OCCUPYING_STATUSES = OCCUPYING_STATUSES
_OCCUPYING_PLACEHOLDERS = ",".join("?" * len(_OCCUPYING_STATUSES))

# 独立的 key 申请路由（路径 /api/generate-api-key，不带 instances 前缀）
key_router = APIRouter(prefix="/api", tags=["api-key"])

ALLOWED_CONFIG_FILES = harness_meta.ALLOWED_CONFIG_FILES()

_status_cache = {}
_STATUS_CACHE_TTL = 5

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


class UpstreamKeyError(Exception):
    """申请 key 时上游明确拒绝（HTTP 200 但 success:false / 无 key）。

    与网络异常区分开：这类错误带着上游给的可读原因（名字太长、邀请码不存在…），
    应该原样透给用户，让用户自己照提示改（比如把 key 名字改短），而不是包成
    一句笼统的「申请失败」。
    """


# 上游返回体里承载失败原因的字段，按优先级取第一个非空的。
# 不同网关叫法不一：tokenfly 用 message，也见过 msg / error / detail。
_UPSTREAM_MSG_FIELDS = ("message", "msg", "error", "detail", "error_msg")


def _upstream_message(data) -> str:
    """从上游响应体里取可读的失败原因；取不到返回空串。"""
    if not isinstance(data, dict):
        return ""
    for field in _UPSTREAM_MSG_FIELDS:
        val = data.get(field)
        if isinstance(val, str) and val.strip():
            return val.strip()
        # 有些网关把原因嵌在 error 对象里（{"error": {"message": "..."}}）
        if isinstance(val, dict):
            nested = _upstream_message(val)
            if nested:
                return nested
    return ""


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
        api_key = data.get(key_field, "")
        if not api_key:
            # 上游用「HTTP 200 + success:false + message」表示失败（实测 tokenfly
            # 网关就是这样：名字超长回 {"message":"Token name is too long"}），
            # raise_for_status 拦不住，必须在这里把 message 透出来。否则用户只看到
            # 一句笼统的「未返回 api_key」，不知道是名字太长还是邀请码不对。
            raise UpstreamKeyError(_upstream_message(data) or "上游服务未返回 api_key")
        return api_key


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
    except UpstreamKeyError as e:
        # 上游明确拒绝：原样透出它给的原因（如 "Token name is too long"），
        # 用户照提示自己改（把 key 名字改短）即可，不必来问。
        raise HTTPException(status_code=502, detail=f"申请 key 失败：{e}")
    except httpx.HTTPStatusError as e:
        # 非 2xx：同样优先用上游的 message。空响应体（如 429 限流只给状态码）
        # 时退回状态码说明，否则前端会弹出一个没有原因的空报错。
        detail = ""
        try:
            detail = _upstream_message(e.response.json())
        except Exception:
            detail = ""
        detail = detail or (e.response.text or "").strip()
        if not detail:
            detail = f"上游返回 HTTP {e.response.status_code}"
            if e.response.status_code == 429:
                detail += "（申请过于频繁，被限流，请稍后再试）"
        raise HTTPException(status_code=502, detail=f"申请 key 失败：{detail}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"申请 key 失败：{e}")

    return {"api_key": api_key}


def _get_instance_dir(instance_id: str) -> str:
    return os.path.join(settings.INSTANCE_DIR, instance_id)


def _get_output_dir(config_path: str) -> str:
    config_basename = Path(config_path).stem
    instance_dir = str(Path(config_path).parent)
    return os.path.join(instance_dir, "outputs", config_basename)


# 「重跑失败」时从源实例继承来的成功记录清单。
#
# 为什么不直接写进 complete.jsonl：平台数的是 complete.jsonl 的**行数**
# （见 _count_lines / _sync_instance_status），而重跑实例会继承源实例的成功名单。
# 两者放同一个文件时，新实例还没跑任何东西，completed_tasks 就已经等于源实例的成功数
# （生产实测出现过 total=32 / completed=68，进度 212%）。分成两个文件后，
# complete.jsonl 天然只含「本次运行真正跑出来的」，所有下游计数不用改。
#
# hive.py 侧把两份都读进跳过集合（run_output_complete_record），行为不变。
INHERITED_COMPLETE_RECORD = "complete.inherited.jsonl"


def _inherited_complete_path(output_dir: str) -> str:
    return os.path.join(output_dir, INHERITED_COMPLETE_RECORD)


def _count_lines(file_path: str) -> int:
    if not os.path.exists(file_path):
        return 0
    count = 0
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            count += chunk.count(b"\n")
    return count


def _done_counts(instance_ids: list[str], conn) -> dict:
    """批量取每个实例的「DONE 数」= task_records 里 task_done=1 的行数。

    与 _task_done_counts 的区别是数据来源：
      - _task_done_counts：task_traj_records.task_done，来自 OBS 的 tsr / 慢路径。
        但补全快路径刻意不拉这个字段，导致 254 个实例该列恒为 0。
      - _done_counts（本函数）：task_records.task_done，由 worker 直接扫本地
        task-N.log 得到，不依赖 OBS —— 也就是「本地日志口径」，最接近真相。

    **回退规则**：某实例若还没被扫过（task_done 全为 NULL），返回的字典里不会有它，
    调用方据此回退到 OBS 口径，避免新列因为「还没轮到」而显示 0。

    性能：走 idx_task_records_done 部分索引。
    """
    ids = [i for i in instance_ids if i]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT instance_id, COUNT(*) AS n FROM task_records "
        f"WHERE task_done=1 AND instance_id IN ({placeholders}) "
        f"GROUP BY instance_id",
        tuple(ids),
    ).fetchall()
    out = {r["instance_id"]: r["n"] for r in rows}
    # 再查一次「扫过但没有 done」的实例，用来区分「扫过且为 0」与「压根没扫」：
    # 前者要显示真实的 0，后者要回退 OBS 口径。不区分的话两者都会漏掉，看起来一样。
    scanned = conn.execute(
        f"SELECT DISTINCT instance_id FROM task_records "
        f"WHERE task_done IS NOT NULL AND instance_id IN ({placeholders})",
        tuple(ids),
    ).fetchall()
    for r in scanned:
        out.setdefault(r["instance_id"], 0)
    return out


def _task_done_counts(instance_ids: list[str], conn) -> dict:
    """批量取每个实例的「成功轨迹数」= task_traj_records 里 task_done=1 的行数。

    为什么单独一个口径：task_done 是 assistant 自己在对话末尾打的【Task_Done】标记，
    代表「任务真的做完了」；而 completed_tasks 只是 complete.jsonl 的行数，
    只要流水线没抛异常就算成功——哪怕 agent 根本没做完（比如撞到最大轮次 T002、
    或 agent 自己判了失败）。两者在生产上差得很远，首页要同时给出。

    性能：走 idx_traj_task_done_inst 部分索引（只收 task_done=1 的行），
    COUNT 走 covering index。全表 GROUP BY 是 1.97s，加索引后 0.10s。
    """
    ids = [i for i in instance_ids if i]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT instance_id, COUNT(*) AS n FROM task_traj_records "
        f"WHERE task_done=1 AND instance_id IN ({placeholders}) "
        f"GROUP BY instance_id",
        tuple(ids),
    ).fetchall()
    return {r["instance_id"]: r["n"] for r in rows}


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


# 镜像地址 → 注册短名。/instances 一轮要给 2000+ 行填这个名字，
# 逐行查库等于把刚优化掉的耗时加回来；镜像表只有几十行且极少变动，
# 所以整表缓存 60s（新注册的镜像最多 1 分钟后在筛选里显示正确短名）。
_IMAGE_NAME_CACHE: dict = {}
_IMAGE_NAME_CACHE_AT = 0.0
_IMAGE_NAME_TTL = 60.0


def _image_name_map() -> dict:
    global _IMAGE_NAME_CACHE, _IMAGE_NAME_CACHE_AT
    now = time.time()
    if _IMAGE_NAME_CACHE_AT and (now - _IMAGE_NAME_CACHE_AT) < _IMAGE_NAME_TTL:
        return _IMAGE_NAME_CACHE
    try:
        with get_connection() as conn:
            rows = conn.execute("SELECT name, address FROM images").fetchall()
        _IMAGE_NAME_CACHE = {r["address"]: r["name"] for r in rows}
        _IMAGE_NAME_CACHE_AT = now
    except Exception:
        # 查不到就沿用上一份（或空表），不能让首页因为镜像表问题整个 500
        pass
    return _IMAGE_NAME_CACHE


def _image_display_name(address: str) -> str:
    """镜像展示名：优先注册短名，未注册的回退地址末段（repo:tag）。"""
    if not address:
        return ""
    name = _image_name_map().get(address)
    if name:
        return name
    return address.rsplit("/", 1)[-1]


def _with_harness_type(d: dict) -> dict:
    params = {}
    if d.get("create_params"):
        try:
            params = json.loads(d["create_params"])
        except Exception:
            params = {}
    d["harness_type"] = params.get("harness_type", "openclaw")
    # 首页「镜像」筛选用。create_params 里存的是镜像完整地址，
    # 下拉要显示的是注册时的短名（multiharness:26.9.24v1），所以这里同时给出两个：
    # image_address 做筛选值（唯一且稳定），image_name 做展示。
    # 未注册/已删除的镜像查不到短名，回退取地址末段（仍可筛，只是名字长一点）。
    address = params.get("image_name") or ""
    d["image_address"] = address
    d["image_name"] = _image_display_name(address)
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


def _cores(v, default: float) -> float:
    """K8s CPU 量 → 核数。复用沙箱配置的 to_cores（500m → 0.5 核）。"""
    return sandbox_configs.to_cores(v, default)


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
                "cpu_request": _cores(req.get("cpu"), 2),
                "cpu_limit": _cores(lim.get("cpu"), 3),
                "memory_request": _gib(req.get("memory") or "6Gi"),
                "memory_limit": _gib(lim.get("memory") or "8Gi"),
            }
        # Linux 沙箱写法
        return {
            "cpu_request": _cores(res.get("cpu_request"), 2),
            "cpu_limit": _cores(res.get("cpu_limit"), 3),
            "memory_request": _gib(res.get("memory_request") or "6Gi"),
            "memory_limit": _gib(res.get("memory_limit") or "8Gi"),
        }

    # 历史形态：env_make.args.resources
    env_make = cfg.get("env_make") or {}
    res = (env_make.get("args") or {}).get("resources") or {}
    if res:
        return {
            "cpu_request": _cores(res.get("cpu_request"), 2),
            "cpu_limit": _cores(res.get("cpu_limit"), 3),
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
                        "cpu_request": _cores(summary.get("cpu_request"), 2),
                        "cpu_limit": _cores(summary.get("cpu_limit"), 3),
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
        ids = [r["id"] for r in rows]
        # 一次聚合出全部实例的成功轨迹数，避免每个实例单独 COUNT（单次 5ms × 1882 个 = 9s）
        done_counts = _task_done_counts(ids, conn)
        # DONE：本地日志口径。字典里没有 = 还没扫过 → 回退 OBS 口径
        local_done = _done_counts(ids, conn)
    out = []
    for r in rows:
        inst = _sync_instance_status(dict(r))
        inst["done_trajs"] = done_counts.get(inst["id"], 0)
        inst["done_tasks"] = local_done.get(inst["id"], inst["done_trajs"])
        out.append(InstanceInfo(**_with_resources(_with_harness_type(inst))))
    return out


@router.get("/{instance_id}", response_model=InstanceInfo)
def get_instance(instance_id: str, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="实例不存在")
        inst = _sync_instance_status(dict(row))
        inst["done_trajs"] = _task_done_counts([instance_id], conn).get(instance_id, 0)
        inst["done_tasks"] = _done_counts([instance_id], conn).get(
            instance_id, inst["done_trajs"])
    return InstanceInfo(**_with_resources(_with_harness_type(inst)))


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
    max_ttl_second = int(req.max_ttl_second or DEFAULT_MAX_TTL_SECOND)
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
    # 三层超时全部由沙箱 TTL 派生，不依赖任务 configs 内容，创建阶段即是终值，
    # 启动准备阶段不再重算（因此用户手改实例 config.yaml 的超时能保留下来）。
    _apply_timeout_settings(base, max_ttl_second)

    if req.skill_dir:
        base.run_config.obs.skill_download_path = req.skill_dir
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
                base_url = base_url[:-3].rstrip("/")
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
                base_url = base_url[:-3].rstrip("/")
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
        # 展示序号：取当前最大值 +1，**只增不复用**（删掉的号留空洞）。
        # 与 INSERT 同一连接同一事务，并发创建不会撞号（SQLite 单写者）。
        # 不用 COUNT(*)+1：删过实例后会和已有的号重复。
        next_seq = (conn.execute(
            "SELECT MAX(seq) AS m FROM task_instances"
        ).fetchone()["m"] or 0) + 1
        conn.execute(
            """INSERT INTO task_instances
               (id, name, config_path, status, created_by, total_tasks, concurrent_num,
                config_snapshot, create_params, created_at, queue_id, queue_position,
                suffix_api_keys, copied_from, seq)
               VALUES (?, ?, ?, 'created', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (instance_id, req.name, config_path, user["username"], 0,
             req.concurrent_num, OmegaConf.to_yaml(base),
             json.dumps(_create_params, ensure_ascii=False),
             datetime.now().isoformat(), req.queue_id, queue_position,
             1 if req.suffix_api_keys else 0, req.copy_complete_from or None,
             next_seq),
        )
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()

    # --- 6. 重跑失败: 搬源实例 complete.jsonl 到新实例的「继承」清单 ---
    #
    # 拷成 complete.inherited.jsonl 而不是 complete.jsonl：源实例的成功记录是
    # 「本次不用再跑」的跳过名单，不是新实例自己的战绩。混进 complete.jsonl 会让
    # completed_tasks 从一开始就继承源实例的数字（用户报告：原来 10 条，新跑 5 条
    # 变成 15 条，进度和成功率全是错的）。分成两个文件后平台只数 complete.jsonl。
    #
    # 源实例若本身也是重跑出来的，它自己的 complete.inherited.jsonl 一并并进来，
    # 否则链式重跑（A→B→C）到 C 时会把 A 的成功任务再跑一遍。
    if req.copy_complete_from:
        with get_connection() as conn:
            src_row = conn.execute(
                "SELECT config_path FROM task_instances WHERE id=?", (req.copy_complete_from,)
            ).fetchone()
        if src_row:
            src_output_dir = _get_output_dir(src_row["config_path"])
            new_output_dir = _get_output_dir(config_path)
            os.makedirs(new_output_dir, exist_ok=True)
            inherited = _inherited_complete_path(new_output_dir)
            seen: set[str] = set()
            lines: list[str] = []
            for src_name in ("complete.jsonl", INHERITED_COMPLETE_RECORD):
                src_file = os.path.join(src_output_dir, src_name)
                if not os.path.exists(src_file):
                    continue
                try:
                    with open(src_file, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line and line not in seen:
                                seen.add(line)
                                lines.append(line)
                except OSError as e:
                    print(f"[create] 读取源实例 {src_name} 失败: {e}")
            if lines:
                # 原地写（NFSv3 不支持 rename，见 sfs_turbo 迁移记录）
                with open(inherited, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")

    return InstanceInfo(**dict(row))


# ============================================================================
# 配置查看
# ============================================================================

# create_params 的读取已移到 services/secrets.py（logs.py 的 obs-view 也要用
# 同一份 key 清单脱敏，放在 router 里会让两个路由模块互相 import）。
# 保留本名字是为了下面 /configs 的调用点不变。
_instance_create_params = instance_create_params


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

    # key 脱敏：这些文件是后端拿 create_params 里的 key 填出来的，原文在手上，
    # 直接按原文替换，不必在 13 种 harness 格式里找 key 字段叫什么、在哪一层
    # （同 hive.py 打沙箱后缀的做法）。拿不到 create_params 时原样返回，不 500。
    content = redact_text(content, _instance_create_params(instance_id))

    file_type = "yaml" if filename.endswith(".yaml") else "json"
    return {"filename": filename, "type": file_type, "content": content}


@router.get("/{instance_id}/task-config")
def get_task_config(instance_id: str, config_name: str = Query(..., description="config 文件名"),
                    user: dict = Depends(get_current_user)):
    """单个 task 的输入配置原文（会话列表「任务配置」列的弹窗）。

    与 `/configs/{filename}` 的区别：那个只放实例级的白名单文件
    （config.yaml 等），这里读的是 `task_input_path` 下**每任务一个**的 json
    （`hive` 建队列时 `enumerate(sorted(files))` 的那批，数量上万）。

    **路径安全**：config_name 必须是纯文件名 —— 用 os.path.basename 削掉任何
    目录成分，再校验结果与入参一致。否则 `?config_name=../../../etc/passwd`
    就能越过 task_input_path 读任意文件（这个接口对所有登录用户开放，含 viewer）。

    **key 脱敏**：与 /configs/{filename} 同款 —— 任务配置里可能带模型 key
    （agents[].config 里嵌 api_key），而 viewer 也能调本接口。
    """
    safe = os.path.basename(config_name or "")
    if not safe or safe != config_name:
        raise HTTPException(status_code=400, detail="非法的 config 文件名")

    with get_connection() as conn:
        row = conn.execute(
            "SELECT config_path FROM task_instances WHERE id=?", (instance_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")

    # 复用 shallow_local 的解析：它读 config.yaml 的 run_config.task.task_input_path，
    # 并在目录不可用时返回 None（实例被搬迁/清理后）。
    from offline.shallow_local import _task_input_path
    d = _task_input_path({"config_path": row["config_path"]})
    if not d:
        raise HTTPException(status_code=404,
                            detail="取不到任务输入目录（实例可能已被清理或搬迁）")

    fpath = os.path.join(d, safe)
    if not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail=f"配置文件不存在: {safe}")

    try:
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"读取失败: {str(e)[:120]}")

    content = redact_text(content, _instance_create_params(instance_id))
    return {"config_name": safe, "type": "json", "content": content,
            "size": len(content), "path": fpath}


def _build_create_params_or_404(instance_id: str) -> dict:
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


@router.get("/{instance_id}/create-params")
def get_create_params(instance_id: str, user: dict = Depends(get_current_user)):
    """详情页「配置信息 → 创建参数」卡片（API Key 已脱敏）。

    组装逻辑与任务导出共用 api.services.create_params.build_create_params，
    保证「详情页看到什么，导出就是什么」。

    脱敏只加在这一层，不动 build_create_params：导出和复制实例都还要原文。
    任何登录用户（含 viewer）都能调本接口，所以 key 绝不能明文下发。
    """
    return mask_create_params(_build_create_params_or_404(instance_id))


@router.get("/{instance_id}/create-params/full")
def get_create_params_full(instance_id: str, user: dict = Depends(require_operator)):
    """「复制实例」专用：返回含完整 API Key 的创建参数，仅 operator/admin 可调。

    复制实例要把源实例的 key 回填进新建表单，拿到掩码值会让新任务带着假 key
    去跑（静默失败，不报错），所以这条路径必须给原文；但收紧到 operator，
    viewer 调不到。前端只在 CreateInstance 的 copy_from 分支用它。
    """
    return _build_create_params_or_404(instance_id)


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

    # 重跑实例：从源实例继承来的成功任务本次不会再跑，分母里要扣掉，
    # 否则 total 与 completed 不同源，进度永远到不了 100%、all_done 判据也会错。
    inherited_count = _count_lines(_inherited_complete_path(_get_output_dir(config_path)))
    if inherited_count:
        total_tasks = max(0, total_tasks - inherited_count)

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
        # queue_priority 一并清 0：上一次那轮插队不该跟着这次重跑一起复活，
        # 否则用户以为排到队尾，实际却被旧优先级顶到了队首。
        with get_connection() as conn:
            max_pos = conn.execute(
                "SELECT MAX(queue_position) AS m FROM task_instances WHERE queue_id = ?",
                (queue_id,)
            ).fetchone()
            new_pos = (max_pos["m"] or 0) + 1
            conn.execute(
                "UPDATE task_instances SET queue_position=?, queue_priority=0 WHERE id=?",
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


def _clear_failed_task_logs(output_dir: str) -> list[tuple[str, int, str | None]]:
    """删除上一轮失败/异常的 task-*.log，保留其它所有日志。

    返回 ``(path, task_idx, config_name)``，供调用方同步清理对应数据库明细。
    成功任务会被 complete.jsonl 跳过，必须保留其日志和统计；main/nohup 日志也
    不属于单任务重跑范围，因此一律不动。
    """
    logs_dir = os.path.join(output_dir, "logs")
    if not os.path.isdir(logs_dir):
        return []

    failed = []
    with os.scandir(logs_dir) as entries:
        for entry in entries:
            match = re.fullmatch(r"task-(\d+)\.log", entry.name)
            if not match or not entry.is_file(follow_symlinks=False):
                continue
            status, _code, config_name = _scan_task_file(entry.path)
            if status in ("任务失败", "任务异常"):
                failed.append((entry.path, int(match.group(1)), config_name))

    failed.sort(key=lambda item: item[1])
    removed = []
    for path, task_idx, config_name in failed:
        try:
            os.remove(path)
            removed.append((path, task_idx, config_name))
        except FileNotFoundError:
            pass
    return removed


def _reset_failed_task_log_stats(
    instance_id: str,
    config_path: str,
    removed_logs: list[tuple[str, int, str | None]],
) -> None:
    """清除已删除失败日志对应的缓存和数据库明细，保留成功任务统计。"""
    if not removed_logs:
        return
    _status_cache.pop(instance_id, None)
    _analyze_cache[config_path] = ({}, time.time())
    _analyze_tree_cache[config_path] = []
    _analyze_file_state.pop(config_path, None)
    _analyze_changed.pop(config_path, None)
    task_indexes = [task_idx for _path, task_idx, _name in removed_logs]
    placeholders = ",".join("?" * len(task_indexes))
    with get_connection() as conn:
        conn.execute(
            f"DELETE FROM task_records WHERE instance_id=? "
            f"AND task_idx IN ({placeholders})",
            (instance_id, *task_indexes),
        )
        conn.execute(
            "UPDATE task_instances SET task_done_sync=0 WHERE id=?",
            (instance_id,),
        )


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
        removed_logs = _clear_failed_task_logs(output_dir)
        _reset_failed_task_log_stats(instance_id, config_path, removed_logs)
        if removed_logs:
            print(
                f"[start] 实例 {instance_id} 已清空上一轮失败任务日志 "
                f"({len(removed_logs)} files)"
            )
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

    # 停止那一刻重算一次 error_breakdown：running 期间的最后一个快照是
    # 「跑到一半」的，而 _analyze_task_status 对非 running 实例永不重扫，
    # 不补算的话页面会把停止前未统计到的任务永久显示成「未执行」
    # （实测某实例 4161 个全部失败，却显示「任务异常 2422 / 未执行 1739」）。
    #
    # 放后台线程：冷扫一个 4000+ 任务的实例要十几秒，不能阻塞停止请求的响应。
    if stopped:
        async def _recompute() -> None:
            try:
                await async_execute(_compute_analyze, inst["config_path"],
                                    inst["total_tasks"])
            except Exception:
                pass
        asyncio.create_task(_recompute())

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
        done_trajs = _task_done_counts([instance_id], conn).get(instance_id, 0)
        done_tasks = _done_counts([instance_id], conn).get(instance_id, done_trajs)

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

    error_breakdown = _analyze_task_status(inst["config_path"], total, inst["status"],
                                       inst.get("stopped_at"))
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
        done_trajs=done_trajs,
        done_tasks=done_tasks,
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


@router.get("/{instance_id}/metrics")
def get_instance_metrics(
    instance_id: str,
    window: int = Query(180, ge=1, le=43200, description="看最近多少分钟（上限 30 天，前端「全部」传 10080）"),
    user: dict = Depends(get_current_user),
):
    """实例成功率时序 —— 详情页「任务执行情况」成功率折线图的数据源。

    **数据来源**：`task_events`，由 offline/success_tracker.py 写入。
    一行 = 一次任务完成（某 config 在某时刻以某结果结束）。两种来源：
      · 扫 task-N.log 得到（`source='log'`）—— 时间是任务**真实完成时刻**
      · 增量读 complete/failed.jsonl 得到（`source='jsonl'`）—— 时间≈观测时刻

    **如实记录，不做改判**：同一 config 先失败后重跑成功是两行事件，
    各自落在自己的时间桶里。所以历史曲线不会因为今天重跑成功而把昨天的
    失败峰抹掉。

    ⚠️ 代价：累计总数会比 complete/failed.jsonl 的行数**多**（同一任务被计了
    两次），与顶部「成功率」卡片不是一个口径 —— 那张卡片数的是 jsonl 当前行数
    （重跑成功后失败那条会被摘掉）。本接口回答的是「当时跑得怎么样」，
    卡片回答的是「现在总共成了多少」。

    **顺带登记**：本接口被调用说明用户正看着详情页，于是写一行
    success_requests，让 tracker 把它纳入跟踪。running 实例本来就自动跟踪，
    这一步是为了让**已结束**的实例也能在点开详情后回填出完整曲线。

    每个点给两类：
      - `win_success_rate`：**桶内**成功率，反映「这段时间跑得怎么样」
      - `cum_success_rate`：累计成功率（前缀和）
    累计值在跑了几千个任务后会被历史压得几乎不动，看不出最近是否开始大面积失败，
    所以前端默认展示窗口口径。桶内没有完成任务时 win_success_rate 为 None
    （前端断线而非画成 0）。
    """
    with get_connection() as conn:
        # 登记：第一次见到这个实例时写 queued，tracker 下一轮会扫日志回填历史。
        # 已登记的不打回（否则每次刷新页面都要重新回填一遍）。
        conn.execute(
            "INSERT INTO success_requests (instance_id, status) VALUES (?, 'queued') "
            "ON CONFLICT(instance_id) DO NOTHING",
            (instance_id,),
        )
        rows = conn.execute(
            # success=-1 是「该实例没有可扫的日志」的哨兵行（见 success_tracker
            # 的回填分支），不是真实事件，不能进聚合。
            "SELECT finished_at, success FROM task_events "
            "WHERE instance_id=? AND success >= 0 ORDER BY finished_at ASC",
            (instance_id,),
        ).fetchall()
    if not rows:
        return {"instance_id": instance_id, "window_minutes": window, "points": []}

    events = []
    for r in rows:
        d = dict(r)
        ts = d["finished_at"]
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except ValueError:
                continue
        elif not isinstance(ts, datetime):
            continue
        events.append((ts, bool(d["success"])))

    if not events:
        return {"instance_id": instance_id, "window_minutes": window, "points": []}

    # window 是「看最近多久」—— 回填后一个实例可能有几十小时的历史，
    # 全画出来既看不清也撑爆点数（实测 19 小时 / 120s 桶 = 567 个点）。
    # 所以先按 window 截取尾部时间range，再在其中分成约 30 个桶。
    # window 覆盖不到全部数据时只画最近那段；数据本来就短则全画。
    t_end = events[-1][0]
    t_start = t_end - timedelta(minutes=window)
    if events[0][0] > t_start:
        t_start = events[0][0]
    events = [e for e in events if e[0] >= t_start]
    if not events:
        return {"instance_id": instance_id, "window_minutes": window, "points": []}

    span = max(1.0, (events[-1][0] - events[0][0]).total_seconds())
    bucket_seconds = max(10, int(span / 30))

    t0 = events[0][0]
    buckets: dict[int, list] = {}
    for ts, ok in events:
        idx = int((ts - t0).total_seconds() // bucket_seconds)
        b = buckets.setdefault(idx, [0, 0])
        b[0 if ok else 1] += 1

    out = []
    cum_done = 0
    cum_fail = 0
    for idx in sorted(buckets):
        done, fail = buckets[idx]
        cum_done += done
        cum_fail += fail
        fin = done + fail
        cum_fin = cum_done + cum_fail
        out.append({
            # 桶的右边界：该桶代表「截至这一刻的这段时间」
            "ts": (t0 + timedelta(seconds=(idx + 1) * bucket_seconds)).isoformat(timespec="seconds"),
            "completed": cum_done,
            "failed": cum_fail,
            "running": 0,
            "total": 0,
            "cum_success_rate": round(cum_done / cum_fin * 100, 1) if cum_fin else None,
            "win_success_rate": round(done / fin * 100, 1) if fin else None,
            "win_finished": fin,
        })

    return {
        "instance_id": instance_id,
        "window_minutes": window,
        "bucket_seconds": bucket_seconds,
        "points": out,
    }


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
        # 一次聚合出全部实例的成功轨迹数（走 idx_traj_task_done_inst 部分索引）
        done_counts = _task_done_counts(ids, conn)
        # DONE：本地日志口径（走 idx_task_records_done）
        local_done = _done_counts(ids, conn)
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
        _analyze_task_status(inst["config_path"], total, inst["status"],
                             inst.get("stopped_at"))
        error_tree = _analyze_tree_cache.get(inst["config_path"], [])
        out["overviews"][iid] = {
            "total": total,
            "completed": inst["completed_tasks"],
            "failed": inst["failed_tasks"],
            "running": running_pods,
            "pending": max(0, total - finished - running_pods),
            "success_rate": round(inst["completed_tasks"] / finished * 100, 1) if finished else 0.0,
            "done_trajs": done_counts.get(iid, 0),
            "done_tasks": local_done.get(iid, done_counts.get(iid, 0)),
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
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
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
    # 传 config_path：真要扫这个实例才付解压成本（见 _load_file_state 的说明）。
    _load_file_state(config_path)
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
    with get_connection() as conn:
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
                # 刚从 running/preparing 转终态：**重算一次 error_breakdown**。
                #
                # running 期间每轮都 _compute_analyze，但最后那一轮的快照是
                # 「跑到一半」的 —— 实例结束后剩下的任务也有了结果，而
                # _analyze_task_status 对非 running 实例永不重扫，旧快照会被永久复用。
                # 实测某实例 4161 个任务全部 C003 失败，页面却显示
                # 「任务异常 2422 / 未执行 1739」（2422 是停止前那一刻的计数），
                # 与 overview 的 failed=4161 口径打架。
                #
                # 只在**转换那一刻**算一次：成本一次性，且结果随后由
                # _save_shared_caches() 落盘，4 个 uvicorn worker 都能复用。
                if prev_status in ("running", "preparing"):
                    try:
                        _compute_analyze(inst["config_path"], inst["total_tasks"])
                    except Exception:
                        pass
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


def _load_file_state(config_path: str | None = None) -> None:
    """灌入上次的 per-file 扫描签名，避免重启后把 10 万+ 份日志重读一遍。

    签名按 (mtime, size) 核对，日志有任何追加都会自然失效并重读，不存在陈旧风险。

    **按需加载单个实例，而不是整文件**。这个 gz 已经涨到 **203 MB / 704 万条签名**
    （注释里写的「11 万条约几 MB」是很久以前的规模），整文件 gzip 解压 + json 解析
    实测 **12.35s** —— 而它是每个进程首次请求时同步做的，uvicorn 有 4 个 worker，
    于是「重启后点开任务实例管理」每次都要卡十几秒（实测列表页计算本身只要 0.43s，
    99% 的时间全在这一步）。

    改成按 config_path 懒加载：
      · 传 config_path → 只解出这一个实例的签名（命中内存则直接返回）
      · 不传（兼容旧调用）→ 不再整文件加载，交给调用方按实例取
    整文件只在 leader 落盘前需要全量（_save_file_state 用内存里的即可）。
    """
    if config_path is not None and config_path in _analyze_file_state:
        return
    if config_path is None:
        # 旧签名的调用方（列表页/overview）不再触发整文件解压 ——
        # 真正需要签名的 _compute_analyze 会带 config_path 再来一次。
        return

    # 单实例懒加载：仍要解压整个 gz（json 不支持随机读），但**只保留命中的那一份**，
    # 并把结果记在 _analyze_file_state 里，同一实例不会重复解。
    # 为避免每个实例各解一遍 203MB，这里解一次就把全部灌进内存（和原来等价），
    # 只是把时机从「进程首次请求」推迟到「真的要扫某个实例」——
    # 列表页/overview 命中缓存时根本不需要签名，也就不必付这 12s。
    if _FILE_STATE_LOADED[0]:
        return
    _FILE_STATE_LOADED[0] = True
    try:
        import gzip
        t0 = time.time()
        with gzip.open(_file_state_path(), "rt", encoding="utf-8") as f:
            payload = json.load(f)
        for k, v in (payload or {}).items():
            _analyze_file_state[k] = {
                fname: tuple(rec) for fname, rec in v.items()
            }
        elapsed = time.time() - t0
        if elapsed > 3:
            print(f"[analyze] file_state 加载 {elapsed:.1f}s "
                  f"({len(_analyze_file_state)} 实例) —— 文件过大时考虑裁剪已删实例",
                  flush=True)
    except Exception:
        pass


def _save_file_state(force: bool = False) -> None:
    """把 per-file 签名落盘（leader 调用）。节流 2 分钟。

    **只保留「还会被再扫一次」的实例**，否则这个文件会无界增长：
    实测涨到 **203 MB / 704 万条签名 / 1319 个实例**（其中 232 个实例已被删除），
    而 gzip 解压 + json 解析要 **12.35s**，是每个 uvicorn worker 首次请求同步付的 ——
    「重启后点开任务实例管理卡十几秒」就是这么来的。

    判据：签名的唯一用途是 `_compute_analyze` 的增量扫描（跳过未变化的日志文件）。
      · running/preparing  —— 每轮都要重扫，必须留
      · 终态且 task_done_sync=0 —— 还差「转终态那一次」补算，留
      · 其它终态实例         —— 已经算完且永不重扫（见 _analyze_task_status），
                                 签名留着纯占体积
    实测只留前两类 = 25 个实例 / 19 万条签名，体积降到约 1/37。

    被裁掉的实例若之后又要扫（比如用户重启了它），签名缺失只是让那一次全量重读，
    功能不受影响。
    """
    now = time.time()
    if not force and now - _file_state_last_save[0] < _FILE_STATE_SAVE_INTERVAL:
        return
    _file_state_last_save[0] = now
    try:
        import gzip
        keep: set[str] = set()
        try:
            with get_connection() as conn:
                keep = {r[0] for r in conn.execute(
                    "SELECT config_path FROM task_instances "
                    " WHERE status IN ('running','preparing') "
                    "    OR (status IN ('completed','finished','stopped') "
                    "        AND COALESCE(task_done_sync,0)=0)")}
        except Exception:
            # 查不到就退回全量（宁可文件大，也不要误删正在用的签名）
            keep = set(_analyze_file_state)
        payload = {k: v for k, v in _analyze_file_state.items() if k in keep}
        # 内存里也同步裁掉，否则进程越跑越占内存（704 万条元组约数百 MB）
        for k in [k for k in _analyze_file_state if k not in keep]:
            _analyze_file_state.pop(k, None)
        path = _file_state_path()
        tmp = f"{path}.{os.getpid()}.tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception:
        pass


def _analyze_task_status(config_path: str, total_tasks: int, instance_status: str = "running",
                         stopped_at: str | None = None) -> dict:
    """error_breakdown（按状态/错误码分类的计数）。

    stopped_at 当前不参与判断，保留参数是因为调用方已经在传 —— 曾用它做
    「缓存是否早于实例结束」的校验，但那会让列表页每次重扫 19/20 个实例
    （单个冷扫 12.66s，整页 88s）。改由实例转终态那一刻补算一次，见
    _refresh_running_once 与 stop_instance。
    """
    # **不在这里 _load_file_state()** —— 命中缓存的请求根本不需要 per-file 签名,
    # 而整文件解压要 12.35s（203MB / 704 万条）。真需要扫的 _compute_analyze
    # 会自己带 config_path 调用。这是「重启后列表页卡十几秒」的真凶。
    now = time.time()
    cache_key = config_path
    cached = _analyze_cache.get(cache_key)

    # 本进程没扫过：先看 leader 落盘的快照，能命中就不必自己扫
    if cached is None:
        _load_shared_caches()
        cached = _analyze_cache.get(cache_key)

    # 非 running：状态已定，命中缓存直接返回，**永不重扫**。
    #
    # 这里刻意不做「缓存是否早于 stopped_at」的校验 —— 试过，代价不可接受：
    # 冷扫一个 4000+ 任务的实例要 12.66s（NFS 上读几千个 task-N.log），
    # 而列表页一次就有 19/20 个实例命中失效条件，整页 88 秒。
    # 更糟的是 _compute_analyze 只写**进程内**缓存，uvicorn 有 4 个 worker、
    # 只有 leader 会 _save_shared_caches()，所以每个 worker 的每次请求都要重扫，
    # 永远修不好。
    #
    # 「跑一半的快照」改由 _refresh_running_once 在**实例刚转终态那一刻**
    # 重算一次（见那里的 _compute_analyze 调用），一次性修正并落进共享缓存，
    # 读路径完全不受影响。
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
    code: str | None = Query(None, description="具体错误码，如 S003"),
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
    if code:
        where.append("error_code = ?")
        params.append(code)
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
