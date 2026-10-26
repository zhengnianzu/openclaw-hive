# -*- coding: utf-8 -*-
"""Worker 调度监控（资源看板「调度监控」区）。

数据来源全部是**实测或已落库的事实**，不做估算包装：
  - worker 存活/CPU/内存：pid 文件 + psutil（缺失则降级为读 /proc）
  - 每轮进度：worker_heartbeats 表（worker 每轮写一行）
  - 队列积压：task_traj_records（产物 artifacts_status + 轨迹 traj_status）/ task_records
  - 磁盘：shutil.disk_usage + 目录占用统计
  - ETA：按最近心跳的实测吞吐推算，吞吐为 0 时明确返回 null

约定：任何一项取不到都降级为 null 并带 available=false，绝不因单项失败让整个接口 500。
"""
from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query

from ..core.config import settings
from ..core.database import async_execute, get_connection
from ..core.security import require_operator

router = APIRouter(prefix="/api/worker-monitor", tags=["worker-monitor"])

# pid 文件固定白名单，路径不接受外部输入（避免被当成任意 pid 探测接口）
WORKER_PID_FILES = {
    "shallow": "worker.pid",          # --shallow-only
    "deep": "deep_worker.pid",        # --deep-only（含产物下载）
    "realtime": "realtime_worker.pid",  # --realtime-only（只下用户点击的）
}
PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WORKER_LABEL = {
    "shallow": "浅层分级",
    "deep": "深层下载 / 产物",
    "realtime": "实时下载（点击优先）",
    "artifacts": "产物下载",
    "mixed": "混合",
}

# 心跳超过这个秒数没更新，判为「可能卡住」。
# 这只是**下限**：心跳是每轮结束时才写一次，所以判定阈值必须跟「上一轮实际跑了多久」
# 挂钩，否则单轮耗时长的 worker 会在正常工作期间被反复误报。
# 实测浅层单轮 746s / 925s / 1625s / 1240s（12~27 分钟），而旧阈值固定 300s，
# 于是每轮跑到第 5 分钟就显示「疑似卡住」，一直红到该轮结束 —— 纯误报。
HEARTBEAT_STALE_SECONDS = 300
# 阈值 = max(下限, 上轮实测耗时 × 该倍数)。1.5 倍留出正常的轮间抖动余量。
HEARTBEAT_STALE_FACTOR = 1.5


def _stale_limit(hb: dict | None) -> float:
    """该 worker 的「卡住」判定阈值（秒），随上一轮实测耗时自适应。

    只用最后一条心跳的 duration：worker 卡死时 duration 为空或很小，
    阈值自然回落到下限 300s，仍然能报出来。
    """
    dur = (hb or {}).get("duration")
    if not dur or dur <= 0:
        return HEARTBEAT_STALE_SECONDS
    return max(HEARTBEAT_STALE_SECONDS, float(dur) * HEARTBEAT_STALE_FACTOR)


# ── 进程指标 ────────────────────────────────────────────────────────────────

def _read_pid(name: str) -> int | None:
    path = os.path.join(PLATFORM_DIR, WORKER_PID_FILES[name])
    try:
        with open(path) as f:
            pid = int(f.read().strip())
        return pid if pid > 0 else None
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """与 instances.py 的 _is_pid_running 同口径：僵尸进程算死。"""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
        state = data[data.rindex(b")") + 2:].split(b" ", 1)[0].decode()
        return state != "Z"
    except OSError:
        return False


