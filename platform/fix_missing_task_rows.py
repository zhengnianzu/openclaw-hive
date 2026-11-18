#!/usr/bin/env python3
"""一次性修复：让终态实例重走「终态最后同步」，补齐 task_records 缺失行。

## 背景

在线同步 _sync_task_records 只在实例 running 时跑，且是 8s 轮询 + 按文件变化增量驱动。
最后一个任务收尾到实例转终态之间若不足一轮（实测实例 #2794 只差 4s），该任务的
task-N.log 就不会被任何一轮捕获 → task_records 永久缺这一行。

本该兜住的是 _refresh_running_once 的终态最后同步，但它原来的判据是
「task_records 整表为空(0 行)」，兜不住**部分缺行**；而且不论补没补都无条件把
task_done_sync 置 1，于是永久封死（该实例再也不会被 refresher 选中）。

首页 DONE 列（instances.py:_done_counts）数的是 task_records 里 task_done=1 的行数，
缺的那行既不在分子也不在分母 → #2794 显示「完成 3 个任务，DONE 只有 2」。

代码侧已修（判据改为 have < total_tasks，见 instances.py 终态分支）。本脚本只做
存量数据：把受影响的终态实例 task_done_sync 置回 0，让修好的 refresher 自己补。

## 为什么不直接 INSERT

补行需要解析 config.yaml 定位 logs 目录、扫 task-*.log 取 config= 与终态行、
算 error_category、还要配合 eval/traj 缓存 —— 这些逻辑在 _sync_task_records /
_seed_task_records_from_logs 里已经有且是权威实现。重写一份必然漂移。
置回 task_done_sync=0 复用现成路径，幂等且零重复逻辑。

## 判据

只选「磁盘日志数 > 库里行数」的实例 —— 即日志确实在盘上、只是没写进库。
不能只看 `have < total_tasks`：stopped 实例大量任务压根没派发（无日志），
那不是缺口，选上只会让 refresher 每轮空转重扫。

## 用法

    cd /root/openclaw-hive/platform
    python3 -I fix_missing_task_rows.py                 # dry-run，只统计
    python3 -I fix_missing_task_rows.py --limit 50      # 只看/改最近 50 个
    python3 -I fix_missing_task_rows.py --apply         # 真正置位

--apply 前会备份 platform.db（只改这一张表的一列，task_data.db 不动）。
置位后由后端后台 refresher 逐轮补齐；也可重启后端加速。
"""
import argparse
import glob
import os
import shutil
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "platform.db")
TASK_DATA = os.path.join(HERE, "database", "task_data.db")
TERMINAL = ("completed", "finished", "stopped")
WORKERS = 32


def logs_dir_of(config_path: str) -> str | None:
    """实例的 task-*.log 目录。与 output_worker._task_log_path 同款布局：
    <实例目录>/outputs/config/logs。另试 config.yaml 的 task_output_path。"""
    inst_dir = os.path.dirname(config_path)
    cands = [os.path.join(inst_dir, "outputs", "config", "logs")]
    try:
        from omegaconf import OmegaConf
        cfg = OmegaConf.load(config_path)
        out = str(cfg.run_config.task.task_output_path or "")
        if out:
            cands.append(os.path.join(out, "logs"))
    except Exception:
        pass
    for d in cands:
        if os.path.isdir(d):
            return d
    return None


def count_logs(inst: dict) -> tuple[dict, int]:
    """数该实例磁盘上的 task-*.log 个数（NFS 上一次 glob，不读内容）。"""
    try:
        d = logs_dir_of(inst["config_path"])
        n = len(glob.glob(os.path.join(d, "task-*.log"))) if d else 0
    except Exception:
        n = 0
    return inst, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    ap.add_argument("--limit", type=int, default=0, help="只处理最近 N 个实例")
    ap.add_argument("--only", help="只处理单个 instance_id")
    args = ap.parse_args()

    if not os.path.exists(TASK_DATA):
        sys.exit(f"找不到 {TASK_DATA}")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS td", (TASK_DATA,))

    sql = (
        "SELECT ti.id, ti.seq, ti.status, ti.total_tasks, ti.config_path, "
        "       ti.task_done_sync, "
        "  (SELECT COUNT(*) FROM td.task_records tr WHERE tr.instance_id=ti.id) have "
        "FROM task_instances ti "
        f"WHERE ti.status IN ({','.join('?' * len(TERMINAL))}) "
        "  AND COALESCE(ti.total_tasks,0) > 0 "
        "  AND (SELECT COUNT(*) FROM td.task_records tr WHERE tr.instance_id=ti.id) "
        "      < ti.total_tasks "
    )
    params = list(TERMINAL)
    if args.only:
        sql += "  AND ti.id = ? "
        params.append(args.only)
    sql += "ORDER BY ti.created_at DESC"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"

    cands = [dict(r) for r in conn.execute(sql, params)]
    print(f"候选（行数 < total_tasks 的终态实例）: {len(cands)}")
    if not cands:
        return

    # 逐个数磁盘日志，筛出「日志在盘上但没写进库」的真实缺口
    t0 = time.time()
    hits = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for inst, n_logs in ex.map(count_logs, cands):
            if n_logs > inst["have"]:
                inst["n_logs"] = n_logs
                hits.append(inst)
    print(f"其中「磁盘日志数 > 库中行数」: {len(hits)}  (扫盘 {time.time() - t0:.1f}s)")
    if not hits:
        return

    total_missing = sum(h["n_logs"] - h["have"] for h in hits)
    by_status = {}
    for h in hits:
        by_status[h["status"]] = by_status.get(h["status"], 0) + 1
    print(f"可补回的行数合计: {total_missing}")
    print(f"按状态: {by_status}")
    print("\n最近 15 个:")
    for h in hits[:15]:
        print(f"  seq={h['seq']:<6} {h['status']:<10} "
              f"rows={h['have']:<6} logs={h['n_logs']:<6} total={h['total_tasks']}")

    already = [h for h in hits if h["task_done_sync"] == 0]
    todo = [h for h in hits if h["task_done_sync"] != 0]
    print(f"\n已是 task_done_sync=0（refresher 本就会处理）: {len(already)}")
    print(f"需要置回 0: {len(todo)}")

    if not args.apply:
        print("\n[dry-run] 未写入。加 --apply 真正置位。")
        return
    if not todo:
        print("\n无需写入。")
        return

    bak = f"{DB}.bak.missingrows.{time.strftime('%Y%m%d%H%M%S')}"
    shutil.copy2(DB, bak)
    print(f"\n已备份主库 -> {bak}")

    conn.executemany(
        "UPDATE task_instances SET task_done_sync=0 WHERE id=?",
        [(h["id"],) for h in todo],
    )
    conn.commit()
    print(f"已把 {len(todo)} 个实例的 task_done_sync 置回 0。")
    print("后端后台 refresher 会逐轮调用 _sync_task_records(full=True) 补齐；"
          "每轮只处理 task_done_sync=0 的终态实例，补过即置位，不会反复重扫。")


if __name__ == "__main__":
    main()
