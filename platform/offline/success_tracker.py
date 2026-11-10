# -*- coding: utf-8 -*-
"""success_tracker：任务完成事件的采集进程。

## 职责

如实记录「哪个 config 在什么时刻、以什么结果完成了」，写进 `task_events`，
供详情页成功率趋势图按时间窗口聚合。

不分析、不算分级、不碰 OBS、不投递任何队列 —— 与 task_tracker / shallow_worker
那套浅层分析流水线无关，只**复用**了它读 jsonl 的几个函数。

## 存事件，不存计数

一行 = 一次任务完成。同一个 config 先失败、重跑后成功，就是**两行**
（时间不同、结果不同），不做改判、不冲正。

这样历史曲线永远是「当时跑得怎么样」的如实记录，不会因为今天重跑成功就把
昨天那个失败峰抹掉。代价是累计总数会比 jsonl 行数多（同一任务计了两次），
与顶部「成功率」卡片不是同一口径 —— 那张卡片数的是 jsonl 当前行数。

## 两种数据来源

1. **回填（扫 task 日志）**：`task-N.log` 里「任务执行状态=」那行，行首自带
   `%Y-%m-%d %H:%M:%S` 时间戳 —— 这是任务**真正完成的时刻**。
   实例首次被跟踪时做一次，历史实例因此能立刻画出完整曲线。
2. **增量（读 jsonl）**：按 offset 增量读 complete/failed.jsonl，秒级发现新完成的
   任务。时间只能取「观测时刻」（文件里没有时间戳），最大滞后一个轮询周期。

两者都往 `task_events` 写，靠 `UNIQUE(instance_id, config_name, finished_at)`
幂等去重。日志源优先：同一条记录若两边都有，日志的时间更准。

## 跟谁

  · **正在跑的实例**（running/preparing）—— 自动跟踪，不需要任何操作
  · **登记过的实例** —— 用户点开「详情」页时后端写 success_requests

## offset 表必须与 task_tracker 分开

`tracker_state` 主键是 (instance_id, file_path)、没有属主列，两个进程共用会
互相推进对方的偏移，各自只能读到一部分行。

用法：
    python offline/success_tracker.py                  # 常驻，10s 一轮
    python offline/success_tracker.py --once           # 跑一轮即退出（调试）
    python offline/success_tracker.py --interval 15
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.core.database import get_connection  # noqa: E402
from offline.heartbeat import write_heartbeat, write_worker_started  # noqa: E402
# 复用 task_tracker 的读取层：目录解析、文件定位、带 inode+头指纹校验的增量读。
from offline.task_tracker import (  # noqa: E402
    RECORDS, find_records, read_new_lines, resolve_output_dir,
)

WORKER_NAME = "success-tracker"
DEFAULT_INTERVAL = 10
STATE_TABLE = "success_tracker_state"
ACTIVE_STATUSES = ("running", "preparing")

# task 日志里的状态行。与 api/routers/instances.py 的 _STATUS_RE 同款 ——
# 那边用它算 error_breakdown，这边用它取完成时刻，必须保持一致。
_STATUS_RE = re.compile(
    r"任务执行状态=(?P<status>任务成功|任务失败|任务异常)\s+error_code=(?P<code>\S+)")
_CONFIG_RE = re.compile(r"config=(?P<config>\S+)")
# 行首时间戳。logger 的 datefmt 是 '%Y-%m-%d %H:%M:%S'（settings/config.yaml），
# **本地时间无时区**，与本表其它时间列口径一致。
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def pick_instances(conn) -> list[dict]:
    """要跟踪的实例：正在跑的 + 登记过的（用户点开过详情页）。

    两者可能重叠，用 instance_id 去重；running 优先（src=0）。
    """
    marks = ",".join("?" * len(ACTIVE_STATUSES))
    rows = conn.execute(
        "SELECT * FROM ( "
        "SELECT ti.id AS instance_id, ti.config_path AS config_path, "
        "       ti.status AS status, sr.status AS req_status, 0 AS src "
        "  FROM task_instances ti "
        "  LEFT JOIN success_requests sr ON sr.instance_id = ti.id "
        f" WHERE ti.status IN ({marks}) "
        "UNION ALL "
        "SELECT sr.instance_id AS instance_id, ti.config_path AS config_path, "
        "       ti.status AS status, sr.status AS req_status, 1 AS src "
        "  FROM success_requests sr "
        "  JOIN task_instances ti ON ti.id = sr.instance_id "
        f" WHERE ti.status NOT IN ({marks}) "
        ") ORDER BY src ASC",
        (*ACTIVE_STATUSES, *ACTIVE_STATUSES),
    ).fetchall()

    out: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        d = dict(r)
        if d["instance_id"] in seen:
            continue
        seen.add(d["instance_id"])
        out.append(d)
    return out


def insert_events(conn, instance_id: str, events: list[tuple], source: str) -> int:
    """批量写事件，返回真正新插入的行数。

    events: [(config_name, finished_at, success, status), ...]
    UNIQUE(instance_id, config_name, finished_at) 保证幂等 —— 重复读到的
    老行会被 IGNORE 掉，而不同时刻的重跑会各自成行。
    """
    if not events:
        return 0
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO task_events "
        "  (instance_id, config_name, finished_at, success, status, source) "
        "VALUES (?,?,?,?,?,?)",
        [(instance_id, cn, ts, ok, st, source) for cn, ts, ok, st in events],
    )
    return conn.total_changes - before


def _find_logs_dir(out_dir: str) -> str | None:
    """定位 task-N.log 所在目录。

    与 find_records 同样的两种布局：<out_dir>/logs 和 <out_dir>/<stem>/logs
    （task_output_path 是否已经带了 batch 段取决于建实例时的配置）。
    实测 2425 实例的 jsonl 和 logs 都在 outputs/config/ 下，直接用 <out_dir>/logs
    会扫不到任何东西 —— 必须下钻一层。
    """
    direct = os.path.join(out_dir, "logs")
    if os.path.isdir(direct):
        return direct
    try:
        for entry in os.scandir(out_dir):
            if not entry.is_dir():
                continue
            p = os.path.join(entry.path, "logs")
            if os.path.isdir(p):
                return p
    except OSError:
        pass
    return None


def scan_task_logs(out_dir: str) -> list[tuple]:
    """扫 task-N.log，取每个任务的**真实完成时刻**。

    返回 [(config_name, finished_at, success, status), ...]。

    一个日志文件里可能有多条状态行（任务重试），**每条都记** —— 这正是
    「如实记录」的含义：那是真实发生过的两次完成。
    解析不到行首时间戳的跳过（没有时间就没法放进时序）。
    """
    logs_dir = _find_logs_dir(out_dir)
    if not logs_dir:
        return []
    events: list[tuple] = []
    try:
        entries = [e for e in os.scandir(logs_dir)
                   if e.name.startswith("task-") and e.name.endswith(".log")]
    except OSError:
        return []
    for entry in entries:
        try:
            with open(entry.path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = _STATUS_RE.search(line)
                    if not m:
                        continue
                    mt = _TS_RE.match(line)
                    mc = _CONFIG_RE.search(line)
                    if not mt or not mc:
                        continue
                    status = m.group("status")
                    events.append((
                        os.path.basename(mc.group("config")),
                        mt.group(1),
                        1 if status == "任务成功" else 0,
                        status,
                    ))
        except OSError:
            continue
    return events


def collect_incremental(conn, instance_id: str, out_dir: str) -> list[tuple]:
    """增量读两个 jsonl，返回本轮新读到的事件。

    jsonl 里只有 config 文件名、没有时间，所以时间取**观测时刻**（现在）。
    同一条 config 同时出现在两份文件里时成功优先 —— 与 hive 的
    `_rewrite_failed_record`「累计失败 ∪ 本轮失败 − 本轮成功」口径一致。
    """
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    done: list[str] = []
    failed: list[str] = []
    for rec in RECORDS:
        path = find_records(out_dir, rec)
        if not path:
            continue
        try:
            got = read_new_lines(conn, instance_id, path, state_table=STATE_TABLE)
        except Exception as e:
            print(f"  [warn] {instance_id} 读 {rec} 失败: {e}", flush=True)
            continue
        (done if rec == "complete.jsonl" else failed).extend(got)

    done_uniq = list(dict.fromkeys(os.path.basename(n) for n in done))
    done_set = set(done_uniq)
    failed_uniq = [n for n in dict.fromkeys(os.path.basename(x) for x in failed)
                   if n not in done_set]
    return ([(cn, now, 1, "任务成功") for cn in done_uniq]
            + [(cn, now, 0, None) for cn in failed_uniq])


def run_once() -> dict:
    """跑一轮：首次见到的实例回填历史，其余做增量。"""
    stats = {"instances": 0, "backfilled": 0, "new": 0}
    with get_connection() as conn:
        for inst in pick_instances(conn):
            iid = inst["instance_id"]
            out_dir = resolve_output_dir(inst["config_path"])
            if not out_dir:
                continue
            stats["instances"] += 1
            try:
                # 首次跟踪：扫 task 日志回填历史（拿到真实完成时刻），之后只做增量。
                #
                # 判据是「这个实例还没有任何 log 来源的事件」，不能用 offset 记录 ——
                # 升级场景下 offset 表是上一版遗留的、早就有行了，用它判断会让所有
                # 既有实例永远跳过回填。按 source='log' 判断则与数据本身绑定：
                # 回填过就有 log 行，没回填过就没有，和什么时候升级无关。
                has_log = conn.execute(
                    "SELECT 1 FROM task_events WHERE instance_id=? AND source='log' "
                    "LIMIT 1", (iid,)).fetchone() is not None
                first = not has_log
                if first:
                    n = insert_events(conn, iid, scan_task_logs(out_dir), "log")
                    if n:
                        stats["backfilled"] += n
                        print(f"  [backfill] {iid[:24]} 回填 {n} 条历史事件", flush=True)
                    else:
                        # 一条都没扫到（日志目录还没建 / 已被清理）：别每轮都重扫。
                        # 写一条哨兵事件占位，下一轮 has_log 就为真。
                        # config_name 用 '' 且 success=-1，聚合时会被过滤掉。
                        conn.execute(
                            "INSERT OR IGNORE INTO task_events "
                            "  (instance_id, config_name, finished_at, success, "
                            "   status, source) "
                            "VALUES (?, '', datetime('now','localtime'), -1, "
                            "        '无日志', 'log')",
                            (iid,))

                # 增量读 jsonl。首轮回填过的记录这里会再读一遍（offset 从 0 开始），
                # 但它们的时间戳是「现在」而非日志里的真实时刻，会与回填行重复计数。
                # 所以首轮只推进 offset、丢弃结果，把时间准的那份留下。
                events = collect_incremental(conn, iid, out_dir)
                if not first:
                    stats["new"] += insert_events(conn, iid, events, "jsonl")

                conn.execute(
                    "INSERT INTO success_requests (instance_id, status, updated_at) "
                    "VALUES (?, 'tracking', datetime('now','localtime')) "
                    "ON CONFLICT(instance_id) DO UPDATE SET "
                    "  status='tracking', updated_at=datetime('now','localtime')",
                    (iid,))
            except Exception as e:
                print(f"  [warn] {iid} 处理失败: {e}", flush=True)
                continue
        conn.commit()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(
        description="success_tracker：采集任务完成事件，供详情页成功率趋势图用")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help=f"轮询间隔秒（默认 {DEFAULT_INTERVAL}）")
    ap.add_argument("--once", action="store_true", help="跑一轮即退出")
    a = ap.parse_args()

    print(f"[success-tracker] 启动: interval={a.interval}s", flush=True)
    write_worker_started(WORKER_NAME, os.getpid(), f"started interval={a.interval}s")
    while True:
        t0 = time.time()
        st: dict = {}
        try:
            st = run_once()
            if st["new"] or st["backfilled"]:
                print(f"[success-tracker] 实例={st['instances']} "
                      f"新事件={st['new']} 回填={st['backfilled']} "
                      f"{time.time()-t0:.2f}s", flush=True)
            write_heartbeat(
                WORKER_NAME, os.getpid(), t0, time.time(),
                st.get("new", 0) + st.get("backfilled", 0), 0,
                f"实例={st.get('instances',0)} 新事件={st.get('new',0)} "
                f"回填={st.get('backfilled',0)}",
            )
        except KeyboardInterrupt:
            print("[success-tracker] 收到中断，退出", flush=True)
            return
        except Exception as e:
            print(f"[success-tracker] 本轮异常: {e}", flush=True)
            write_heartbeat(WORKER_NAME, os.getpid(), t0, time.time(), 0, 1,
                            f"本轮异常: {str(e)[:150]}")
        if a.once:
            break
        time.sleep(max(1, a.interval))


if __name__ == "__main__":
    main()