def _proc_metrics(pid: int) -> dict:
    """进程 CPU% / RSS / 启动时间。psutil 缺失时降级读 /proc。"""
    out = {"available": False, "cpu_percent": None, "memory_mb": None,
           "started_at": None, "uptime_seconds": None}
    try:
        import psutil
    except ImportError:
        psutil = None

    if psutil is not None:
        try:
            p = psutil.Process(pid)
            with p.oneshot():
                out["cpu_percent"] = round(p.cpu_percent(interval=None), 1)
                out["memory_mb"] = round(p.memory_info().rss / 1048576, 1)
                ct = p.create_time()
                out["started_at"] = datetime.fromtimestamp(ct).isoformat(timespec="seconds")
                out["uptime_seconds"] = round(time.time() - ct, 0)
                out["available"] = True
            return out
        except Exception:
            pass

    # 降级：读 /proc/<pid>/stat（field 22 = starttime，单位 clock tick）
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
        parts = stat[stat.rindex(")") + 2:].split()
        start_ticks = int(parts[19])
        hz = os.sysconf("SC_CLK_TCK")
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        started = time.time() - (uptime - start_ticks / hz)
        out["started_at"] = datetime.fromtimestamp(started).isoformat(timespec="seconds")
        out["uptime_seconds"] = round(time.time() - started, 0)
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    out["memory_mb"] = round(int(line.split()[1]) / 1024, 1)
                    break
        out["available"] = True
    except Exception:
        pass
    return out


# ── 心跳 ────────────────────────────────────────────────────────────────────

def _latest_heartbeat(worker: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM worker_heartbeats WHERE worker=? "
            "ORDER BY id DESC LIMIT 1", (worker,)).fetchone()
    return dict(row) if row else None


def _heartbeat_age(hb: dict | None) -> float | None:
    """最后一轮心跳距今多少秒。启动标记行（round_start=0）不算活动。"""
    if not hb:
        return None
    ts = hb.get("round_end") or None
    if not ts:
        return None
    if hb.get("round_start") == 0:
        # 启动标记：用 created_at 的 wall clock（SQLite UTC 字符串）
        try:
            created = datetime.fromisoformat(hb["created_at"])
            return (datetime.utcnow() - created).total_seconds()
        except Exception:
            return None
    return max(0.0, time.time() - float(ts))


# ── 端点 ────────────────────────────────────────────────────────────────────

@router.get("/overview")
async def overview(user: dict = Depends(require_operator)):
    """worker 状态 + 队列积压 + 磁盘。"""
    workers = []
    for name in WORKER_PID_FILES:
        pid = _read_pid(name)
        alive = bool(pid) and _pid_alive(pid)
        hb = _latest_heartbeat(name)
        age = _heartbeat_age(hb)
        # 阈值跟着上一轮实测耗时走，避免单轮 20 分钟的 worker 在正常跑的时候被误报
        limit = _stale_limit(hb)
        item = {
            "worker": name,
            "label": WORKER_LABEL.get(name, name),
            "pid": pid,
            "alive": alive,
            "state": ("offline" if not alive
                      else ("stalled" if (age is not None and age > limit)
                            else "running")),
            "stale_after_seconds": round(limit),
            "cpu_percent": None,
            "memory_mb": None,
            "started_at": None,
            "uptime_seconds": None,
            "last_round_at": (datetime.fromtimestamp(hb["round_end"]).isoformat(timespec="seconds")
                              if hb and hb.get("round_end") else None),
            "last_duration": hb.get("duration") if hb else None,
            "last_processed": hb.get("processed") if hb else None,
            "last_failed": hb.get("failed") if hb else None,
            "last_note": hb.get("note") if hb else None,
            "heartbeat_age_seconds": round(age, 0) if age is not None else None,
        }
        if alive and pid:
            m = _proc_metrics(pid)
            item.update({k: m[k] for k in
                         ("cpu_percent", "memory_mb", "started_at", "uptime_seconds")})
            item["metrics_available"] = m["available"]
        else:
            item["metrics_available"] = False
        workers.append(item)

    # 如果 deep worker 也跑产物，额外标出来；单独的 artifacts 进程（--artifacts-only）没有 pid 文件
    art_hb = _latest_heartbeat("artifacts")
    if art_hb:
        age = _heartbeat_age(art_hb)
        art_limit = _stale_limit(art_hb)
        workers.append({
            "worker": "artifacts", "label": WORKER_LABEL["artifacts"], "pid": art_hb.get("pid"),
            "alive": bool(art_hb.get("pid")) and _pid_alive(int(art_hb["pid"])),
            "state": "running" if (age is not None and age <= art_limit) else "offline",
            "stale_after_seconds": round(art_limit),
            "cpu_percent": None, "memory_mb": None, "started_at": None, "uptime_seconds": None,
            "last_round_at": (datetime.fromtimestamp(art_hb["round_end"]).isoformat(timespec="seconds")
                              if art_hb.get("round_end") else None),
            "last_duration": art_hb.get("duration"),
            "last_processed": art_hb.get("processed"),
            "last_failed": art_hb.get("failed"),
            "last_note": art_hb.get("note"),
            "heartbeat_age_seconds": round(age, 0) if age is not None else None,
            "metrics_available": False,
        })

    disk = None
    try:
        usage = shutil.disk_usage(PLATFORM_DIR)
        disk = {
            "total_bytes": usage.total, "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_percent": round(usage.used / usage.total * 100, 1) if usage.total else None,
            # 产物占用取 DB 记账值（SUM(artifact_bytes)），不再 walk OUTPUT_CACHE。
            #
            # 原来这里调 _dir_size_cached 做 NFS 全量遍历：实测稳定吃满 5 秒墙钟
            # 上限后返回 None —— 每轮白烧 5 秒、一个字节都算不出来（539 个批次目录、
            # 5 秒只走到 1,334 文件 / 4,431 目录）。而且 uvicorn 是 --workers 4，
            # 每个进程各有独立的模块级缓存，请求随机落点 → 约 4/5 的请求打冷缓存，
            # 整个 /overview 就被这 5 秒拖到 6.2 秒，看板一进去先卡 6 秒。
            #
            # DB 里本来就有交付物下载的记账（artifact_bytes），0.07 秒聚合出
            # 46 GiB，与目录口径一致，且是**准确的交付物**体积（不含深层详情等
            # 其它缓存）。磁盘总量上面已经由 disk_usage 给了。
            "output_cache_bytes": await async_execute(_artifact_bytes_total),
        }
    except OSError:
        disk = None

    counts = await async_execute(_queue_counts)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "workers": workers,
        "disk": disk,
        "queue": counts,
    }


