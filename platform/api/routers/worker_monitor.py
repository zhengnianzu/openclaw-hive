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

import fcntl
import json
import os
import shutil
import sys
import time
import threading
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query

from ..core.config import settings
from ..core.database import async_execute, get_connection
from ..core.security import require_operator

router = APIRouter(prefix="/api/worker-monitor", tags=["worker-monitor"])

# pid 文件固定白名单，路径不接受外部输入（避免被当成任意 pid 探测接口）
WORKER_PID_FILES = {
    # 目标架构的 4 个常驻进程：按「场景」分车道，不按阶段分。
    "tracker": "tracker.pid",                      # 发现投递
    "shallow-realtime": "shallow_realtime.pid",    # 实时分析（用户点击，两阶段）
    "shallow-auto": "shallow_auto.pid",            # 背景分析（全量，最新优先）
    "deep-realtime": "realtime_worker.pid",        # 实时下载（点击的交付物）
    # 代理用量采集链路（三进程）
    "proxy-tracker": "proxy_tracker.pid",          # 增量发现完成 task → 投队列
    "proxy-realtime": "proxy_realtime.pid",        # 实时车道：拉 OBS + 解析入库
    "proxy-auto": "proxy_auto.pid",                # 背景车道：历史回填
    # 数据集同步（本地改动 → OBS）。此前不在表里，进程跑没跑面板上看不见。
    "sync-worker": "sync_worker.pid",
    # 这两个一直在跑、也在写心跳，但此前漏登记 —— 面板上永远看不到，
    # 卡住时只能靠 ssh 才发现。
    "auto-tracker": "auto_tracker.pid",            # 历史实例投递
    "shallow-query": "shallow_query.pid",          # 任务描述回填
    # 旧进程名：保留以便升级期间仍能看到它们的状态与历史心跳。
    # shallow-local / shallow-obs 已被 shallow-realtime/auto 取代（按阶段拆 → 按场景拆），
    # shallow（--shallow-only）与 deep（--deep-only）是更早的单体循环。
    "shallow-local": "shallow_local.pid",
    "shallow-obs": "shallow_obs.pid",
    "shallow": "worker.pid",
    "deep": "deep_worker.pid",
}
PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# worker → 日志文件名。**同样是白名单**，日志路径绝不接受客户端输入 ——
# 否则这里就成了「任意文件读取」接口。文件名与 run.sh 的 start_* 一一对应。
WORKER_LOG_FILES = {
    "tracker": "tracker.log",
    "shallow-realtime": "shallow_realtime.log",
    "shallow-auto": "shallow_auto.log",
    "deep-realtime": "realtime_worker.log",
    "proxy-tracker": "proxy_tracker.log",
    "proxy-realtime": "proxy_realtime.log",
    "proxy-auto": "proxy_auto.log",
    "sync-worker": "sync_worker.log",
    "auto-tracker": "auto_tracker.log",
    "shallow-query": "shallow_query.log",
    # 旧进程
    "shallow-local": "shallow_local.log",
    "shallow-obs": "shallow_obs.log",
    "shallow": "worker.log",
    "deep": "deep_worker.log",
}

# 已被取代的旧进程（按阶段拆分的 shallow-local/shallow-obs，以及更早的单体循环
# shallow/deep）。它们只剩回滚价值：正常永远 offline，在表里堆四行「离线 / 尚无心跳」
# 只会稀释真正的信号。
# 处理方式：**未运行就隐藏**，而不是从白名单删掉 —— 万一有人用
# `./run.sh shallow-start` 回滚，它照样能在面板上看到（活着就显示），
# 而且历史心跳、日志入口都还在。
LEGACY_WORKERS = {"shallow-local", "shallow-obs", "shallow", "deep"}

