# -*- coding: utf-8 -*-
"""worker_heartbeats 的写入工具（浅层三个进程共用）。

原来这两个函数私有在 offline/output_worker.py 里（`_write_heartbeat` /
`_write_worker_started`），新的 tracker / shallow_local / shallow_obs 是独立进程，
复用它们要 import output_worker —— 那会把整个 worker 模块（含 obsutil 相关依赖）
拖进来，启动慢且耦合。这里只依赖 get_connection，保持轻量。

表结构见 api/core/database.py 的 worker_heartbeats；
WorkerMonitor.vue 与 worker_monitor.py 直接读这张表，所以写完就能在页面上看到。
"""
from __future__ import annotations

import os
import sys

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.core.database import get_connection  # noqa: E402

# 每个 worker 各自保留的行数。
#
# 原来是**全表** 2000 行上限，而 5 个进程共用一张表、tracker 每 5s 写一行 ——
# 实测窗口只剩约 36 分钟，于是看板的 `/trend?hours=4` 根本取不到 4 小时数据。
# 改成按 worker 分组各留 KEEP_ROWS 行：间隔最密的 tracker（5s/轮）也能覆盖
# 2000×5s ≈ 2.8 小时，慢的 worker 覆盖更久。
KEEP_ROWS = 2000


def _trim(conn, worker: str) -> None:
    """只裁剪**本 worker** 的旧心跳，不动别人的。

    按 worker 分组裁剪而不是全表裁剪：全表上限会让写得最勤的进程把其它进程的
    历史挤掉（tracker 5s 一轮，几分钟就能冲掉 deep 的全部记录）。
    """
    conn.execute(
        "DELETE FROM worker_heartbeats WHERE worker=? AND id NOT IN ("
        "  SELECT id FROM worker_heartbeats WHERE worker=? "
        "  ORDER BY id DESC LIMIT ?)",
        (worker, worker, KEEP_ROWS))


def write_heartbeat(worker: str, pid: int, round_start: float, round_end: float,
                    processed: int, failed: int, note: str = "") -> None:
    """落一行心跳。**失败静默** —— 监控不能拖垮 worker 本身。"""
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO worker_heartbeats "
                "(worker, pid, round_start, round_end, duration, processed, failed, note) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (worker, pid, round_start, round_end, round(round_end - round_start, 2),
                 processed, failed, (note or "")[:300]))
            _trim(conn, worker)
            conn.commit()
    except Exception:
        pass


def write_worker_started(worker: str, pid: int, note: str) -> None:
    """启动标记行（round_start=0）。

    用来区分「从未运行过」和「运行过但已退出」：进程被 kill 时来不及写退出记录，
    监控页看到最后一行是 round_start>0 的旧心跳，就能判断这个 worker 已经不在了。

    duration 留 NULL —— round_start=0 是哨兵值，拿它算时长会得到无意义的巨大数。
    """
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO worker_heartbeats "
                "(worker, pid, round_start, round_end, duration, processed, failed, note) "
                "VALUES (?,?,0,?,NULL,0,0,?)",
                (worker, pid, __import__("time").time(), (note or "")[:300]))
            _trim(conn, worker)
            conn.commit()
    except Exception:
        pass
