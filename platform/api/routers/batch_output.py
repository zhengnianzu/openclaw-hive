# -*- coding: utf-8 -*-
"""批跑作业「输出」页端点：只读写 platform.db（task_records + task_traj_records），不碰 obsutil。

数据源（README_outputview.md §二-0）：复用 platform.db，不另起 hive_output.db。
  - 浅层 = task_records（唯一事实表）：漏斗分布按 traj_level SQL 聚合；会话表格直接查行。
  - 深层 = task_traj_records：行由 offline/output_worker.py 回填分级；用户点开详情时
    POST 置 status='pending'，worker 消费后回填 5 个路径列（status → done/failed）。

全部端点同步实现（async_execute 跑在专用线程池），绝不阻塞事件循环，也不触发 OBS 下载。
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.config import settings
from ..core.database import get_connection, async_execute, async_query, async_query_one
from ..core.security import get_current_user, require_operator

router = APIRouter(prefix="/api/instances", tags=["batch-output"])

# 漏斗档位（x 轴顺序，与 README 一致）
_TRAJ_LEVELS = ["L0", "L1", "L1.5", "L2", "L3"]

# 会话表格允许的排序列（防 SQL 注入）——task_records 的实际列
_SORTABLE = {
    "task_idx", "config_name", "traj_level", "status", "eval_score", "eval_completion",
}


def _get_instance(instance_id: str) -> dict:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")
    return dict(row)


# ============ 浅层 ============

@router.post("/{instance_id}/shallow")
async def trigger_shallow(instance_id: str, user: dict = Depends(require_operator)):
    """请求浅层分级：把该实例 task_records 中 traj_level 为 NULL 的行重新暴露给 worker 队列。

    实际是幂等"重扫"请求——worker 常驻轮询自然消费 traj_level IS NULL 的行；分级完成后
    traj_level 被回填，行自然出队。前端 10s 轮询 GET 观察结果即可，无需 await 完成。
    """
    inst = _get_instance(instance_id)
    with get_connection() as conn:
        conn.execute(
            "UPDATE task_records SET traj_level=NULL "
            "WHERE instance_id=? AND traj_level IS NOT NULL",
            (instance_id,),
        )
    return {"instance_id": instance_id, "status": "queued",
            "hint": "worker 常驻轮询消费，GET /shallow 观察进度"}


@router.get("/{instance_id}/shallow")
async def get_shallow(instance_id: str, user: dict = Depends(get_current_user)):
    """浅层漏斗：统计卡 + 会话表格行（一次性返回，前端分页过滤在本地做）。

    返回 {summary, rows}：summary 为 L0-L3 计数（traj_level 分组），rows 为全部
    task_records 行（含 traj_level 与 eval 列），前端按 level/tag 过滤 + 分页。
    """
    inst = _get_instance(instance_id)

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT task_idx, config_name, traj_level, status, error_code, error_category, "
            "eval_score, eval_completion, gate "
            "FROM task_records WHERE instance_id=? ORDER BY task_idx",
            (instance_id,),
        ).fetchall()
        rows = [dict(r) for r in rows]
        graded = [r for r in rows if r["traj_level"] in _TRAJ_LEVELS]

    summary = {lv: 0 for lv in _TRAJ_LEVELS}
    for r in graded:
        summary[r["traj_level"]] += 1
    summary["graded"] = len(graded)
    summary["total"] = len(rows)
    summary["task_done"] = 0

    return {
        "instance_id": instance_id,
        "instance_status": inst.get("status"),
        "total_tasks": inst.get("total_tasks"),
        "summary": summary,
        "rows": rows,
    }


@router.get("/{instance_id}/shallow/tasks")
async def shallow_tasks(
    instance_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    tag: str | None = Query(None, description="L0/L1/L1.5/L2/L3/done/fail/unevaluated"),
    sort_by: str = Query("task_idx"),
    sort_dir: str = Query("asc"),
    user: dict = Depends(get_current_user),
):
    """会话表格数据（分页 + 标签过滤 + 排序）。"""
    inst = _get_instance(instance_id)

    where = ["instance_id = ?"]
    params: list = [instance_id]
    if tag:
        if tag == "done":
            where.append("traj_level IS NOT NULL")
        elif tag == "fail":
            where.append("traj_level = 'failed'")
        elif tag == "unevaluated":
            where.append("traj_level IS NULL")
        elif tag in _TRAJ_LEVELS:
            where.append("traj_level = ?")
            params.append(tag)
    where_sql = " AND ".join(where)

    col = sort_by if sort_by in _SORTABLE else "task_idx"
    direction = "DESC" if str(sort_dir).lower() == "desc" else "ASC"

    total_row = await async_query_one(
        f"SELECT COUNT(*) AS n FROM task_records WHERE {where_sql}", tuple(params)
    )
    total = total_row["n"] if total_row else 0
    offset = (page - 1) * page_size
    tasks = await async_query(
        f"SELECT task_idx, config_name, traj_level, status, error_code, error_category, "
        f"eval_score, eval_completion, gate "
        f"FROM task_records WHERE {where_sql} ORDER BY {col} {direction} LIMIT ? OFFSET ?",
        tuple(params) + (page_size, offset),
    )
    return {"total": total, "page": page, "page_size": page_size, "tasks": tasks}


# ============ 深层 ============

@router.post("/{instance_id}/deep/{traj_name}")
async def trigger_deep(instance_id: str, traj_name: str,
                       user: dict = Depends(require_operator)):
    """请求深层加载：该 task 的 task_traj_records 行置 pending，worker 下载并回填路径列。"""
    inst = _get_instance(instance_id)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, status, assistant_traj_path FROM task_traj_records "
            "WHERE instance_id=? AND traj_name=?",
            (instance_id, traj_name),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404,
                                detail=f"无轨迹记录: {traj_name}（浅层分级可能尚未完成）")
        # 路径列已齐全（此前下载成功过）→ 恢复 done，不重复下载
        if row["assistant_traj_path"]:
            conn.execute(
                "UPDATE task_traj_records SET status='done', updated_at=CURRENT_TIMESTAMP "
                "WHERE id=?",
                (row["id"],),
            )
            return {"instance_id": instance_id, "traj_name": traj_name,
                    "status": "done"}
        # 已在 downloading/进行中则幂等跳过（不打断正在下载的）
        cur = conn.execute(
            "UPDATE task_traj_records SET status='pending', updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status NOT IN ('downloading')",
            (row["id"],),
        )
        already = cur.rowcount == 0
    return {"instance_id": instance_id, "traj_name": traj_name,
            "status": "queued" if not already else "in_progress"}


@router.get("/{instance_id}/deep/{traj_name}/status")
async def deep_status(instance_id: str, traj_name: str,
                      user: dict = Depends(get_current_user)):
    """深层加载状态：前端 3s 轮询。done 时附带 5 个缓存路径。"""
    inst = _get_instance(instance_id)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT traj_name, status, error, assistant_traj_path, evaluator_traj_path, "
            "task_log_path, gateway_log_path, eval_log_path "
            "FROM task_traj_records WHERE instance_id=? AND traj_name=?",
            (instance_id, traj_name),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"无轨迹记录: {traj_name}")
    return dict(row)


@router.get("/{instance_id}/deep/{traj_name}/detail")
async def deep_detail(instance_id: str, traj_name: str,
                      user: dict = Depends(get_current_user)):
    """读取本地缓存的详情（assistant/evaluator 轨迹块 + 主 log/gateway/eval log 尾部）。

    需先 POST /deep/{traj_name} 触发下载、worker 状态 done 后调用；本地缓存无数据返回 409，
    前端据 status 轮询后重试。只读 output_cache/<batch>/<traj_name>/，不触发 OBS 下载。
    """
    inst = _get_instance(instance_id)
    with get_connection() as conn:
        tr = conn.execute(
            "SELECT * FROM task_traj_records WHERE instance_id=? AND traj_name=?",
            (instance_id, traj_name),
        ).fetchone()
    if not tr:
        raise HTTPException(status_code=404, detail=f"无轨迹记录: {traj_name}")
    tr = dict(tr)

    cache_root = os.path.abspath(settings.OUTPUT_CACHE)
    # 任务目录判定优先级：
    #   1. assistant_traj_path（worker 深层下载后回填的缓存绝对路径，obsutil cp 保留
    #      OBS leaf 目录 → "<batch>/<leaf>/<leaf>/projects/..."，取 traj_name 最后出现处前缀）
    #   2. trajectory_rel（浅层写入的 OBS 路径，<batch...>/<leaf>/...，取 leaf 前缀）
    task_dir = None
    for rel in (tr.get("assistant_traj_path"), tr.get("trajectory_rel")):
        if not rel:
            continue
        rparts = (rel.replace(cache_root, "", 1).lstrip("/") if rel.startswith(cache_root)
                  else rel).split("/")
        # traj_name 在 rel 中最后一次出现：任务目录 = cache_root + 此前缀
        idx = len(rparts) - 1 - rparts[::-1].index(tr["traj_name"]) if tr["traj_name"] in rparts else -1
        if idx >= 0:
            candidate = os.path.join(cache_root, *rparts[:idx + 1])
            if os.path.isdir(candidate):
                task_dir = candidate
                break
    if task_dir is None:
        # 退化：直接以 traj_name 为目录名
        candidate = os.path.join(cache_root, tr["traj_name"])
        if os.path.isdir(candidate):
            task_dir = candidate
    if task_dir is None:
        raise HTTPException(status_code=409,
                            detail="本地缓存不存在，请先 POST /deep/{traj_name} 触发下载")

    try:
        from src import offline_analysis as oa
        detail = oa.load_task_detail(task_dir, traj_name)
        # 轨迹 jsonl 缺失（OBS 已清理/仅 tsr+log 浅层产物）→ 用 tsr 兜底，视为可展示详情
        if not detail.get("assistant_stats") and not oa.read_tsr_stats(task_dir):
            raise HTTPException(status_code=500,
                                detail=f"本地详情解析失败: 缓存目录 {task_dir} 无轨迹也无 stats")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"本地详情解析失败: {str(e)[:200]}")

    # 路径列再确认（trajectory_rel 可能因缓存目录结构偏差需调整）
    if not tr.get("assistant_traj_path"):
        for t in detail.get("trajectories") or []:
            if t.get("role") == "assistant" and not tr.get("assistant_traj_path"):
                tr["assistant_traj_path"] = t["path"]
            elif t.get("role") == "evaluator" and not tr.get("evaluator_traj_path"):
                tr["evaluator_traj_path"] = t["path"]

    return {
        "traj_name": traj_name,
        "harness": detail.get("harness"),
        "assistant_stats": detail.get("assistant_stats"),
        "assistant_trajectory": detail.get("assistant_trajectory"),
        "evaluator_trajectory": detail.get("evaluator_trajectory"),
        "trajectories": detail.get("trajectories"),
        "log": detail.get("log"),
        "gateway": detail.get("gateway"),
        "eval_use_log": detail.get("eval_use_log"),
        "verdict": detail.get("verdict"),
        "paths": {
            "assistant_traj_path": tr.get("assistant_traj_path"),
            "evaluator_traj_path": tr.get("evaluator_traj_path"),
            "task_log_path": tr.get("task_log_path"),
            "gateway_log_path": tr.get("gateway_log_path"),
            "eval_log_path": tr.get("eval_log_path"),
        },
    }
