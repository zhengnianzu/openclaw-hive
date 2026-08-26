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
    """请求浅层分级：登记该实例到 shallow_requests 表，worker 下一轮消费。

    点击驱动：worker 不再全量扫历史 finished 缺口实例（动辄数十万行追不完），
    只消费 running/preparing + 本表登记的 finished/completed 实例。幂等：重复登记只更新
    status/created_by，不动已有的处理进度。处理完成后 worker 删登记出队。

    output_status 门控：任务完成（completed）且 output_status='done' 时说明浅层已完成、
    结果可直接用 → 不再重复登记，直接返回；output_status IS NULL 才允许提交新浅层。
    """
    inst = _get_instance(instance_id)
    if inst.get("output_status") == "done":
        return {"instance_id": instance_id, "status": "done",
                "hint": "该实例浅层已完成(output_status=done)，直接使用，无需重复处理"}
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO shallow_requests (instance_id, created_by, status, created_at) "
            "VALUES (?, ?, 'queued', CURRENT_TIMESTAMP) "
            "ON CONFLICT(instance_id) DO UPDATE SET "
            "status='queued', created_by=excluded.created_by, created_at=CURRENT_TIMESTAMP",
            (instance_id, user.get("username")),
        )
    return {"instance_id": instance_id, "status": "queued",
            "hint": "已登记浅层请求，worker 下一轮消费，GET /shallow 观察进度"}


