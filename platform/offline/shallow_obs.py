# -*- coding: utf-8 -*-
"""shallow-obs：浅层分析的「OBS 定级」进程。

## 职责

只做一件事：给 config 打 L0/L1/L1.5/L2/L3，写 `task_records.traj_level`。

数据源是 OBS 上的 `logs/traj_stats_result.json`（约 1KB，harness 预先算好的统计）。
拿不到 tsr 时退回读本地日志判等级（log_fallback，≥L1.5），都没有则标 failed。

## 为什么独立进程

本地判定（shallow-local）约 900 个/秒、无配额限制；OBS 单次 0.4~2s、且并发超过
32 会触发 obsutil exit 8（请求数超限）。两者速度差一个数量级，挤在一个线程池里
快的会被慢的拖住。拆开后这里固定 8 并发，慢就慢，不影响 DONE 的产出。

## 只处理「任务成功」

`任务异常` 的行没有轨迹可定级，直接写 failed 占位即可，不需要碰 OBS
（实测最近 100 实例里这类占 72,734 行，而真正需 OBS 的只有 1,209 行）。
这一步在领取阶段就过滤掉，把 OBS 配额全部留给需要它的行。

用法：
    python offline/shallow_obs.py                 # 常驻
    python offline/shallow_obs.py --once          # 跑一轮即退出
    python offline/shallow_obs.py --concurrency 8
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.core.database import (                        # noqa: E402
    get_connection, ensure_shallow_extra_columns)
from src import offline_analysis as oa                # noqa: E402
from offline.task_tracker import LANE_REALTIME_MIN    # noqa: E402
from offline.output_worker import (                    # noqa: E402
    _fetch_tsr_entries_fast,
    _instance_harness_type,
    _logs_dir,
    _make_obs_base_for,
    _read_task_log_for,
    oa as _owa,
)
from offline.heartbeat import write_heartbeat, write_worker_started  # noqa: E402

WORKER_NAME = "shallow-obs"
# OBS 侧配额：实测 32 并发才触发 obsutil exit 8（请求数超限），16 是安全值。
# 原值 8 时实测吞吐只有 1.4 条/秒（单轮 1.09s 只能领 8 条），提到 16 后翻倍；
# 真正的瓶颈是 claim 查询（0.7~1.3s），所以同时加了 idx_shallow_queue_obs 部分索引。
DEFAULT_CONCURRENCY = 16
DEFAULT_INTERVAL = 5
CLAIM_TIMEOUT_SEC = 900
MAX_ATTEMPTS = 3


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def reclaim_stuck(conn) -> int:
    """回收超时 claimed（阶段 2 维）。与阶段 1 的 claimed 互不干扰。

    **先 SELECT 探测、命中 0 行就直接返回**，不开写事务 ——
    理由同 shallow_local.reclaim_stuck：WAL 只允许一个写者，无条件 UPDATE
    会让没活干的 worker 每轮也抢一次写锁，9.9GB 库 checkpoint 时直接把
    busy_timeout 耗尽，到处 `database is locked`。
    """
    now = time.time()
    cutoff = now - CLAIM_TIMEOUT_SEC
    hit = conn.execute(
        "SELECT 1 FROM shallow_queue "
        "WHERE obs_state='claimed' AND obs_claimed_at < ? LIMIT 1",
        (cutoff,),
    ).fetchone()
    if not hit:
        return 0

    conn.execute(
        "UPDATE shallow_queue SET obs_state='skipped', note='obs claimed 超时且已达最大尝试次数' "
        "WHERE obs_state='claimed' AND obs_claimed_at < ? AND obs_attempts >= ?",
        (cutoff, MAX_ATTEMPTS),
    )
    cur = conn.execute(
        "UPDATE shallow_queue SET obs_state='queued', obs_claimed_by=NULL, obs_claimed_at=NULL "
        "WHERE obs_state='claimed' AND obs_claimed_at < ? AND obs_attempts < ?",
        (cutoff, MAX_ATTEMPTS),
    )
    return cur.rowcount


def claim(conn, limit: int, lane: str | None = None) -> list[dict]:
    """CAS 领取「待定级」的条目（阶段 2，obs_state 这一维）。

    领取条件：
      - obs_state='queued'（obs 自己还没处理过）
      - task_records.traj_level IS NULL（还没定级）
      - task_records.task_done IS NOT NULL（阶段 1 已经判过，说明本地日志完整；
        未判说明还在写，定级会拿到不完整的 tsr）—— 这是两阶段的**串联依赖**

    **关键**：不看共用的 state，也不看 local_state。这样阶段 1 把 local_state
    置 done 之后，这一条仍然能被阶段 2 正常领到 —— 改前共用 state 时，阶段 1 一完成
    就把 state 置成 done，阶段 2 永远领不到，traj_level 一直卡在 NULL。

    lane：场景车道，语义与 shallow_local.claim 完全一致
      'realtime' → priority>=100，id 升序
      'auto'     → priority<100，id DESC（最新投递优先）
      None       → 不分车道（单独作为调试 CLI 跑时的旧行为）
    """
    where = ["q.obs_state='queued'", f"(q.priority >= {LANE_REALTIME_MIN} OR q.obs_attempts < ?)"]
    params: list = [MAX_ATTEMPTS]
    order = "q.priority DESC, q.id"
    if lane == "realtime":
        where.append(f"q.priority >= {LANE_REALTIME_MIN}")
        # 实时车道内按 priority 降序优先：点击(100) 插到自动跟踪(90) 前面。
        # 只按 id 的话，用户新点的实例要排在自动跟踪的几万行后面
        # （实测实时车道曾积压 26,650 条 ≈ 37 分钟）。
        order = "q.priority DESC, q.id"
    elif lane == "auto":
        where.append(f"q.priority < {LANE_REALTIME_MIN}")
        order = "q.id DESC"
    params.append(limit)
    rows = conn.execute(
        # 用 EXISTS 子查询而不是 JOIN：JOIN 会让 SQLite 走 idx_task_records_level
        # 扫全部未定级行再逐条回查队列（实测 762~1407ms），EXISTS 则先走
        # idx_shallow_queue_obs 部分索引按 priority 取前 N 条，再逐条确认（1ms）。
        # 差 700 倍，这是 obs 吞吐上不去的真正原因。
        "SELECT q.id, q.instance_id, q.config_name, q.task_idx, q.obs_attempts "
        "FROM shallow_queue q "
        # attempts 上限只对自动发现的行生效，用户点击的（priority>=100）不限次 ——
        # 同 shallow_local.claim：点了没反应比「无限重试几个坏行」严重得多。
        "WHERE " + " AND ".join(where) +
        "  AND EXISTS(SELECT 1 FROM task_records tr "
        "             WHERE tr.instance_id=q.instance_id "
        "               AND tr.config_name=q.config_name "
        "               AND tr.traj_level IS NULL "
        "               AND tr.task_done IS NOT NULL) "
        f"ORDER BY {order} LIMIT ?",
        tuple(params),
    ).fetchall()
    me = worker_name()
    claimed = []
    for r in rows:
        cur = conn.execute(
            "UPDATE shallow_queue SET obs_state='claimed', obs_claimed_by=?, "
            "obs_claimed_at=?, obs_attempts=obs_attempts+1, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND obs_state='queued'",
            (me, time.time(), r["id"]),
        )
        if cur.rowcount == 0:
            continue
        claimed.append(dict(r))
    conn.commit()
    return claimed


def grade_one(item: dict, inst_cache: dict, obsutil: str, origin: str) -> tuple[str, str | None, str]:
    """给一条定级。返回 (state, level, note)。"""
    iid = item["instance_id"]
    cn = item["config_name"]
    inst = inst_cache.get(iid)
    if inst is None:
        with get_connection() as c:
            row = c.execute(
                "SELECT id, config_path, config_snapshot FROM task_instances WHERE id=?",
                (iid,)).fetchone()
        inst = dict(row) if row else None
        inst_cache[iid] = inst
    if not inst:
        return "skipped", "failed", "实例已不存在"

    # 任务异常 / 未执行的行：没有轨迹可定级，直接 failed，不碰 OBS
    with get_connection() as c:
        tr = c.execute(
            "SELECT status, error_code FROM task_records WHERE instance_id=? AND config_name=?",
            (iid, cn)).fetchone()
    status = tr["status"] if tr else None
    code = tr["error_code"] if tr else None
    if status != "任务成功":
        return "done", "failed", f"status={status}"

    inst_ht = _instance_harness_type(inst)
    obs_base = _make_obs_base_for(inst)
    leaf = os.path.splitext(cn)[0]
    task_obs = obs_base.rstrip("/") + "/" + leaf + "/"

    try:
        entries = _fetch_tsr_entries_fast(obsutil, task_obs, origin, inst_ht)
    except Exception as e:
        # OBS 侧瞬时错误：回队列重试（不进 skipped，否则这条永远没等级）
        return "queued", None, f"拉 tsr 失败: {str(e)[:150]}"

    if entries:
        e = entries[0]
        level = oa.compute_level(
            e.get("harness", inst_ht),
            e.get("tool_calls", 0),
            e.get("plain_rounds", 0),
            e.get("evaluator_completion"),
        )
        return "done", level, ""

    # 无有效 tsr：本地日志兜底（含 Task_Done 或旧版「所有任务执行完成!」即 ≥L1.5）
    #
    # task_idx 三级回退，与 shallow_local.judge_one 完全一致。
    # 这里原来只读 queue.task_idx，而 tracker 投递时 task_records 常常还没建行，
    # 于是 task_idx 为 NULL → 拼不出日志路径 → 兜底直接返回 None → 判 failed。
    # 实测某实例 39 条「任务成功」就是这样被误判成 failed 的（本地日志其实有
    # 「所有任务执行完成!」标记，log_fallback_grade 带 status 就能判出 L1.5）。
    logs_dir = _logs_dir(inst)
    idx = item.get("task_idx")
    if idx is None:
        with get_connection() as c2:
            r2 = c2.execute(
                "SELECT task_idx FROM task_records WHERE instance_id=? AND config_name=?",
                (iid, cn)).fetchone()
        if r2:
            idx = r2["task_idx"]
    if idx is None:
        # 由输入目录排序下标还原（hive 建队列就是 enumerate(sorted(files))）
        cached_map = inst_cache.get(("__idx_map__", iid))
        if cached_map is None:
            from offline.shallow_local import _task_idx_map
            cached_map = _task_idx_map(inst)
            inst_cache[("__idx_map__", iid)] = cached_map
        idx = cached_map.get(cn)

    log_path = os.path.join(logs_dir, f"task-{idx}.log") if idx is not None else None
    if log_path and os.path.isfile(log_path):
        # 必须传 status：旧版 harness 只打「所有任务执行完成!」，不带 status 时
        # log_fallback_grade 不采信这个标记（单独用有误判），结果一律判 failed。
        fb = oa.log_fallback_grade(log_path, inst_ht, status)
        if fb:
            return "done", fb[1], "日志兜底"
    return "done", "failed", "OBS 无有效 tsr"


def apply_results(results: list[tuple[dict, str, str | None, str]]) -> dict:
    """写回定级结果。只推进 obs_state 这一维，不碰 local_state 与共用 state。"""
    stat = {"done": 0, "queued": 0, "skipped": 0}
    tr_rows, q_rows = [], []
    for item, state, level, note in results:
        stat[state] = stat.get(state, 0) + 1
        if state == "done" and level:
            # 不碰 updated_at：该列语义是「在线 _sync_task_records 最后写入时间」
            tr_rows.append((level, item["instance_id"], item["config_name"]))
        q_rows.append((state, (note or "")[:200] or None, item["id"]))

    with get_connection() as conn:
        if tr_rows:
            conn.executemany(
                "UPDATE task_records SET traj_level=? "
                "WHERE instance_id=? AND config_name=? AND traj_level IS NULL",
                tr_rows,
            )
        if q_rows:
            conn.executemany(
                "UPDATE shallow_queue SET obs_state=?, note=?, "
                "obs_claimed_by=NULL, obs_claimed_at=NULL, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=?",
                q_rows,
            )
        conn.commit()
    return stat


def run_once(concurrency: int, obsutil: str, origin: str) -> dict:
    with get_connection() as conn:
        n_reclaim = reclaim_stuck(conn)
        items = claim(conn, concurrency)
    if n_reclaim:
        print(f"[obs] 回收超时 claimed {n_reclaim} 条", flush=True)
    if not items:
        return {"done": 0, "queued": 0, "skipped": 0, "claimed": 0}

    inst_cache: dict = {}
    results = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = {pool.submit(grade_one, it, inst_cache, obsutil, origin): it for it in items}
        for fut in as_completed(futs):
            it = futs[fut]
            try:
                state, level, note = fut.result()
            except Exception as e:
                state, level, note = "queued", None, f"定级异常: {e}"
            results.append((it, state, level, note))

    stat = apply_results(results)
    stat["claimed"] = len(items)
    return stat


def main() -> None:
    ap = argparse.ArgumentParser(description="浅层分析：OBS 定级")
    ap.add_argument("--once", action="store_true", help="跑一轮即退出")
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help=f"并发数（默认 {DEFAULT_CONCURRENCY}，OBS 配额限制）")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    ap.add_argument("--cache-dir", dest="origin", default=oa.DEFAULT_OUTPUT_CACHE)
    ap.add_argument("--obsutil", default=oa.DEFAULT_OBSUTIL)
    a = ap.parse_args()

    origin = os.path.abspath(a.origin)
    os.makedirs(origin, exist_ok=True)
    print(f"[obs] 启动: concurrency={a.concurrency} interval={a.interval}s origin={origin}",
          flush=True)
    # 自愈补列：同表的 artifact 维由 task_tracker / output_worker 写入。
    ensure_shallow_extra_columns()
    write_worker_started(WORKER_NAME, os.getpid(),
                         f"started concurrency={a.concurrency}")

    while True:
        t0 = time.time()
        st: dict = {}
        try:
            st = run_once(a.concurrency, a.obsutil, origin)
            if st.get("claimed"):
                print(f"[obs] 领取={st['claimed']} done={st.get('done',0)} "
                      f"留队={st.get('queued',0)} 跳过={st.get('skipped',0)} "
                      f"{time.time()-t0:.2f}s", flush=True)
            # 每轮都写（哪怕 0 条）：监控页靠最后心跳时间判断进程是否存活
            write_heartbeat(
                WORKER_NAME, os.getpid(), t0, time.time(),
                st.get("done", 0), st.get("skipped", 0),
                f"领取={st.get('claimed',0)} 留队={st.get('queued',0)}",
            )
        except KeyboardInterrupt:
            print("[obs] 收到中断，退出", flush=True)
            return
        except Exception as e:
            print(f"[obs] 本轮异常: {e}", flush=True)
            write_heartbeat(WORKER_NAME, os.getpid(), t0, time.time(), 0, 1,
                            f"本轮异常: {str(e)[:150]}")
        if a.once:
            break
        time.sleep(max(1, a.interval) if not st.get("claimed") else 1)


if __name__ == "__main__":
    main()