WORKER_LABEL = {
    "tracker": "发现投递",
    "shallow-realtime": "实时分析（点击）",
    "shallow-auto": "背景分析（全量）",
    "deep-realtime": "实时下载（点击）",
    "proxy-tracker": "代理用量发现",
    "proxy-realtime": "代理用量采集（实时）",
    "proxy-auto": "代理用量采集（批量）",
    "sync-worker": "数据集同步",
    "auto-tracker": "历史实例投递",
    "shallow-query": "任务描述回填",
    # 旧名
    "shallow-local": "本地 DONE（旧）",
    "shallow-obs": "OBS 定级（旧）",
    "shallow": "浅层分级（旧）",
    "deep": "深层下载 / 产物（旧）",
    "realtime": "实时下载（旧名）",
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
        # 旧进程未运行就不展示（理由见 LEGACY_WORKERS）
        if name in LEGACY_WORKERS and not alive:
            continue
        workers.append(item)

    # 如果 deep worker 也跑产物，额外标出来；单独的 artifacts 进程（--artifacts-only）没有 pid 文件
    art_hb = _latest_heartbeat("artifacts")
    # 同 LEGACY_WORKERS：进程已经不在就不展示这条历史心跳，避免「离线」行堆积
    art_alive = bool(art_hb and art_hb.get("pid") and _pid_alive(int(art_hb["pid"])))
    if art_hb and art_alive:
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


# ── 看板数据缓存 ────────────────────────────────────────────────────────────
#
# 为什么需要：调度监控的三块数据都是**千万行表的全表聚合**，单次 1~5 秒：
#   · _queue_counts    5.5s（overview / queue 用）
#   · _load_inputs     1.9s（车道负载用）
#   · _queue_page      0.7s（下载明细用）
# 而前端每 30 秒轮询一次，等于每 30 秒把这几条聚合重跑一遍。
#
# 为什么是「文件 + 内存」两层：
#   uvicorn 跑 --workers 4，**内存缓存各自一份**。只放内存的话，同一份聚合会被
#   四个进程各算一遍（实测首屏连撞好几次 5 秒）。落成小 JSON 让进程间共享，
#   再用 flock 保证同一时刻只有一个进程在算（跨进程单飞）。
#
# 读取策略是 stale-while-revalidate：有旧值就先返回旧值、后台刷新。
# 任何请求都不会被聚合卡住 —— 唯一的例外是「从来没有算过」，所以下面还配了
# prewarm_caches()，在后端启动时就把第一份算好。
_CACHE_DIR = os.path.join(PLATFORM_DIR, "tmp")
_CACHE_LOCK = threading.Lock()
_CACHE_MEM: dict[str, list] = {}      # key -> [data, computed_at]
_CACHE_REFRESHING: set[str] = set()   # 本进程正在刷新哪些 key

_QC_TTL = 30.0      # 与前端 30s 轮询对齐
_LOAD_TTL = 20.0
_QUEUE_TTL = 15.0


def _cache_paths(key: str) -> tuple[str, str]:
    base = os.path.join(_CACHE_DIR, f"wm_cache_{key}.json")
    return base, base + ".lock"


def _cache_read(path: str) -> tuple[dict | list | None, float]:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        data, at = d.get("data"), float(d.get("at") or 0)
        # 注意也接受 list：_queue_page 返回的是数组，只认 dict 会让它永远不命中
        if isinstance(data, (dict, list)):
            return data, at
    except (OSError, ValueError):
        pass
    return None, 0.0


def _cache_write(path: str, data, at: float) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"data": data, "at": at}, f, ensure_ascii=False)
        # 本地盘，rename 可用（sfs_turbo 上不行，所以缓存必须落在 platform/tmp 下）
        os.replace(tmp, path)
    except OSError:
        pass