@router.get("/{instance_id}/shallow")
async def get_shallow(instance_id: str, user: dict = Depends(get_current_user)):
    """浅层漏斗：统计卡 + 会话表格行（一次性返回，前端分页过滤在本地做）。

    返回 {summary, rows}：summary 为 L0-L3 计数（traj_level 分组），rows 为全部
    task_records 行（含 traj_level 与 eval 列），前端按 level/tag 过滤 + 分页。
    """
    inst = _get_instance(instance_id)

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT tr.task_idx, tr.config_name, tr.traj_level, tr.status, tr.error_code, "
            "tr.error_category, tr.eval_score, tr.eval_completion, tr.gate, "
            "ttr.task_done, ttr.has_eval "
            "FROM task_records tr "
            "LEFT JOIN task_traj_records ttr "
            "  ON ttr.instance_id = tr.instance_id AND ttr.config_name = tr.config_name "
            "WHERE tr.instance_id=? ORDER BY tr.task_idx",
            (instance_id,),
        ).fetchall()
        rows = [dict(r) for r in rows]
        graded = [r for r in rows if r["traj_level"] in _TRAJ_LEVELS]

    summary = {lv: 0 for lv in _TRAJ_LEVELS}
    for r in graded:
        summary[r["traj_level"]] += 1
    summary["graded"] = len(graded)
    summary["total"] = len(rows)
    summary["task_done"] = sum(1 for r in rows if r.get("task_done"))

    # 浅层进度：已分级 + failed 占位 = 处理完；残留 NULL = 未处理。
    # finished 实例已完成分级（历史堆积）时 both = total，进度条隐藏。
    # unaudited = 仍为 NULL 的行数（浅层分析未覆盖/队列未消费）。
    unaudited = len(rows) - len(graded) - sum(
        1 for r in rows if r["traj_level"] == "failed")
    queued = False
    status = inst.get("status")
    if status in ("finished", "completed"):
        # finished/completed 实例是否被登记（worker 待处理或正在处理）
        with get_connection() as conn:
            queued = conn.execute(
                "SELECT 1 FROM shallow_requests WHERE instance_id=? LIMIT 1",
                (instance_id,),
            ).fetchone() is not None

    return {
        "instance_id": instance_id,
        "instance_status": status,
        "output_status": inst.get("output_status"),
        "total_tasks": inst.get("total_tasks"),
        "summary": summary,
        "rows": rows,
        # 浅层进度：processed = graded + failed（已定级）；undone = total - processed；
        # queued = finished/completed 且已登记（worker 待处理）；全部行处理后 undone=0。
        "progress": {
            "approved": len(graded),
            "failed": sum(1 for r in rows if r["traj_level"] == "failed"),
            "total": len(rows),
            "undone": unaudited,
            "queued": queued,
        },
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

# 漏斗等级（批量入队的合法 traj_level；failed / NULL 不入队）
_ENQUEUE_LEVELS = _TRAJ_LEVELS

# ⚠ 静态批量端点（enqueue_all/queue）必须定义在参数路由 {traj_name} 之前，
#   否则 FastAPI 按注册序匹配，POST /deep/enqueue_all 会被 trigger_deep 的
#   {traj_name} 捕获（traj_name="enqueue_all"）→ 404。


@router.post("/{instance_id}/deep/enqueue_all")
async def deep_enqueue_all(instance_id: str, user: dict = Depends(require_operator)):
    """把该实例所有已分级（L0-L3）且未下载的行批量置 pending 入队。

    与单会话 trigger_deep 同款幂等保护：
      - 已 downloading 的不打断（跳过）
      - 已有 assistant_traj_path（此前下载成功）的跳过，不重复拉 OBS
      - failed / level IS NULL 的行不参与（无轨迹或不可分级）
    返回 {queued, skipped_done, skipped_downloading, skipped_ungraded, total}。
    """
    inst = _get_instance(instance_id)
    level_ph = ",".join("?" for _ in _ENQUEUE_LEVELS)
    with get_connection() as conn:
        # 已 done 且已下载过（有路径）——批量入队时跳过，不重复下载
        skipped_done = conn.execute(
            "SELECT COUNT(*) FROM task_traj_records "
            "WHERE instance_id=? AND assistant_traj_path IS NOT NULL AND status='done'",
            (instance_id,),
        ).fetchone()[0]
        # 正在下载的——不打断
        skipped_downloading = conn.execute(
            "SELECT COUNT(*) FROM task_traj_records "
            "WHERE instance_id=? AND status='downloading'",
            (instance_id,),
        ).fetchone()[0]
        # 入队：L0-L3 且未下载（无路径）且未 downloading 的行 → pending
        cur = conn.execute(
            f"UPDATE task_traj_records SET status='pending', updated_at=CURRENT_TIMESTAMP "
            f"WHERE instance_id=? AND level IN ({level_ph}) "
            f"AND assistant_traj_path IS NULL AND status NOT IN ('downloading')",
            (instance_id, *_ENQUEUE_LEVELS),
        )
        conn.commit()
    queued = cur.rowcount or 0
    with get_connection() as conn:
        skipped_ungraded = conn.execute(
            "SELECT COUNT(*) FROM task_traj_records "
            "WHERE instance_id=? AND (level IS NULL OR level='failed') "
            "AND assistant_traj_path IS NULL",
            (instance_id,),
        ).fetchone()[0]
    return {
        "instance_id": instance_id,
        "queued": queued,
        "skipped_done": skipped_done,
        "skipped_downloading": skipped_downloading,
        "skipped_ungraded": skipped_ungraded,
        "total": queued + skipped_done + skipped_downloading + skipped_ungraded,
        "status": "queued" if queued else "noop",
        "hint": "worker 常驻消费 pending 行，GET /deep/{traj}/status 或 /deep/queue 观察进度",
    }


@router.get("/{instance_id}/deep/queue")
async def deep_queue(instance_id: str, user: dict = Depends(get_current_user)):
    """该实例深层下载队列状态（前端轮询）。

    返回 {summary, rows}：
      summary = {pending, downloading, done, failed, total}（该实例 task_traj_records 计数）
      rows    = 按「进行中优先」排序的行（pending/downloading 在前，再 failed/done），
                每行含 traj_name/level/status/updated_at/error/assistant_traj_path。
    """
    inst = _get_instance(instance_id)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT traj_name, level, status, error, assistant_traj_path, updated_at "
            "FROM task_traj_records WHERE instance_id=? "
            "ORDER BY CASE status "
            "           WHEN 'pending' THEN 0 WHEN 'downloading' THEN 1 "
            "           WHEN 'failed' THEN 2 ELSE 3 END, updated_at DESC",
            (instance_id,),
        ).fetchall()
        rows = [dict(r) for r in rows]
        summary = {"pending": 0, "downloading": 0, "done": 0, "failed": 0}
        for r in rows:
            st = r["status"]
            if st in summary:
                summary[st] += 1
        summary["total"] = len(rows)

    return {"instance_id": instance_id, "summary": summary, "rows": rows}


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
        # traj_name 在 rel 中第一次出现处：任务目录 = cache_root + 此前缀。
        # 注：obsutil cp -r 会保留 OBS leaf 目录 → 深层下载后本地是 <batch>/<leaf>/<leaf>/… 双层嵌套，
        #     assistant_traj_path 指向子层；但日志（run.log/gateway.log/evaluator_use.log）落在父层 dest。
        #     故取「第一次出现」（=父层任务根）而非最后一次（=子层），否则读不到父层日志。
        #     浅层 trajectory_rel 为 <batch>/<leaf>/agents/… 时第一次出现即任务根，同样正确。
        idx = rparts.index(tr["traj_name"]) if tr["traj_name"] in rparts else -1
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
        detail = oa.load_task_detail(task_dir, traj_name,
                                     known_harness=tr.get("harness"))
        # 本地是否有真实 assistant 轨迹文件（多候选取最大者；任务确无回复时 OBS 无轨迹，为 None）
        has_traj_file = bool(oa.find_primary_assistant_trajectory(task_dir))
        if has_traj_file:
            # 轨迹文件在但 stats 不可解析，且无 tsr 兜底 → 真异常
            if not detail.get("assistant_stats") and not oa.read_tsr_stats(task_dir):
                raise HTTPException(status_code=500,
                                    detail=f"本地详情解析失败: 缓存目录 {task_dir} 无轨迹也无 stats")
        else:
            # 本地无轨迹文件。区分两种情形：
            #   deep 已下载过（worker 会把可见产物回填到路径列；shallow 只拉 tsr 不碰主 log，
            #   故仅浅层快照时五项路径列全 NULL 且本地无主 log）→ 任务确无 assistant 轨迹
            #   （工具全失败/无助手回复）→ 不 409，tsr 兜底展示，前端渲染"无 assistant 轨迹"。
            #   未触发 deep、无任何深层产物 → 本地仅 tsr 浅层快照 → 409 触发下载。
            # 注：task_done 不作判据——它标"任务是否完成"，而 done 行里大量 task_done=0
            #   的浅层产物同样无文件、轨迹仍在 OBS，仍需触发下载。
            deep_ran = any(tr.get(k) for k in (
                "assistant_traj_path", "evaluator_traj_path",
                "task_log_path", "gateway_log_path", "eval_log_path"))
            if deep_ran or oa.find_primary_log(task_dir, traj_name):
                if not detail.get("assistant_stats") and not oa.read_tsr_stats(task_dir):
                    raise HTTPException(status_code=500,
                                        detail=f"本地详情解析失败: 缓存目录 {task_dir} 无轨迹也无 stats")
            else:
                raise HTTPException(status_code=409,
                                    detail="本地轨迹未下载，请先加载详情触发下载")
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

    # 深层下载状态：5 个路径列任一非空（worker 已回填）或本地轨迹文件可读 = 已下载；
    # 全 NULL 且无本地轨迹文件 = 未下载。未下载但 harness/stats 可读（DB+tsr）时仍返回
    # detail，前端据 deep_status 触发下载。
    deep_paths = [tr.get(k) for k in (
        "assistant_traj_path", "evaluator_traj_path",
        "task_log_path", "gateway_log_path", "eval_log_path")]
    deep_status = ("downloaded" if (any(deep_paths) or has_traj_file)
                   else "not_downloaded")

    return {
        "traj_name": traj_name,
        "harness": tr.get("harness") or detail.get("harness"),
        "deep_status": deep_status,
        "deep_error": tr.get("error"),
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