def _artifact_bytes_total() -> int:
    """已下载交付物的总体积（字节），取自 DB 记账。

    替代原来对 OUTPUT_CACHE 的 NFS 全量遍历：那次遍历实测耗满 5 秒上限、
    最终返回 None，等于每轮白烧 5 秒。DB 里 artifact_bytes 是下载时实测写入的
    落盘字节数，聚合 0.07 秒即可，口径与「产物」这一项完全对得上。
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(artifact_bytes), 0) s FROM task_traj_records "
            "WHERE artifacts_status='done'").fetchone()
    return (row["s"] if row else 0) or 0


def _queue_counts() -> dict:
    with get_connection() as conn:
        # 唯一的状态机就是产物下载（artifacts_status）。原 traj_status/status 驱动的
        # 「深层下载」已取消：那两列恒为 'done'，按它统计永远返回 0，只会误导看板。
        art = {r["st"]: r["n"] for r in conn.execute(
            "SELECT COALESCE(artifacts_status,'queued') st, COUNT(*) n "
            "FROM task_traj_records WHERE artifact_complete=1 GROUP BY st")}
        art_bytes = conn.execute(
            "SELECT COALESCE(SUM(artifact_bytes),0) s FROM task_traj_records "
            "WHERE artifacts_status='done'").fetchone()["s"]
        # 浅层分级的进度：原来只返回 shallow_pending 一个数字，看板拿不到
        # 「总共多少 / 已分多少 / 失败多少」，也就画不出进度。这里一次聚合算全，
        # 走 idx_task_records_level 覆盖索引，实测 0.54s（拆成 5 条独立查询反而 1.5s+）。
        shallow_row = conn.execute(
            "SELECT COUNT(*) total, "
            "       SUM(CASE WHEN traj_level IS NULL THEN 1 ELSE 0 END) pending, "
            "       SUM(CASE WHEN traj_level='failed' THEN 1 ELSE 0 END) failed, "
            "       SUM(CASE WHEN traj_level IN ('L0','L1','L1.5','L2','L3') "
            "                THEN 1 ELSE 0 END) graded "
            "FROM task_records").fetchone()
        shallow_total = shallow_row["total"] or 0
        shallow_pending = shallow_row["pending"] or 0
        shallow_failed = shallow_row["failed"] or 0
        shallow_graded = shallow_row["graded"] or 0
        inst_active = conn.execute(
            "SELECT COUNT(*) n FROM task_instances "
            "WHERE status IN ('running','preparing')").fetchone()["n"]
        inst_queued = conn.execute(
            "SELECT COUNT(*) n FROM task_instances WHERE status='queued'").fetchone()["n"]
        not_complete = conn.execute(
            "SELECT COUNT(*) n FROM task_traj_records WHERE artifact_complete=0"
        ).fetchone()["n"]
    return {
        "artifact_pending": art.get("queued", 0),
        "artifact_downloading": art.get("downloading", 0),
        "artifact_done": art.get("done", 0),
        "artifact_failed": art.get("failed", 0),
        "artifact_oversize": art.get("skipped", 0),
        # OBS 前缀确认存在但没有任何文件（沙箱没产出）。这是终态、不是故障，
        # 单独统计，别混进 failed 让人白排查。
        "artifact_empty": art.get("empty", 0),
        "artifact_done_bytes": art_bytes,
        "artifact_not_complete": not_complete,
        "shallow_pending": shallow_pending,
        "shallow_total": shallow_total,
        "shallow_graded": shallow_graded,
        "shallow_failed": shallow_failed,
        # 进度百分比：已分级 / 总数。分母为 0 时给 None，前端显示「—」而不是 0%。
        "shallow_progress": (round(shallow_graded / shallow_total * 100, 1)
                             if shallow_total else None),
        "instances_active": inst_active,
        "instances_queued": inst_queued,
    }


@router.get("/queue")
async def queue(
    limit: int = Query(default=50, ge=1, le=500),
    status: str = Query(default="active",
                        description="active=活跃队列(queued/downloading/failed) | done=已完成 | all"),
    user: dict = Depends(require_operator),
):
    """队列明细。含下载速度、排队位置、所属实例的进度。

    排队位置 = 该实例内 queued 行按 updated_at 的序号（同实例内才有意义，
    因为不同实例是独立批次，混排没有意义）。

    实例进度取自 task_instances 的 total/completed/failed（沙箱侧的任务完成情况），
    与产物下载进度（artifact_complete / artifacts_status）是两回事，这里都给出。
    """
    # 这条查询带 3 个相关子查询，实测 27s+（首次 97s），前端每 30 秒轮询。
    # 同步跑在 async handler 里 = 事件循环被钉住几十秒，必须整个丢线程池。
    items = await async_execute(_queue_page, limit, status)
    counts = await async_execute(_queue_counts)
    return {"items": items, "counts": counts}


def _queue_page(limit: int, status: str) -> list[dict]:
    """同步实现：查队列明细并算速度/进度。调用方放线程池。"""
    where = {
        # empty 是终态（OBS 前缀确实为空、沙箱没产出），不该出现在「活跃队列」里 ——
        # 它不是待处理的积压，混进去会让运维以为还有活没干完。
        "active": "a.artifacts_status IN ('queued','downloading','failed','skipped')",
        "done": "a.artifacts_status='done'",
        "empty": "a.artifacts_status='empty'",
        "all": "1=1",
    }.get(status, "1=1")

    now = time.time()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT a.instance_id, a.traj_name, a.artifacts_status AS status, a.error, "
            "a.artifact_files AS file_count, a.artifact_bytes AS total_bytes, "
            "a.artifact_started_at, a.artifact_downloaded_at, a.updated_at, "
            "a.artifact_attempts, a.artifact_worker, a.artifact_priority, "
            "ti.name AS instance_name, ti.status AS instance_status, "
            "ti.total_tasks, ti.completed_tasks, ti.failed_tasks "
            f"FROM task_traj_records a "
            "LEFT JOIN task_instances ti ON ti.id = a.instance_id "
            f"WHERE {where} AND a.artifact_complete=1 "
            # 必须与 worker 的领取顺序完全一致（offline/output_worker.py 的
            # `ORDER BY artifact_priority DESC, updated_at`），否则看板第一行是队尾：
            # 用户看到的「排在前面」实际最后才下。原先是 a.updated_at DESC，方向反了。
            "ORDER BY a.artifact_priority DESC, a.updated_at LIMIT ?", (limit,)).fetchall()

        # 实例级计数 + 排队位置都在 Python 侧算，不用相关子查询。
        # 相关子查询被 SQLite 选错索引时（实测选了 idx_traj_artifacts_status，
        # 先扫全库 362 万条 queued 再按实例过滤）单次 6~17s，乘上每页行数就是分钟级。
        #
        # 两条子查询都必须 INDEXED BY idx_traj_records_inst：
        # 优化器只看 WHERE 里有没有 artifacts_status，就选了那个索引，
        # 于是「按实例取几千行」变成「扫全库几百万行再按实例过滤」。
        # 强制走 instance_id 索引后实测 16.99s -> 0.03s。
        iids = {r["instance_id"] for r in rows}
        stats: dict[str, tuple[int, int]] = {}
        queued_ts: dict[str, list[str]] = {}
        for iid in iids:
            row = conn.execute(
                "SELECT SUM(CASE WHEN artifact_complete=1 THEN 1 ELSE 0 END) AS total, "
                "       SUM(CASE WHEN artifacts_status='done' THEN 1 ELSE 0 END) AS done "
                "FROM task_traj_records INDEXED BY idx_traj_records_inst "
                "WHERE instance_id=?", (iid,)).fetchone()
            stats[iid] = ((row["total"] or 0) if row else 0,
                          (row["done"] or 0) if row else 0)
            queued_ts[iid] = [
                (r["artifact_priority"] or 0, r["updated_at"])
                for r in conn.execute(
                    "SELECT artifact_priority, updated_at FROM task_traj_records "
                    "INDEXED BY idx_traj_records_inst "
                    # 必须带 artifact_complete=1：worker 只领 complete=1 的行，
                    # 不加这条会把 359 万条永不下载的行算进名次 —— 实测同一实例
                    # UI 显示第 19515 名、真实第 6599 名，虚高 2.96 倍。
                    "WHERE instance_id=? AND artifacts_status='queued' "
                    "AND artifact_complete=1", (iid,))
                if r["updated_at"] is not None]

    def _queue_pos(d: dict) -> int | None:
        """同实例内排在该行之前的可领取行数（与 worker 领取顺序同口径）。

        排序键 = (artifact_priority DESC, updated_at ASC)，与
        offline/output_worker.py 的领取 SQL 完全一致 —— 用户点了「下载此任务」
        会置 priority=100，名次必须立刻跳到最前，否则看板与真实队列不符。
        updated_at 是 SQLite 的 UTC 字符串，字典序即时间序。
        """
        if d["status"] != "queued":
            return None
        ts = d.get("updated_at")
        if ts is None:
            return None
        my_pri = d.get("artifact_priority") or 0
        before = sum(
            1 for pri, t in queued_ts.get(d["instance_id"], ())
            if pri > my_pri or (pri == my_pri and t < ts)
        )
        return before + 1

    items = []
    for r in rows:
        d = dict(r)
        # 下载速度：已完成用 (downloaded-started)，下载中用 (now-started) 的瞬时平均
        speed = None
        started = d.get("artifact_started_at")
        if started and d.get("total_bytes"):
            end = d.get("artifact_downloaded_at") or (
                now if d["status"] == "downloading" else None)
            if end and end > started:
                speed = round(d["total_bytes"] / (end - started) / 1048576, 2)

        inst_total, inst_done = stats.get(d["instance_id"], (0, 0))
        items.append({
            "instance_id": d["instance_id"],
            "instance_name": d.get("instance_name") or d["instance_id"],
            "instance_status": d.get("instance_status"),
            "traj_name": d["traj_name"],
            "status": d["status"],
            "error": d.get("error"),
            "file_count": d.get("file_count"),
            "total_bytes": d.get("total_bytes"),
            "speed_mbps": speed,
            "queue_pos": _queue_pos(d),
            "started_at": started,
            "downloaded_at": d.get("artifact_downloaded_at"),
            "attempts": d.get("artifact_attempts"),
            "worker": d.get("artifact_worker"),
            "updated_at": d.get("updated_at"),
            # 实例级：沙箱任务进度（task_instances）与产物下载进度（本表）分开给
            "instance_total": inst_total,
            "instance_done": inst_done,
            "instance_progress": (round(inst_done / inst_total * 100, 1)
                                  if inst_total else None),
            "sandbox_total": d.get("total_tasks"),
            "sandbox_completed": d.get("completed_tasks"),
            "sandbox_failed": d.get("failed_tasks"),
        })
    return items


@router.get("/trend")
async def trend(
    hours: float = Query(default=1, ge=0.1, le=72),
    user: dict = Depends(require_operator),
):
    """心跳按时间分桶，供负载趋势图。桶宽自适应（最多 ~60 个点）。"""
    points, bucket = await async_execute(_trend_points, hours)
    counts = await async_execute(_queue_counts)
    return {
        "hours": hours,
        "bucket_seconds": bucket,
        "points": points,
        "counts": counts,
    }


def _trend_points(hours: float) -> tuple[list[dict], float]:
    """同步实现：读心跳并按时间分桶。调用方放线程池。"""
    since = time.time() - hours * 3600
    bucket = max(60.0, (hours * 3600) / 60)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT worker, round_start, processed, failed, duration "
            "FROM worker_heartbeats WHERE round_start >= ? ORDER BY round_start",
            (since,)).fetchall()

    buckets: dict[int, dict] = {}
    for r in rows:
        rs = r["round_start"]
        if not rs:
            continue
        b = int(rs // bucket) * int(bucket)
        cur = buckets.setdefault(b, {"ts": b, "processed": 0, "failed": 0, "rounds": 0})
        cur["processed"] += r["processed"] or 0
        cur["failed"] += r["failed"] or 0
        cur["rounds"] += 1

    return [buckets[k] for k in sorted(buckets)], bucket


@router.get("/eta")
async def eta(
    window_minutes: int = Query(default=30, ge=5, le=240),
    user: dict = Depends(require_operator),
):
    """按「最近 window_minutes 的实测吞吐」估算剩余时间。

    吞吐为 0 时明确返回 null（estimated_finish_at / remaining_seconds 都为 null），
    不显示假数字。
    """
    processed, pending = await async_execute(_eta_inputs, window_minutes)

    per_min = processed / window_minutes if window_minutes else 0
    remaining = None
    finish_at = None
    if per_min > 0 and pending > 0:
        remaining = round(pending / per_min * 60, 0)
        finish_at = (datetime.now() + timedelta(seconds=remaining)).isoformat(timespec="seconds")

    return {
        "window_minutes": window_minutes,
        "processed_in_window": processed,
        "throughput_per_minute": round(per_min, 2),
        "pending": pending,
        "remaining_seconds": remaining,
        "estimated_finish_at": finish_at,
        "estimable": remaining is not None,
    }


def _eta_inputs(window_minutes: int) -> tuple[int, int]:
    """同步实现：取窗口内吞吐与积压量。调用方放线程池。"""
    since = time.time() - window_minutes * 60
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(processed),0) p FROM worker_heartbeats "
            "WHERE round_start >= ? AND round_start > 0", (since,)).fetchone()
        pending = conn.execute(
            "SELECT COUNT(*) n FROM task_traj_records "
            "WHERE artifacts_status='queued' AND artifact_complete=1").fetchone()["n"]
    return row["p"], pending


@router.post("/reclaim")
async def reclaim(user: dict = Depends(require_operator)):
    """手动把卡住的 downloading 改回 pending。

    worker 每轮也会自动回收（超时阈值 ARTIFACT_DOWNLOADING_TIMEOUT_MIN），
    这个端点用于人工立即处理。
    """
    from offline.output_worker import _reclaim_stuck_hours
    # 回收会 UPDATE 多行 + 连库，同步跑在 async handler 里同样会钉住事件循环
    n = await async_execute(_reclaim_stuck_hours)
    counts = await async_execute(_queue_counts)
    return {"reclaimed": n, "counts": counts}
