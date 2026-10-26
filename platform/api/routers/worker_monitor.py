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
from ..core.database import get_connection
from ..core.security import require_operator

router = APIRouter(prefix="/api/worker-monitor", tags=["worker-monitor"])

# pid 文件固定白名单，路径不接受外部输入（避免被当成任意 pid 探测接口）
WORKER_PID_FILES = {
    "shallow": "worker.pid",          # --shallow-only
    "deep": "deep_worker.pid",        # --deep-only（含产物下载）
}
PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WORKER_LABEL = {
    "shallow": "浅层分级",
    "deep": "深层下载 / 产物",
    "artifacts": "产物下载",
    "mixed": "混合",
}

# 心跳超过这个秒数没更新，判为「可能卡住」（worker 默认 60s 一轮）
HEARTBEAT_STALE_SECONDS = 300


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


def _dir_size(path: str, limit: int = 200000) -> int | None:
    """目录占用字节数。超量时返回 None（不做昂贵统计）。"""
    if not path or not os.path.isdir(path):
        return None
    total = 0
    seen = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(path):
            for fn in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    pass
                seen += 1
                if seen > limit:
                    return None
    except OSError:
        return None
    return total


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
        item = {
            "worker": name,
            "label": WORKER_LABEL.get(name, name),
            "pid": pid,
            "alive": alive,
            "state": ("offline" if not alive
                      else ("stalled" if (age is not None and age > HEARTBEAT_STALE_SECONDS)
                            else "running")),
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
        workers.append({
            "worker": "artifacts", "label": WORKER_LABEL["artifacts"], "pid": art_hb.get("pid"),
            "alive": bool(art_hb.get("pid")) and _pid_alive(int(art_hb["pid"])),
            "state": "running" if (age is not None and age <= HEARTBEAT_STALE_SECONDS) else "offline",
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
            "output_cache_bytes": _dir_size(settings.OUTPUT_CACHE),
        }
    except OSError:
        disk = None

    counts = _queue_counts()
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "workers": workers,
        "disk": disk,
        "queue": counts,
    }


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
        shallow_pending = conn.execute(
            "SELECT COUNT(*) n FROM task_records WHERE traj_level IS NULL").fetchone()["n"]
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
        "artifact_done_bytes": art_bytes,
        "artifact_not_complete": not_complete,
        "shallow_pending": shallow_pending,
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
    where = {
        "active": "a.artifacts_status IN ('queued','downloading','failed','skipped')",
        "done": "a.artifacts_status='done'",
        "all": "1=1",
    }.get(status, "1=1")

    now = time.time()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT a.instance_id, a.traj_name, a.artifacts_status AS status, a.error, "
            "a.artifact_files AS file_count, a.artifact_bytes AS total_bytes, "
            "a.artifact_started_at, a.artifact_downloaded_at, a.updated_at, "
            "a.artifact_attempts, a.artifact_worker, "
            "ti.name AS instance_name, ti.status AS instance_status, "
            "ti.total_tasks, ti.completed_tasks, ti.failed_tasks, "
            "(SELECT COUNT(*) FROM task_traj_records q "
            "   WHERE q.instance_id=a.instance_id AND q.artifacts_status='queued' "
            "     AND q.updated_at <= a.updated_at) AS queue_pos, "
            "(SELECT COUNT(*) FROM task_traj_records c "
            "   WHERE c.instance_id=a.instance_id AND c.artifact_complete=1) AS inst_total, "
            "(SELECT COUNT(*) FROM task_traj_records c "
            "   WHERE c.instance_id=a.instance_id AND c.artifacts_status='done') AS inst_done "
            f"FROM task_traj_records a "
            "LEFT JOIN task_instances ti ON ti.id = a.instance_id "
            f"WHERE {where} AND a.artifact_complete=1 "
            "ORDER BY a.updated_at DESC LIMIT ?", (limit,)).fetchall()

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

        inst_total = d.get("inst_total") or 0
        inst_done = d.get("inst_done") or 0
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
            "queue_pos": (d.get("queue_pos") if d["status"] == "queued" else None),
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
    return {"items": items, "counts": _queue_counts()}


