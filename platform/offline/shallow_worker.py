# -*- coding: utf-8 -*-
"""shallow_worker：浅层分析进程，**按场景分车道、内部跑完两阶段**。

## 为什么是这个切分

浅层分析有两个阶段（都要做，顺序固定）：
  阶段 1（local）：读实例本地 `logs/task-<idx>.log` 判 DONE → `task_records.task_done`
  阶段 2（obs）  ：读 OBS 的 `logs/traj_stats_result.json` 定级 → `task_records.traj_level`

而真正需要**进程级隔离**的不是阶段，是**场景**：
  realtime —— 用户点了「输出」，要秒级看到结果
  auto     —— 背景全量同步，慢一点没关系

旧架构按阶段拆成 shallow-local / shallow-obs 两个进程，两种场景混在同一条队列里抢并发，
后果是线上实测 **23,828 行卡在阶段 2**（`local_state='done'` 但 `obs_state='queued'`）：
阶段 2 固定 16 并发，要和 92 万条背景行抢，用户点的那批排不上。

本文件把切分维度换成场景：一个进程内先跑阶段 1、再跑阶段 2，两种模式各起一个进程。
好处是**同一轮内阶段 1 刚判出 `task_done` 的行，阶段 2 立刻就能领走**（阶段 2 的 claim
条件含 `task_done IS NOT NULL`，这是两阶段的串联依赖）；旧架构下这要等 obs 进程的下一轮，
且还要和背景行竞争。

## 车道

| 模式 | claim 过滤 | 排序 | 并发(local/obs) | 间隔 |
|---|---|---|---|---|
| realtime | `priority >= 100` | `id`     | 16 / 8  | 2s |
| auto     | `priority < 100`  | `id DESC`| 64 / 16 | 5s |

两条车道的 WHERE 区间互斥 → CAS 不会重叠 → 两个进程同时跑互不抢行。
auto 用 `id DESC`（最新投递优先）是刻意的：积压几十万条时新批次应该先出结果。

并发差异沿用实测结论：本地读 NFS 约 900 个/秒且无配额；OBS 单次 0.4~2s、并发超过 32 会
触发 obsutil exit 8（请求数超限），16 是安全值。realtime 队列通常很短，不需要开满。

## 判定/定级逻辑完全复用

`judge_one` / `grade_one` 及各自的 `apply_results` 直接从 shallow_local / shallow_obs 导入 ——
那些函数已在线上验证（含旧版 harness 的「所有任务执行完成!」兜底、任务异常一步置 failed、
task_idx 三级回退等一堆踩过的坑），这里只负责「领哪些行、按什么顺序、跑几个线程」。

用法：
    python offline/shallow_worker.py --mode realtime        # 实时（用户点击）
    python offline/shallow_worker.py --mode auto            # 背景全量
    python offline/shallow_worker.py --mode auto --once     # 跑一轮即退出（调试）
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.core.database import (                        # noqa: E402
    get_connection, ensure_shallow_extra_columns)
from src import offline_analysis as oa                 # noqa: E402
from offline.heartbeat import write_heartbeat, write_worker_started  # noqa: E402
from offline import shallow_local as sl                # noqa: E402
from offline import shallow_obs as so                  # noqa: E402
# 只为取 tsr 轮内统计（tsr_round_summary）；定级本身在 shallow_obs.grade_one 里
from offline import output_worker as ow                # noqa: E402

# 每种模式的默认参数。realtime 要的是响应快（短间隔、队列短所以并发不用大），
# auto 要的是吞吐（并发拉满到各自的饱和点）。
MODE_DEFAULTS = {
    # realtime 的职责从「只处理点击」扩成「点击 + 所有 running 实例的自动跟踪」后,
    # 16/8 明显偏小：实测一轮只领 160 条、积压 26,650 条要 37 分钟。
    # 阶段1 是读本地 NFS（约 900 个/秒、无配额），提到 48 没问题。
    # 阶段2 保持 16 —— OBS 并发超 32 会返回 exit 8，而 auto 那边也占 16，加起来已到 32。
    "realtime": {"local": 48, "obs": 16, "interval": 2, "max_seconds": 10},
    "auto":     {"local": 64, "obs": 16, "interval": 5, "max_seconds": 30},
}

# 回收超时 claimed 的最小间隔（秒）。见 run_once 里的说明：每轮都回收会让
# 空闲 worker 也每 2~5s 抢一次写锁，在 9.9GB 的库上直接打爆 busy_timeout。
RECLAIM_EVERY_SEC = int(os.environ.get("SHALLOW_RECLAIM_EVERY_SEC", "300"))
# 上次回收的时间戳（进程内状态；两种模式各自一个进程，互不影响）
_last_reclaim_at = 0.0


def _worker_name(mode: str) -> str:
    """心跳里的 worker 名。监控页按这个名字画曲线、判存活。"""
    return f"shallow-{mode}"


def run_stage1(lane: str, concurrency: int, inst_cache: dict,
               pool: ThreadPoolExecutor) -> dict:
    """阶段 1：本地日志判 DONE。返回 sl.apply_results 的统计 + claimed。

    领取与回写都复用 shallow_local 的函数，这里只传车道。
    """
    with get_connection() as conn:
        items = sl.claim(conn, concurrency, lane=lane)
    if not items:
        return {"claimed": 0}

    results = []
    futs = {pool.submit(sl.judge_one, it, inst_cache): it for it in items}
    for fut in as_completed(futs):
        it = futs[fut]
        try:
            state, done_val, note = fut.result()
        except Exception as e:
            state, done_val, note = "queued", None, f"判定异常: {e}"
        results.append((it, state, done_val, note))

    st = sl.apply_results(results)
    st["claimed"] = len(items)
    return st


def run_stage2(lane: str, concurrency: int, inst_cache: dict, obsutil: str,
               origin: str, pool: ThreadPoolExecutor) -> dict:
    """阶段 2：OBS 定级。返回 so.apply_results 的统计 + claimed。

    必须在阶段 1 之后跑：claim 条件含 `task_done IS NOT NULL`，
    阶段 1 本轮刚写出的 task_done 在这里立刻生效。
    """
    with get_connection() as conn:
        items = so.claim(conn, concurrency, lane=lane)
    if not items:
        return {"claimed": 0}

    results = []
    futs = {pool.submit(so.grade_one, it, inst_cache, obsutil, origin): it
            for it in items}
    for fut in as_completed(futs):
        it = futs[fut]
        try:
            state, level, note = fut.result()
        except Exception as e:
            state, level, note = "queued", None, f"定级异常: {e}"
        results.append((it, state, level, note))

    st = so.apply_results(results)
    st["claimed"] = len(items)
    return st


def run_once(lane: str, local_conc: int, obs_conc: int, obsutil: str, origin: str,
             max_seconds: float = 0) -> dict:
    """跑一轮：阶段 1 → 阶段 2，必要时在轮内连续领取。

    max_seconds > 0 时在同一轮里反复跑两阶段，直到两边队列都空或超时。
    为什么要循环：claim 的 LIMIT 是并发数，每轮只领 64 条就 sleep，68 万条要跑 5 小时，
    而实测判 64 条只要 0.15s —— 时间全耗在轮间睡眠上。

    线程池与 inst_cache 提到循环外：
      - 每轮新建线程池本身有开销
      - inst_cache 存 (inst, logs_dir, idx_map)，而 _logs_dir 每次都要 OmegaConf.load
        解析 config.yaml（实测 9.1ms/次）；每轮清空等于每轮重付一次
      - 两阶段共用一个 cache：阶段 2 的 grade_one 也要 inst，缓存键不冲突
        （阶段 2 自己的 idx_map 用 ("__idx_map__", iid) 做键）
    """
    total = {"s1_done": 0, "s1_claimed": 0, "s1_skipped": 0,
             "s2_done": 0, "s2_claimed": 0, "s2_skipped": 0}
    t_start = time.time()
    # 两阶段的 inst_cache 语义不同（阶段 1 存三元组、阶段 2 存 inst 本身），各用一个
    cache1: dict = {}
    cache2: dict = {}

    # 回收超时 claimed：**节流到 RECLAIM_EVERY_SEC 一次**，不是每轮都做。
    #
    # 回收的目的只是救「worker 被 kill 后永久停在 claimed」的行，这种事几分钟
    # 处理一次完全够。而本函数每 2~5s 跑一轮，每轮都回收等于每轮都开写事务：
    # SQLite 的 WAL 只允许一个写者，4 个 worker + 4 个 uvicorn 的 queue_scheduler
    # 一起抢，9.9GB 的库触发 checkpoint 时独占写锁远超 busy_timeout(15s)，
    # 线上就报成满屏 `database is locked`（连待办 0 行的 shallow-auto 也在抢）。
    # 配合 reclaim_stuck 内部的「0 行短路」，空闲时一个写事务都不会开。
    global _last_reclaim_at
    if time.time() - _last_reclaim_at >= RECLAIM_EVERY_SEC:
        _last_reclaim_at = time.time()
        with get_connection() as conn:
            n1 = sl.reclaim_stuck(conn)
            n2 = so.reclaim_stuck(conn)
            if n1 or n2:
                conn.commit()
        if n1 or n2:
            print(f"[{lane}] 回收超时 claimed: 阶段1={n1} 阶段2={n2}", flush=True)

    # 线程池按两阶段里较大的并发开，两阶段串行复用同一个池
    with ThreadPoolExecutor(max_workers=max(local_conc, obs_conc)) as pool:
        while True:
            st1 = run_stage1(lane, local_conc, cache1, pool)
            total["s1_claimed"] += st1.get("claimed", 0)
            total["s1_done"] += st1.get("done", 0) + st1.get("done_failed", 0)
            total["s1_skipped"] += st1.get("skipped", 0)

            st2 = run_stage2(lane, obs_conc, cache2, obsutil, origin, pool)
            total["s2_claimed"] += st2.get("claimed", 0)
            total["s2_done"] += st2.get("done", 0)
            total["s2_skipped"] += st2.get("skipped", 0)

            # 两阶段都没活了 → 本轮结束
            if not st1.get("claimed") and not st2.get("claimed"):
                break
            if max_seconds <= 0 or (time.time() - t_start) >= max_seconds:
                break
    return total


def main() -> None:
    ap = argparse.ArgumentParser(
        description="浅层分析 worker：按场景分车道，内部跑完 local+obs 两阶段")
    ap.add_argument("--mode", choices=("realtime", "auto"), required=True,
                    help="realtime=只处理用户点击的行；auto=只处理背景行（最新优先）")
    ap.add_argument("--once", action="store_true", help="跑一轮即退出")
    ap.add_argument("--local-concurrency", type=int, default=None,
                    help="阶段 1 并发（默认 realtime=16 / auto=64）")
    ap.add_argument("--obs-concurrency", type=int, default=None,
                    help="阶段 2 并发（默认 realtime=8 / auto=16；OBS 配额上限 ~32）")
    ap.add_argument("--interval", type=int, default=None,
                    help="空闲时轮询间隔秒（默认 realtime=2 / auto=5）")
    ap.add_argument("--max-seconds", type=float, default=None,
                    help="单轮最长连续处理秒数（0=不限）")
    ap.add_argument("--cache-dir", dest="origin", default=oa.DEFAULT_OUTPUT_CACHE)
    ap.add_argument("--obsutil", default=oa.DEFAULT_OBSUTIL)
    a = ap.parse_args()

    d = MODE_DEFAULTS[a.mode]
    local_conc = a.local_concurrency if a.local_concurrency is not None else d["local"]
    obs_conc = a.obs_concurrency if a.obs_concurrency is not None else d["obs"]
    interval = a.interval if a.interval is not None else d["interval"]
    max_seconds = a.max_seconds if a.max_seconds is not None else d["max_seconds"]

    origin = os.path.abspath(a.origin)
    os.makedirs(origin, exist_ok=True)
    name = _worker_name(a.mode)
    print(f"[{a.mode}] 启动: 阶段1并发={local_conc} 阶段2并发={obs_conc} "
          f"interval={interval}s max_seconds={max_seconds} origin={origin}", flush=True)

    # 自愈补列：本进程写 local_state/obs_state 两维，但同表的 artifact 维由
    # task_tracker / output_worker 写入 —— 缺列时它们会静默失败，这里先补齐。
    ensure_shallow_extra_columns()
    write_worker_started(name, os.getpid(),
                         f"started mode={a.mode} local={local_conc} obs={obs_conc}")

    while True:
        t0 = time.time()
        # 必须先初始化：异常分支之后还要读 st.get(...)，
        # 未赋值会 UnboundLocalError 把整个进程带崩（deep worker 踩过这个坑）。
        st: dict = {}
        try:
            st = run_once(a.mode, local_conc, obs_conc, a.obsutil, origin, max_seconds)
            busy = st.get("s1_claimed", 0) + st.get("s2_claimed", 0)
            if busy:
                # tsr 统计汇总成一行（替代原来每任务 2 行的 per-task print）。
                # 「无tsr(走日志兜底)」占多数是正常的 —— 2026-08 及更早的批次
                # harness 不写 traj_stats_result.json，定级靠本地日志兜底。
                tsr = ow.tsr_round_summary()
                print(f"[{a.mode}] 阶段1(领={st['s1_claimed']} done={st['s1_done']}) "
                      f"阶段2(领={st['s2_claimed']} done={st['s2_done']}) "
                      f"{time.time()-t0:.2f}s"
                      + (f" | tsr: {tsr}" if tsr else ""), flush=True)
            # 每轮都写心跳（哪怕 0 条）：监控页靠「最后心跳时间」判断进程是否还活着，
            # 只在有产出时写会让空闲的 worker 看起来已经死了。
            # processed 取两阶段 done 之和 —— 这是本 worker 真正的产出。
            write_heartbeat(
                name, os.getpid(), t0, time.time(),
                st.get("s1_done", 0) + st.get("s2_done", 0),
                st.get("s1_skipped", 0) + st.get("s2_skipped", 0),
                f"阶段1领={st.get('s1_claimed',0)} 阶段2领={st.get('s2_claimed',0)}",
            )
        except KeyboardInterrupt:
            print(f"[{a.mode}] 收到中断，退出", flush=True)
            return
        except Exception as e:
            print(f"[{a.mode}] 本轮异常: {e}", flush=True)
            write_heartbeat(name, os.getpid(), t0, time.time(), 0, 1,
                            f"本轮异常: {str(e)[:150]}")
        if a.once:
            break
        # 队列还有活就不睡太久：只领了并发数条，固定长睡会让队列空转
        busy = st.get("s1_claimed", 0) + st.get("s2_claimed", 0)
        time.sleep(max(1, interval) if not busy else 1)


if __name__ == "__main__":
    main()
