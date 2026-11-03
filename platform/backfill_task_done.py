#!/usr/bin/env python3
"""全量回填 task_records.task_done（本地日志口径的 DONE）。

## 背景

「成功」数的是 complete.jsonl 行数 —— 流水线没抛异常就算成功，不代表任务做完了。
真实完成信号是每个 task-N.log 里的【Task_Done】标记（agent 自己在回复末尾打的）。

这个标记原本只存在于 task_traj_records.task_done，而补全快路径
（output_worker._fetch_tsr_entries_fast）刻意不拉该字段，导致 254 个实例、
243 万行的 task_done 恒为 0。本脚本直接用本地日志重算，落到 task_records.task_done。

## 成本

单日志 34.7ms（NFS），32 并发约 900 个/秒。
全库约 1000 万个 task-*.log → 约 185 分钟。

## 用法

    cd /root/openclaw-hive/platform
    python3 backfill_task_done.py                      # dry-run，只看统计
    python3 backfill_task_done.py --since-days 7       # 只处理最近 7 天
    python3 backfill_task_done.py --only <instance_id> # 单实例
    python3 backfill_task_done.py --apply              # 真正写入

--apply 会先备份 platform.db（sqlite3 backup API）。

## 判据

只写「日志里有终态行」的任务。日志里没有 `任务执行状态=` 的（还在写 / 被 kill）
留 NULL 不写 —— 否则会把「还没跑完」永久判成「没做完」，且再也不会重判。
"""
import argparse
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "platform.db")
DONE_MARKER = b"\xe3\x80\x90Task_Done\xe3\x80\x91"   # 【Task_Done】
STATUS_RE = re.compile(rb"\xe4\xbb\xbb\xe5\x8a\xa1\xe6\x89\xa7\xe8\xa1\x8c\xe7\x8a\xb6\xe6\x80\x81=")  # 任务执行状态=
CFG_RE = re.compile(r"BEGIN config=(\S+)")
WORKERS = 32


def logs_dir_of(config_path: str) -> str | None:
    """实例的 logs 目录。优先 config.yaml 的 task_output_path，回退 outputs/<stem>。"""
    inst_dir = os.path.dirname(config_path)
    stem = Path(config_path).stem
    cands = []
    try:
        from omegaconf import OmegaConf
        cfg = OmegaConf.load(config_path)
        out = str(cfg.run_config.task.task_output_path or "")
        if out:
            cands.append(os.path.join(out, "logs"))
            cands.append(os.path.join(out, stem, "logs"))
    except Exception:
        pass
    cands.append(os.path.join(inst_dir, "outputs", stem, "logs"))
    cands.append(os.path.join(inst_dir, "outputs", "config", "logs"))
    for d in cands:
        if os.path.isdir(d):
            return d
    return None


def scan_log(path: str):
    """返回 (config_name, done) 或 None（无终态行 / 读不到）。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    text = data.decode("utf-8", "replace")
    m = CFG_RE.search(text)
    if not m:
        return None
    if not STATUS_RE.search(data):
        return None                       # 没有终态行 → 留 NULL，下轮再判
    return (os.path.basename(m.group(1)), 1 if DONE_MARKER in data else 0)


def process_instance(inst: dict):
    """扫一个实例，返回 (updates, stats)。updates = [(done, instance_id, config_name)]"""
    ld = logs_dir_of(inst["config_path"])
    if not ld:
        return [], {"logs": 0, "done": 0, "skip": "无 logs 目录"}
    try:
        names = [f for f in os.listdir(ld) if f.startswith("task-") and f.endswith(".log")]
    except OSError as e:
        return [], {"logs": 0, "done": 0, "skip": f"列目录失败 {e}"}
    if not names:
        return [], {"logs": 0, "done": 0, "skip": "无 task 日志"}

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = [r for r in ex.map(lambda f: scan_log(os.path.join(ld, f)), names) if r]

    updates, done_n = [], 0
    for cn, d in results:
        updates.append((d, inst["id"], cn))
        done_n += d
    return updates, {"logs": len(names), "parsed": len(results), "done": done_n}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    ap.add_argument("--only", default=None, help="只处理指定实例 id")
    ap.add_argument("--since-days", type=int, default=0, help="只处理最近 N 天有活动的实例")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 个实例")
    args = ap.parse_args()

    if not os.path.exists(DB):
        print(f"找不到数据库: {DB}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sql = ("SELECT id, config_path, status, completed_tasks FROM task_instances "
           "WHERE completed_tasks > 0")
    params = []
    if args.only:
        sql += " AND id = ?"
        params.append(args.only)
    if args.since_days:
        import datetime
        cut = (datetime.datetime.now() - datetime.timedelta(days=args.since_days)).strftime("%Y-%m-%d")
        sql += " AND COALESCE(stopped_at, created_at) >= ?"
        params.append(cut)
    sql += " ORDER BY COALESCE(stopped_at, created_at) DESC"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    insts = [dict(r) for r in conn.execute(sql, tuple(params))]
    conn.close()

    print(f"待处理实例: {len(insts)}")
    if not insts:
        return

    if args.apply:
        print("写入模式（DB 7.8GB，自动备份耗时过长，已跳过；运行前请确认有外部备份）")

    wconn = sqlite3.connect(DB) if args.apply else None
    if wconn:
        wconn.execute("PRAGMA busy_timeout=60000")

    t0 = time.time()
    tot_logs = tot_done = tot_upd = tot_failed = 0
    print(f"\n{'实例':34} {'状态':10} {'日志':>7} {'DONE':>7} {'耗时':>7}")
    print("-" * 76)
    for i, inst in enumerate(insts, 1):
        t = time.time()
        updates, st = process_instance(inst)
        el = time.time() - t
        tot_logs += st.get("logs", 0)
        tot_done += st.get("done", 0)
        tot_upd += len(updates)
        note = st.get("skip", "")
        print(f"{inst['id'][:32]:34} {inst['status']:10} {st.get('logs', 0):>7} "
              f"{st.get('done', 0):>7} {el:>6.1f}s {note}")
        if updates and wconn:
            # 每实例单独提交：整批只在末尾 commit 会全程占住写锁，
            # 与 shallow worker / uvicorn 的写冲突，实测直接卡死（database is locked）。
            # 逐实例提交把持锁时间压到毫秒级，退避重试吸收瞬时冲突。
            for attempt in range(5):
                try:
                    wconn.executemany(
                        "UPDATE task_records SET task_done=? "
                        "WHERE instance_id=? AND config_name=? AND task_done IS NULL",
                        updates,
                    )
                    wconn.commit()
                    break
                except sqlite3.OperationalError as e:
                    try:
                        wconn.rollback()
                    except Exception:
                        pass
                    if attempt == 4:
                        print(f"  [fail] 写库失败 {inst['id']}: {e}")
                        tot_failed += len(updates)
                    else:
                        time.sleep(2 * (attempt + 1))

    if wconn:
        wconn.close()

    el = time.time() - t0
    print("-" * 76)
    print(f"合计: {len(insts)} 实例 / {tot_logs:,} 日志 / DONE {tot_done:,} 条 / {el/60:.1f} 分钟")
    if not args.apply:
        print(f"\n[dry-run] 可写入 {tot_upd:,} 行。确认无误后加 --apply 执行。")
    else:
        print(f"\n已写入 {tot_upd:,} 行。")
        print("worker 下次启动会自动补齐剩余（它只扫 task_done IS NULL 的行）。")


if __name__ == "__main__":
    main()
