# -*- coding: utf-8 -*-
"""backfill_finished_at：给历史实例回填 task_records.finished_at。

## 为什么需要

`finished_at`（任务完成时刻）是详情页成功率趋势图的时间轴。正常情况下由
`_compute_analyze` 的增量扫描顺手填入 —— 但那只对「还会被重扫」的实例生效
（running，或 task_records 为 0 行的兜底）。加列之前就已跑完的实例，
task_records 行是齐的、唯独 `finished_at` 为 NULL，画不出曲线。

本脚本扫这些实例的 `task-N.log`，把完成时刻补上。

## 只补最近 N 个实例

默认 100。全库有 2200+ 个实例，绝大多数是几周前的历史，没人会去看它们的
实时成功率曲线，而扫它们的日志要付几十分钟的 NFS IO。划一条线更划算。
更早的实例 `finished_at` 留 NULL，前端显示「暂无数据」。

想补更多就调 `--limit`，脚本是幂等的（只填 NULL，不覆盖已有值）。

## 并发

复用 output_worker 的做法：日志是 NFS 上的小文件，单个约 35ms，串行扫 1 万个
要 350s。16 并发后实测 5000 个任务约 0.9s。

用法：
    python offline/backfill_finished_at.py                  # 最近 100 个实例
    python offline/backfill_finished_at.py --limit 1        # 先验 1 个
    python offline/backfill_finished_at.py --limit 300
    python offline/backfill_finished_at.py --dry-run        # 只统计不写库
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.core.database import get_connection  # noqa: E402
# 复用已有的路径解析与正则：_logs_dir 处理了「config.yaml 的 task_output_path
# 优先、再回退几种目录布局」，实例搬迁过也能找到（硬编码 outputs/config/logs
# 曾经就是个线上 bug）。_FINAL_STATUS_RE 是全平台判任务终态的同一个正则。
from offline.output_worker import _FINAL_STATUS_RE, _logs_dir  # noqa: E402

CONCURRENCY = int(os.environ.get("BACKFILL_CONCURRENCY", "16"))
# 状态行里的 config=<文件名>。注意不是 output_worker 的 _BEGIN_CFG_RE
# （那个匹配 "BEGIN config="，是任务开始行）—— 我们要的是**终态行**上的 config。
_CONFIG_RE = re.compile(r"config=(?P<config>\S+)")
# 行首时间戳，logger datefmt '%Y-%m-%d %H:%M:%S'，本地时间无时区
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def scan_logs(logs_dir: str) -> dict[str, str]:
    """并发扫 task-N.log，返回 {config_name: finished_at}。

    取**最后一条**终态行的时间（与 _scan_task_file 口径一致）。
    行首时间戳解析不到时回退文件 mtime —— 有个近似时间也比没有强，
    趋势图只做相对时间轴。
    """
    try:
        names = [e.name for e in os.scandir(logs_dir)
                 if e.name.startswith("task-") and e.name.endswith(".log")]
    except OSError:
        return {}
    if not names:
        return {}

    def scan_one(fn: str):
        path = os.path.join(logs_dir, fn)
        cfg = ts = None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not _FINAL_STATUS_RE.search(line):
                        continue
                    mt = _TS_RE.match(line)
                    mc = _CONFIG_RE.search(line)
                    if mc:
                        cfg = os.path.basename(mc.group("config"))
                    ts = mt.group(1) if mt else None
        except OSError:
            return None
        if not cfg:
            return None
        if not ts:
            try:
                ts = time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(os.path.getmtime(path)))
            except OSError:
                return None
        return (cfg, ts)

    out: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for res in pool.map(scan_one, names):
            if res:
                out[res[0]] = res[1]
    return out


def backfill_one(inst: dict, dry_run: bool = False) -> tuple[int, int]:
    """回填一个实例，返回 (扫到的条数, 实际更新的行数)。"""
    logs_dir = _logs_dir(inst)
    if not logs_dir or not os.path.isdir(logs_dir):
        return 0, 0
    found = scan_logs(logs_dir)
    if not found or dry_run:
        return len(found), 0

    # 只填 NULL，不覆盖已有值 —— 在线扫描写入的时间同样来自日志，
    # 重复跑本脚本不该改动任何已有数据（幂等）。
    with get_connection() as conn:
        before = conn.total_changes
        conn.executemany(
            "UPDATE task_records SET finished_at=? "
            "WHERE instance_id=? AND config_name=? AND finished_at IS NULL",
            [(ts, inst["id"], cfg) for cfg, ts in found.items()],
        )
        updated = conn.total_changes - before
    return len(found), updated


def main() -> None:
    ap = argparse.ArgumentParser(
        description="回填 task_records.finished_at（详情页成功率趋势图的时间轴）")
    ap.add_argument("--limit", type=int, default=100,
                    help="补最近多少个实例（按 seq 倒序，默认 100）")
    ap.add_argument("--dry-run", action="store_true", help="只扫不写库")
    a = ap.parse_args()

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, seq, config_path, total_tasks, status FROM task_instances "
            "ORDER BY seq DESC LIMIT ?", (a.limit,)).fetchall()
    insts = [dict(r) for r in rows]
    print(f"[backfill] 待处理 {len(insts)} 个实例"
          f"（seq {insts[-1]['seq']} ~ {insts[0]['seq']}）"
          f"{' [dry-run]' if a.dry_run else ''}", flush=True)

    t_all = time.time()
    tot_found = tot_upd = n_skip = 0
    for i, inst in enumerate(insts, 1):
        t0 = time.time()
        try:
            found, upd = backfill_one(inst, a.dry_run)
        except Exception as e:
            print(f"  [warn] seq={inst['seq']} 失败: {e}", flush=True)
            continue
        tot_found += found
        tot_upd += upd
        if found == 0:
            n_skip += 1
            continue
        print(f"  [{i}/{len(insts)}] seq={inst['seq']} 扫到 {found} "
              f"更新 {upd} ({time.time()-t0:.1f}s)", flush=True)

    print(f"[backfill] 完成：扫到 {tot_found} 条，更新 {tot_upd} 行，"
          f"{n_skip} 个实例无日志可扫，总耗时 {time.time()-t_all:.1f}s", flush=True)


if __name__ == "__main__":
    main()