def _cache_compute_locked(key: str, compute) -> dict | None:
    """跨进程单飞：拿不到文件锁说明别的进程正在算，返回 None。"""
    path, lockpath = _cache_paths(key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(lockpath, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None
        data = compute()
        at = time.time()
        _cache_write(path, data, at)
        with _CACHE_LOCK:
            _CACHE_MEM[key] = [data, at]
        return data
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _cache_refresh_bg(key: str, compute) -> None:
    try:
        _cache_compute_locked(key, compute)
    except Exception:
        pass          # 刷新失败保留旧值，比让看板报错好
    finally:
        with _CACHE_LOCK:
            _CACHE_REFRESHING.discard(key)


def _file_cache(key: str, ttl: float, compute) -> dict:
    now = time.time()

    with _CACHE_LOCK:
        mem = _CACHE_MEM.get(key)
        if mem and now - mem[1] < ttl:
            return mem[0]

    path, _ = _cache_paths(key)
    data, at = _cache_read(path)
    if data is not None:
        with _CACHE_LOCK:
            _CACHE_MEM[key] = [data, at]
        if now - at < ttl:
            return data
        # 过期：先给旧值，后台刷新，不阻塞请求
        with _CACHE_LOCK:
            spawn = key not in _CACHE_REFRESHING
            if spawn:
                _CACHE_REFRESHING.add(key)
        if spawn:
            threading.Thread(target=_cache_refresh_bg, args=(key, compute),
                             daemon=True).start()
        return data

    # 从没算过（首次部署 / tmp 被清）：自己算；别的进程在算就等它写完
    d = _cache_compute_locked(key, compute)
    if d is not None:
        return d
    deadline = time.time() + 15
    while time.time() < deadline:
        time.sleep(0.25)
        data, at = _cache_read(path)
        if data is not None:
            with _CACHE_LOCK:
                _CACHE_MEM[key] = [data, at]
            return data
    return compute()      # 兜底：自己算（慢，但总比返回空好）


def prewarm_caches() -> None:
    """后端启动时预热，避免部署后第一个用户请求吃 5 秒。

    三份**并行**算：串行要 16 秒（5.5 + 1.9 + 0.7 再加等锁），并行只要最慢的那份。
    四个 worker 会同时调到这里；flock 保证每份只算一次，其余进程等文件出现后直接读。
    """
    targets = (
        ("queue_counts", _QC_TTL, _queue_counts_uncached),
        ("load_10", _LOAD_TTL, lambda: _load_inputs(10)),
        ("queue_active_50", _QUEUE_TTL, lambda: _queue_page(50, "active")),
    )
    threads = []
    for key, ttl, compute in targets:
        t = threading.Thread(target=_prewarm_one, args=(key, ttl, compute), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=30)


def _prewarm_one(key: str, ttl: float, compute) -> None:
    try:
        _file_cache(key, ttl, compute)
    except Exception:
        pass


def _queue_counts() -> dict:
    """看板的队列/进度汇总。具体缓存策略见上面 _file_cache 的注释。"""
    return _file_cache("queue_counts", _QC_TTL, _queue_counts_uncached)


def _queue_counts_uncached() -> dict:
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
        # 按车道拆的浅层待办：上面的 shallow_pending 是全局 traj_level IS NULL，
        # 看不出「用户刚点的那批还剩多少」—— 而这正是看板最该回答的问题。
        # 读权威队列 shallow_queue 的两阶段维，按 priority 车道分开。
        #
        # 阶段 2 的条件必须和 shallow_obs.claim 一致（_OBS_CLAIMABLE）：
        # 线上有 2.5 万行 obs_state='queued' 但 traj_level 早已写好，worker 不会领，
        # 算进待办会让看板显示永远清不完的假积压。
        lane_row = conn.execute(
            "SELECT "
            "  SUM(CASE WHEN priority >= 90 AND local_state='queued' "
            "           THEN 1 ELSE 0 END) rt_s1, "
            "  SUM(CASE WHEN priority >= 90 AND obs_state='queued' AND "
            + _OBS_CLAIMABLE + " THEN 1 ELSE 0 END) rt_s2, "
            "  SUM(CASE WHEN priority < 90 AND local_state='queued' "
            "           THEN 1 ELSE 0 END) auto_s1, "
            "  SUM(CASE WHEN priority < 90 AND obs_state='queued' AND "
            + _OBS_CLAIMABLE + " THEN 1 ELSE 0 END) auto_s2, "
            "  SUM(CASE WHEN artifact_state='queued' THEN 1 ELSE 0 END) art_q, "
            "  SUM(CASE WHEN artifact_state='pending_click' THEN 1 ELSE 0 END) art_pc "
            "FROM shallow_queue").fetchone()
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
        # 按车道拆的浅层两阶段待办（权威队列口径）
        "shallow_realtime_stage1": lane_row["rt_s1"] or 0,
        "shallow_realtime_stage2": lane_row["rt_s2"] or 0,
        "shallow_auto_stage1": lane_row["auto_s1"] or 0,
        "shallow_auto_stage2": lane_row["auto_s2"] or 0,
        # 产物：queued = 用户点过待下；pending_click = 静默态（点开才下，不占配额）
        "artifact_queue_pending": lane_row["art_q"] or 0,
        "artifact_pending_click": lane_row["art_pc"] or 0,
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
    items = await async_execute(_file_cache, f"queue_{status}_{limit}", _QUEUE_TTL,
                                lambda: _queue_page(limit, status))
    counts = await async_execute(_queue_counts)
    return {"items": items, "counts": counts}


def _queue_page(limit: int, status: str) -> list[dict]:
    """同步实现：查队列明细并算速度/进度。调用方放线程池。"""
    where = {
        # empty 是终态（OBS 前缀确实为空、沙箱没产出），不该出现在「活跃队列」里 ——
        # 它不是待处理的积压，混进去会让运维以为还有活没干完。
        # 前端标签叫「下载失败」。仍保留 queued/downloading：产物纯点击驱动下
        # 它们结构上恒为 0，但用户点了「下载交付物」的那几秒里确实会短暂出现，
        # 一并列出才不会让人以为点击没生效。
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
    points, bucket, series = await async_execute(_trend_points, hours)
    # 不再顺带返回 _queue_counts：那个聚合单次 5 秒级，而前端 trend 只画
    # points / series，counts 从来没用过（loadTrend 只读这两个字段）。
    # overview 已经提供了同样的 counts，没必要每个趋势刷新都再算一遍。
    return {
        "hours": hours,
        "bucket_seconds": bucket,
        "points": points,
        # 按 worker 拆开的曲线（tracker / shallow-local / shallow-obs / deep / realtime），
        # 每条与 points 的 ts 一一对齐。前端用它画分线，而不是把所有 worker 加成一条。
        "series": series,
    }


def _trend_points(hours: float) -> tuple[list[dict], float]:
    """同步实现：读心跳并按时间分桶。调用方放线程池。

    除了总量，还按 worker 拆出 series —— 不同 worker 的 processed 语义完全不同
    （浅层一轮几千行、产物下载一轮几个任务），混在一条线上相加没有任何意义：
      tracker       每轮「新投递到 shallow_queue 的 config 数」
      shallow-local 每轮「判出 DONE 的数量」
      shallow-obs   每轮「定级 L0~L3 的数量」
      deep/realtime 每轮「产物下载完成数」
    前端按 series 各画一条线，才能看出瓶颈卡在发现、本地判定还是 OBS 定级。
    """
    since = time.time() - hours * 3600
    bucket = max(60.0, (hours * 3600) / 60)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT worker, round_start, processed, failed, duration "
            "FROM worker_heartbeats WHERE round_start >= ? ORDER BY round_start",
            (since,)).fetchall()

    buckets: dict[int, dict] = {}
    by_worker: dict[str, dict[int, int]] = {}
    for r in rows:
        rs = r["round_start"]
        if not rs:
            continue
        b = int(rs // bucket) * int(bucket)
        cur = buckets.setdefault(b, {"ts": b, "processed": 0, "failed": 0, "rounds": 0})
        cur["processed"] += r["processed"] or 0
        cur["failed"] += r["failed"] or 0
        cur["rounds"] += 1

        w = r["worker"] or "unknown"
        wb = by_worker.setdefault(w, {})
        wb[b] = wb.get(b, 0) + (r["processed"] or 0)

    points = [buckets[k] for k in sorted(buckets)]
    ts_list = [p["ts"] for p in points]
    # series 里每个 worker 一条，按 ts_list 对齐（缺失桶补 0），前端直接画
    series = [
        {"worker": w, "data": [buckets_ts.get(ts, 0) for ts in ts_list]}
        for w, buckets_ts in sorted(by_worker.items())
    ]
    return points, bucket, series


@router.get("/eta")
async def eta(
    window_minutes: int = Query(default=30, ge=5, le=240),
    user: dict = Depends(require_operator),
):
    """按「最近 window_minutes 的实测吞吐」估算剩余时间，**按车道分别算**。

    吞吐为 0 时明确返回 null（estimated_finish_at / remaining_seconds 都为 null），
    不显示假数字。

    为什么要分车道：原来分子是 `SUM(processed)` 把**全部 worker 混加**
    （实测 30 分钟 78,424，而真正做产物下载的只有 6,220 → 虚高 12.6 倍），
    分母又读镜像表 `task_traj_records`（1,931,992 vs 权威队列 973,599 → 虚高 2 倍）。
    两个误差方向相反但不抵消，算出来的完成时间毫无参考价值。
    现在每条车道各用自己的 worker 吞吐 ÷ 自己的权威队列积压。
    """
    lanes = await async_execute(_eta_inputs, window_minutes)

    out = {}
    for key, (processed, pending) in lanes.items():
        per_min = processed / window_minutes if window_minutes else 0
        remaining = None
        finish_at = None
        if per_min > 0 and pending > 0:
            remaining = round(pending / per_min * 60, 0)
            finish_at = (datetime.now()
                         + timedelta(seconds=remaining)).isoformat(timespec="seconds")
        out[key] = {
            "processed_in_window": processed,
            "throughput_per_minute": round(per_min, 2),
            "pending": pending,
            "remaining_seconds": remaining,
            "estimated_finish_at": finish_at,
            "estimable": remaining is not None,
        }

    # 顶层保留旧字段（取产物车道）兼容现有前端：WorkerMonitor.vue 直接读
    # eta.throughput_per_minute / eta.pending / eta.estimated_finish_at。
    top = out.get("artifact", {})
    return {
        "window_minutes": window_minutes,
        "by_lane": out,
        **{k: top.get(k) for k in
           ("processed_in_window", "throughput_per_minute", "pending",
            "remaining_seconds", "estimated_finish_at", "estimable")},
    }


# 阶段 2「真的可被领取」的条件，与 shallow_obs.claim 的 EXISTS 子句同源。
# 抽成常量是为了让这里和 worker 的口径不会各自漂移 —— 一旦不一致，看板的积压数
# 就会包含 worker 永远不碰的行（实测 2.5 万行已定级但 obs_state 仍 queued）。
_OBS_CLAIMABLE = (
    "EXISTS(SELECT 1 FROM task_records tr "
    "       WHERE tr.instance_id=shallow_queue.instance_id "
    "         AND tr.config_name=shallow_queue.config_name "
    "         AND tr.traj_level IS NULL "
    "         AND tr.task_done IS NOT NULL)"
)

# 每条车道：(心跳里的 worker 名, 权威队列的积压 WHERE 条件)。
#
# 分母一律读 shallow_queue —— 它是 worker 唯一认的队列。
# task_traj_records 只是给前端看的镜像，两者实测差 2 倍（193 万 vs 97 万）。
_ETA_LANES = {
    # 浅层两阶段任一维还在排队就算待办；按场景车道拆开。
    #
    # 阶段 2 必须带 EXISTS 子查询，和 shallow_obs.claim 的条件**完全一致** ——
    # 线上有 2.5 万行 obs_state 还是 'queued' 但 traj_level 其实早已写好
    # （旧架构遗留：定级成功了但队列那一维没推进）。这些行 worker 根本不会领，
    # 不带 EXISTS 就会把它们算进积压，ETA 直接报出「181 天」这种假数字。
    "shallow_realtime": (
        ("shallow-realtime",),
        "(local_state='queued' OR (obs_state='queued' AND " + _OBS_CLAIMABLE + ")) "
        "AND priority >= 90",      # LANE_REALTIME_MIN（点击 100 + 自动跟踪 90）
    ),
    "shallow_auto": (
        ("shallow-auto",),
        "(local_state='queued' OR (obs_state='queued' AND " + _OBS_CLAIMABLE + ")) "
        "AND priority < 90",
    ),
    # 产物只有实时车道（不设 deep-auto：全量下载会写爆磁盘）。
    # 兼容旧进程名 deep/realtime/artifacts，免得改名后看板读不到历史吞吐。
    "artifact": (
        ("deep-realtime", "deep", "realtime", "artifacts"),
        "artifact_state='queued'",
    ),
}


def _eta_inputs(window_minutes: int) -> dict:
    """同步实现：每条车道各取「自己 worker 的吞吐」与「自己队列的积压」。

    调用方放线程池。返回 {lane: (processed, pending)}。
    """
    since = time.time() - window_minutes * 60
    out: dict = {}
    with get_connection() as conn:
        for lane, (workers, where) in _ETA_LANES.items():
            marks = ",".join("?" * len(workers))
            row = conn.execute(
                "SELECT COALESCE(SUM(processed),0) p FROM worker_heartbeats "
                f"WHERE round_start >= ? AND round_start > 0 AND worker IN ({marks})",
                (since, *workers)).fetchone()
            pending = conn.execute(
                f"SELECT COUNT(*) n FROM shallow_queue WHERE {where}").fetchone()["n"]
            out[lane] = (row["p"] or 0, pending or 0)
    return out


@router.get("/load")
async def load(
    window_minutes: int = Query(default=10, ge=2, le=120),
    user: dict = Depends(require_operator),
):
    """按车道的**实际负载**：排队多少 / 吞吐多少 / 多久清完 / 会不会积压。

    这是看板最该回答的四个问题，原来的「浅层分级进度」只给全库累计的
    已分级/待分级（1000 万量级的静态数字），看不出此刻堵不堵。

    每条车道返回：
      pending      当前待办行数（与 worker 的 claim 条件同口径）
      throughput   实测吞吐（行/分，取自 worker_heartbeats.processed）
      inflow       新增投递速率（行/分，取 tracker / auto-tracker 的 processed）
      eta_seconds  按当前吞吐清空 pending 需要多久（吞吐为 0 时 null）
      overloaded   inflow > throughput 且 pending 在涨 → 会持续积压
    """
    data = await async_execute(_file_cache, f"load_{window_minutes}", _LOAD_TTL,
                               lambda: _load_inputs(window_minutes))
    return {"window_minutes": window_minutes, **data}


# 投递方 → 它喂的车道。tracker 投点击(100)和自动跟踪(90)，都进实时车道；
# auto-tracker 只投背景车道(0)。
_INFLOW_WORKERS = {
    "shallow_realtime": ("tracker",),
    "shallow_auto": ("auto-tracker",),
    # 产物没有自动投递方（纯点击驱动），inflow 恒 0
    "artifact": (),
}
_LANE_LABEL = {
    "shallow_realtime": "实时分析（点击 + 运行中实例）",
    "shallow_auto": "背景分析（历史补全）",
    "artifact": "交付物下载（点击触发）",
}


def _load_inputs(window_minutes: int) -> dict:
    """每条车道的 pending / 吞吐 / 新增速率 / ETA / 过载判定。"""
    since = time.time() - window_minutes * 60
    lanes = []
    with get_connection() as conn:
        def _sum(workers: tuple) -> int:
            if not workers:
                return 0
            marks = ",".join("?" * len(workers))
            row = conn.execute(
                "SELECT COALESCE(SUM(processed),0) p FROM worker_heartbeats "
                f"WHERE round_start >= ? AND round_start > 0 AND worker IN ({marks})",
                (since, *workers)).fetchone()
            return row["p"] or 0

        for key, (workers, where) in _ETA_LANES.items():
            pending = conn.execute(
                f"SELECT COUNT(*) n FROM shallow_queue WHERE {where}").fetchone()["n"]
            done = _sum(workers)
            inflow = _sum(_INFLOW_WORKERS.get(key, ()))
            tp = done / window_minutes
            inf = inflow / window_minutes
            eta = round(pending / tp * 60) if tp > 0 and pending > 0 else None
            lanes.append({
                "lane": key,
                "label": _LANE_LABEL.get(key, key),
                "pending": pending,
                "throughput_per_min": round(tp, 1),
                "inflow_per_min": round(inf, 1),
                "eta_seconds": eta,
                # 过载：进得比出得快，且已经有积压 —— 再等下去只会更堵。
                # 吞吐为 0 但有积压也算过载（worker 挂了/被锁住）。
                "overloaded": bool(pending > 0 and (inf > tp or tp == 0)),
                "workers": list(workers),
            })
    return {"lanes": lanes}


@router.get("/log")
async def worker_log(
    worker: str = Query(..., description="worker 名（白名单内）"),
    lines: int = Query(default=300, ge=1, le=2000),
    user: dict = Depends(require_operator),
):
    """某个 worker 的日志尾部。

    点「Worker 资源状态」里的行时调用 —— 状态标红却看不到原因，排查就得 ssh，
    这个接口把那段补上。**worker 名走白名单**，不接受路径。
    """
    fn = WORKER_LOG_FILES.get(worker)
    if not fn:
        # artifacts 没有独立日志（跑在 deep 进程里），其余是非法名字
        return {"available": False, "worker": worker,
                "detail": "该 worker 没有独立日志文件"}
    path = os.path.join(PLATFORM_DIR, fn)
    if not os.path.exists(path):
        return {"available": False, "worker": worker, "file": fn,
                "detail": f"{fn} 不存在（进程可能未启动过）"}

    # 只读尾部，不整文件加载：日志可能有几十 MB，而面板只需要最后几百行。
    # 先按 256KB 试读，行数不够就翻倍，上限 8MB。
    size = os.path.getsize(path)
    budget = min(256 << 10, size)
    text = ""
    while True:
        with open(path, "rb") as f:
            if budget < size:
                f.seek(size - budget)
            data = f.read()
        text = data.decode("utf-8", "replace")
        if text.count("\n") > lines or budget >= size or budget >= (8 << 20):
            break
        budget *= 2

    tail = text.splitlines()
    # 从中间截断时，第一行很可能是半行，丢掉
    cut = budget < size
    if cut and tail:
        tail = tail[1:]
    truncated = len(tail) > lines
    tail = tail[-lines:]
    if cut or truncated:
        tail.insert(0, f"...（仅显示最后 {len(tail)} 行，文件共 {size} 字节）")

    return {
        "available": True,
        "worker": worker,
        "file": fn,
        "lines": tail,
        "bytes": size,
        "mtime": datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds"),
        # 方便用户复制到终端里跑
        "shell_hint": f"cd {PLATFORM_DIR} && tail -f {fn}",
    }


# ── 数据库备份状态 ──────────────────────────────────────────────────────────

def _load_db_backup():
    """按路径加载 platform/db_backup.py（它不在 api 包里，也不在任何 sys.path 上）。"""
    import importlib.util
    path = os.path.join(PLATFORM_DIR, "db_backup.py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location("db_backup", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@router.get("/backup")
async def backup_status(user: dict = Depends(require_operator)):
    """最新备份多新、多大、保留几份 + 现有清单。

    备份停摆是「静默故障」：没人点开目录就不会发现。这里把它变成一个能一眼看到的数字。
    只读 manifest 与文件大小（每个目录 3~4 个文件），不做目录递归。
    """
    def _collect():
        mod = _load_db_backup()
        if mod is None:
            return {"available": False, "detail": "db_backup.py 不存在"}
        dest = os.environ.get("BACKUP_DIR") or mod.DEFAULT_BACKUP_DIR
        rows = mod.list_backups(dest)
        items = []
        for b in rows:
            mf = b.get("manifest") or {}
            items.append({
                "name": b["name"],
                "kind": mf.get("kind", "?"),
                "complete": b["complete"],
                "created_at": mf.get("created_at"),
                "bytes": b["size"],
            })
        latest = items[0] if items else None
        age_hours = None
        if latest and latest.get("created_at"):
            try:
                age_hours = round((
                    datetime.now() - datetime.fromisoformat(latest["created_at"])
                ).total_seconds() / 3600, 1)
            except ValueError:
                pass
        return {
            "available": True,
            "dest": dest,
            "latest": latest,
            "age_hours": age_hours,
            "count": len(items),
            "total_bytes": sum(i["bytes"] for i in items),
            "keep_days": getattr(mod, "KEEP_DAYS", None),
            "items": items[:20],
        }

    return await async_execute(_collect)


@router.post("/backup")
async def backup_run(user: dict = Depends(require_operator)):
    """立即备份一次（每日套件）。

    起子进程而不是在 handler 里直接调：备份要 20~30s，进程里跑完即弃，
    异常也不会污染后端进程。并发由 db_backup.py 自己的 flock 挡住。
    """
    import asyncio
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", os.path.join(PLATFORM_DIR, "db_backup.py"), "run",
        cwd=PLATFORM_DIR,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = (out or b"").decode("utf-8", "replace")
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "returncode": proc.returncode,
        # 只回最后几行：完整日志在 <dest>/db_backup.log
        "output": "\n".join(text.splitlines()[-12:]),
    }


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