@router.get("/gantt")
async def gantt(
    limit: int = Query(default=40, ge=1, le=200),
    instance_id: str | None = Query(default=None, description="只看某实例"),
    user: dict = Depends(require_operator),
):
    """每任务的实际耗时（甘特图数据）。

    数据来自 artifact_started_at / artifact_downloaded_at 两个真实时间戳，
    不是估算。只返回有起止时间的行（即真正跑过的下载）。
    """
    now = time.time()
    with get_connection() as conn:
        sql = ("SELECT a.instance_id, a.traj_name, a.artifacts_status AS status, "
               "a.artifact_started_at AS t0, a.artifact_downloaded_at AS t1, "
               "a.artifact_bytes, a.artifact_files, ti.name AS instance_name "
               "FROM task_traj_records a "
               "LEFT JOIN task_instances ti ON ti.id = a.instance_id "
               "WHERE a.artifact_started_at IS NOT NULL "
               "  AND a.artifacts_status IN ('done','failed','skipped') ")
        params: list = []
        if instance_id:
            sql += " AND a.instance_id=?"
            params.append(instance_id)
        sql += " ORDER BY a.artifact_started_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, tuple(params)).fetchall()

        pend = conn.execute(
            "SELECT COUNT(*) n FROM task_traj_records "
            "WHERE artifacts_status='queued' AND artifact_complete=1").fetchone()["n"]

    items = []
    for r in rows:
        d = dict(r)
        t0, t1 = d["t0"], d["t1"]
        dur = round(t1 - t0, 1) if (t0 and t1) else None
        spd = (round(d["artifact_bytes"] / dur / 1048576, 2)
               if (dur and dur > 0 and d["artifact_bytes"]) else None)
        items.append({
            "instance_id": d["instance_id"],
            "instance_name": d.get("instance_name") or d["instance_id"],
            "traj_name": d["traj_name"],
            "status": d["status"],
            "started_at": t0,
            "finished_at": t1,
            "duration_seconds": dur,
            "bytes": d["artifact_bytes"],
            "files": d["artifact_files"],
            "speed_mbps": spd,
        })

    durations = [i["duration_seconds"] for i in items if i["duration_seconds"]]
    avg = round(sum(durations) / len(durations), 1) if durations else None
    # 用已完成任务的平均耗时估剩余时间（比按轮次吞吐更贴近真实工作量）
    remaining = round(pend * avg, 0) if (avg and pend) else None
    return {
        "items": items,
        "pending": pend,
        "avg_seconds": avg,
        "median_seconds": (round(sorted(durations)[len(durations) // 2], 1)
                           if durations else None),
        "remaining_seconds": remaining,
        "estimated_finish_at": ((datetime.now() + timedelta(seconds=remaining)
                                 ).isoformat(timespec="seconds")
                                if remaining else None),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


@router.get("/trend")
async def trend(
    hours: float = Query(default=1, ge=0.1, le=72),
    user: dict = Depends(require_operator),
):
    """心跳按时间分桶，供负载趋势图。桶宽自适应（最多 ~60 个点）。"""
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

    points = [buckets[k] for k in sorted(buckets)]
    return {
        "hours": hours,
        "bucket_seconds": bucket,
        "points": points,
        "counts": _queue_counts(),
    }


@router.get("/eta")
async def eta(
    window_minutes: int = Query(default=30, ge=5, le=240),
    user: dict = Depends(require_operator),
):
    """按「最近 window_minutes 的实测吞吐」估算剩余时间。

    吞吐为 0 时明确返回 null（estimated_finish_at / remaining_seconds 都为 null），
    不显示假数字。
    """
    since = time.time() - window_minutes * 60
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(processed),0) p FROM worker_heartbeats "
            "WHERE round_start >= ? AND round_start > 0", (since,)).fetchone()
        pending = conn.execute(
            "SELECT COUNT(*) n FROM task_traj_records "
            "WHERE artifacts_status='queued' AND artifact_complete=1").fetchone()["n"]

    processed = row["p"]
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


@router.post("/reclaim")
async def reclaim(user: dict = Depends(require_operator)):
    """手动把卡住的 downloading 改回 pending。

    worker 每轮也会自动回收（超时阈值 ARTIFACT_DOWNLOADING_TIMEOUT_MIN），
    这个端点用于人工立即处理。
    """
    from offline.output_worker import _reclaim_stuck_hours
    n = _reclaim_stuck_hours()
    return {"reclaimed": n, "counts": _queue_counts()}
