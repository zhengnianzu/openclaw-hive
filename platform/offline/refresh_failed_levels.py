#!/usr/bin/env python3
"""存量 failed 行批量刷新：用实例本地日志兜底重判，把「实际成功」的 failed 占位翻案。

背景
----
task_records.traj_level='failed' 是「不可分级占位」，**不等于任务失败**。历史批次里大量
行的轨迹其实跑完了（主 log 含「【Task_Done】」标记），只是 tsr 缺失/空壳（如 openjiuwen
老镜像写死 harness_home=~/.openclaw、trajectory=null、计数全零）导致快路径无法分级。

本脚本对这些 failed 行逐行查实例本地 `outputs/config/logs/task-<idx>.log`（零 OBS 成本），
命中「【Task_Done】」即按 oa.log_fallback_grade 重判等级（≥L1.5，有 evaluator 分数则抬到
L2/L3），同步刷新 task_records 与 task_traj_records；未命中/日志不可读的行保持 failed 不动。

用法
----
    python3 -m offline.refresh_failed_levels                 # dry-run：只统计，不写库
    python3 -m offline.refresh_failed_levels --apply          # 实际写库
    python3 -m offline.refresh_failed_levels --apply --instance <id>   # 只刷单实例
    python3 -m offline.refresh_failed_levels --apply --limit 5000      # 限量（调试）

安全
----
- 分批事务提交（BATCH_COMMIT 行），busy_timeout 加长，避免长时间独占 WAL 写锁阻塞在线 worker。
- 只 UPDATE traj_level='failed' 的行（WHERE 再限定一次），幂等：重复运行只对残留 failed 生效。
- 不触碰 task_records.updated_at（保持在线写入时间）；traj 行更新 updated_at（本轮改动时间）。
- dry-run 为默认；务必先跑一遍看命中率再 --apply。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import offline_analysis as oa  # noqa: E402
from api.core.config import settings   # noqa: E402

BATCH_COMMIT = 2000          # 每 N 行提交一次
PROGRESS_EVERY = 20000       # 每 N 行打印进度
DEFAULT_HT = "openclaw"      # 实例 harness 取不到时的回退（与 output_worker 一致）
SCAN_WORKERS = 16            # 扫描并发：本地日志读是 NFS I/O 密集，串行 ~20ms/行太慢


def _instance_harness_type(inst: dict) -> str:
    """实例 harness 类型：优先 config_snapshot（不依赖挂载），回退磁盘 config.yaml。

    与 offline.output_worker._instance_harness_type 同款逻辑。fail 行分级时 harness 只影响
    写库的 harness 列与兜底 entry 的 harness 字段（兜底路径不再过家族门，等级与 harness 无关）。
    """
    snap = inst.get("config_snapshot")
    if snap:
        try:
            import yaml
            ht = (yaml.safe_load(snap) or {}).get("run_config", {}).get("harness_type")
            if ht:
                return str(ht)
        except Exception:
            pass
    try:
        from omegaconf import OmegaConf
        cfg = OmegaConf.load(inst["config_path"])
        ht = getattr(cfg.run_config, "harness_type", None)
        return str(ht) if ht else DEFAULT_HT
    except Exception:
        return DEFAULT_HT


def _task_log_path(inst: dict, task_idx: int) -> str:
    return os.path.join(os.path.dirname(inst["config_path"]),
                        "outputs", "config", "logs", f"task-{task_idx}.log")


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(settings.DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def main() -> None:
    ap = argparse.ArgumentParser(description="存量 failed 行批量刷新（本地日志兜底重判）")
    ap.add_argument("--apply", action="store_true", help="实际写库（默认 dry-run 只统计）")
    ap.add_argument("--instance", default=None, help="只刷指定 instance_id")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 行（0=不限，调试用）")
    a = ap.parse_args()

    con = _conn()
    where = ["tr.traj_level='failed'"]
    params: list = []
    if a.instance:
        where.append("tr.instance_id=?")
        params.append(a.instance)
    sql = (f"SELECT tr.instance_id, tr.task_idx, tr.config_name, tr.status "
           f"FROM task_records tr WHERE {' AND '.join(where)} "
           f"ORDER BY tr.instance_id, tr.task_idx")
    rows = [dict(r) for r in con.execute(sql, tuple(params)).fetchall()]
    if a.limit:
        rows = rows[:a.limit]
    print(f"[refresh] failed 行 {len(rows)}（apply={a.apply}），"
          f"本地日志兜底重判（并发 {SCAN_WORKERS}）…", flush=True)

    # 实例元数据一次性取全（避免逐行查库；并发扫描时字典只读，线程安全）
    inst_meta: dict[str, dict] = {}
    for r in con.execute("SELECT id, config_path, config_snapshot FROM task_instances"):
        inst_meta[r["id"]] = {"id": r["id"], "config_path": r["config_path"],
                              "config_snapshot": r["config_snapshot"]}
    # harness 类型按实例缓存（解析 config 有成本，同实例多行复用）
    ht_cache: dict[str, str] = {}

    def scan_one(r: dict) -> tuple | None:
        """单行扫描：命中 Task_Done → (iid, config_name, task_idx, level, entry, ht)。

        返回 None + 更新计数器不便于并发，故返回值同时携带 miss 原因由调用方归类。
        """
        iid = r["instance_id"]
        inst = inst_meta.get(iid)
        if not inst:
            return ("__noinst__", None)
        lp = _task_log_path(inst, r["task_idx"])
        if not os.path.isfile(lp):
            return ("__nolog__", None)
        ht = ht_cache.get(iid)
        if ht is None:
            ht = ht_cache[iid] = _instance_harness_type(inst)
        fb = oa.log_fallback_grade(lp, ht, r.get("status"))
        if not fb:
            return ("__miss__", None)
        entry, level = fb
        return ("hit", (iid, r["config_name"], r["task_idx"], level, entry, ht))

    n_hit = n_miss = n_nolog = n_noinst = 0
    n_traj_rows = 0
    level_hist: dict[str, int] = defaultdict(int)
    pending: list[tuple] = []   # (iid, config_name, task_idx, level, entry, ht)
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS,
                            thread_name_prefix="refresh-scan") as pool:
        for k, fut in enumerate(as_completed([pool.submit(scan_one, r) for r in rows]), 1):
            kind, pay = fut.result()
            if kind == "hit":
                n_hit += 1
                level_hist[pay[3]] += 1
                pending.append(pay)
            elif kind == "__miss__":
                n_miss += 1
            elif kind == "__nolog__":
                n_nolog += 1
            else:
                n_noinst += 1
            if k % PROGRESS_EVERY == 0:
                print(f"  进度 {k}/{len(rows)}  命中={n_hit} 未命中={n_miss} "
                      f"无日志={n_nolog}  用时 {time.time()-t0:.0f}s", flush=True)

    print(f"\n[refresh] 扫描完成：命中 Task_Done {n_hit} / 未命中 {n_miss} / "
          f"无日志 {n_nolog} / 无实例 {n_noinst}")
    if level_hist:
        print("  等级分布: " + " ".join(f"{k}={v}" for k, v in sorted(level_hist.items())))
    print(f"  可翻案行数（task_records）: {n_hit}")

    if not a.apply:
        print("\n[dry-run] 未写库。加 --apply 实际刷新。")
        return

    # ---- 写库：分批提交 ----
    print(f"\n[refresh] 开始写库（{n_hit} 行，每 {BATCH_COMMIT} 行提交）…", flush=True)
    for j, (iid, config_name, task_idx, level, entry, ht) in enumerate(pending, 1):
        comp = entry.get("evaluator_completion")
        con.execute(
            "UPDATE task_records SET traj_level=?, "
            "eval_completion=COALESCE(?, eval_completion), "
            "eval_score=COALESCE(?, eval_score), "
            "gate=COALESCE(?, gate) "
            "WHERE instance_id=? AND config_name=? AND traj_level='failed'",
            (level, comp, comp, int(bool(entry.get("passed_gate"))),
             iid, config_name),
        )
        cur = con.execute(
            "UPDATE task_traj_records SET level=?, harness=?, passed_gate=?, has_eval=?, "
            "task_done=?, completion=?, plain_rounds=?, shallow_status='log_fallback', "
            "shallow_error='存量刷新：无有效 tsr，日志兜底', updated_at=CURRENT_TIMESTAMP "
            "WHERE instance_id=? AND config_name=? AND level='failed'",
            (level, entry.get("harness", ht), int(bool(entry.get("passed_gate"))),
             int(bool(entry.get("has_eval"))), int(bool(entry.get("task_done"))),
             comp, entry.get("plain_rounds"), iid, config_name),
        )
        n_traj_rows += cur.rowcount
        if j % BATCH_COMMIT == 0:
            con.commit()
            print(f"  已提交 {j}/{n_hit}（traj 行 {n_traj_rows}）用时 {time.time()-t0:.0f}s",
                  flush=True)
    con.commit()
    print(f"\n[refresh] 完成：task_records 刷新 {n_hit} 行；"
          f"task_traj_records 刷新 {n_traj_rows} 行；用时 {time.time()-t0:.0f}s")
    con.close()


if __name__ == "__main__":
    main()
