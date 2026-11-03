# -*- coding: utf-8 -*-
"""auto_tracker：历史实例的「发现与投递」进程（背景车道专用）。

## 为什么需要它

`task_tracker` 是**纯点击驱动**：它只扫 `shallow_requests` 里登记过的实例
（用户点过「输出」的），没点过的一行都不投。这在交互上是对的，但留下一个结构性缺口：

实测全库 **2126 个已结束实例,只有 16 个被跟踪**,其余 2110 个 tracker 根本不扫。
其中 243 个实例共 **301 万行** `traj_level IS NULL` —— 永远等不到分析,
而 shallow-auto 的 64 并发全程闲着。

本进程补这个缺口：主动找「已结束 + 仍有未定级行」的历史实例,按背景车道
（`priority=0`）投进 `shallow_queue`,交给 shallow-auto 消化。

## 每轮只投一个实例

刻意的。历史缺口 301 万行,一次全投进去会：
  · 把 `shallow_queue` 瞬间灌成几百万行（表已有 100 万行,写放大明显）
  · 让单次 UPDATE/INSERT 持锁过久 —— 实测一条 876 万行的 UPDATE 持锁 22.8s,
    期间用户点下载全部 500（见 doc/README_worker.md 的写锁注意事项）
  · 新点击的实例被挤在几百万条历史后面

每轮一个实例、投完就等下一轮,shallow-auto 消化完再投下一个。这样历史缺口
以「实例」为粒度稳步推进,且任何时刻队列里的背景行都是有界的。

## 与 task_tracker 的分工

|            | task_tracker          | auto_tracker(本文件)        |
|------------|-----------------------|-----------------------------|
| 实例来源   | `shallow_requests`(点击) | 全库扫「已结束+有缺口」      |
| 车道       | 按实例状态(100/0)      | 恒为 0(背景)                 |
| 粒度       | 每轮扫全部被跟踪实例    | **每轮一个实例**             |
| 增量       | offset + inode + 指纹   | 同款(复用 tracker 的函数)    |
| 间隔       | 5s                    | 60s(历史活不急)              |

两者写同一张 `shallow_queue`,靠 `ON CONFLICT(instance_id, config_name)` 去重,
同时跑是安全的：某实例既被点过又被本进程选中时,`priority=MAX()` 保证它留在
实时车道（用户在等,优先级更高）。

用法：
    python offline/auto_tracker.py                  # 常驻,60s 一轮
    python offline/auto_tracker.py --once           # 跑一轮即退出(调试)
    python offline/auto_tracker.py --interval 120
    python offline/auto_tracker.py --min-gap 100    # 只补缺口 >= 100 行的实例
"""
from __future__ import annotations

import argparse
import os
import sys
import time

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.core.database import get_connection, ensure_shallow_extra_columns  # noqa: E402
from offline.heartbeat import write_heartbeat, write_worker_started  # noqa: E402
from offline.task_tracker import (  # noqa: E402
    LANE_AUTO, LANE_REALTIME, _short_iid, _submit_incremental, resolve_output_dir)

WORKER_NAME = "auto-tracker"
DEFAULT_INTERVAL = 60
# 背景车道水位上限：auto 车道待办超过这个行数就**不再投新实例**，等消化。
#
# 没有水位控制时本进程是「60s 无条件投一个实例」，而历史实例缺口差异极大
# （实测最大 61,808 行、最小 3 行）—— 连投 5 个大实例就是 20 万行涌入，
# 把 shallow_queue 撑大、写放大、点击的实例被挤在后面。
# 有水位后：小实例连续投（消化快），大实例投完自然等几轮，队列深度自适应。
#
# 取 20000 的依据：shallow-auto 实测阶段2 约 467 行/分，2 万行约 43 分钟消化完；
# 再大就失去「队列有界」的意义，再小则大实例（6 万行）永远投不进去。
DEFAULT_MAX_PENDING = int(os.environ.get("AUTO_TRACKER_MAX_PENDING", "20000"))
# 缺口小于这个行数的实例跳过：几行的缺口通常是「任务确实没产出轨迹」的终态,
# 反复投递只会让 shallow-auto 白跑。真要补这些行,手工调低该阈值。
DEFAULT_MIN_GAP = 1
# 已结束的状态。与 task_tracker.ACTIVE_STATUSES 互补（running/preparing 由
# task_tracker 负责,本进程不碰 —— 那些实例还在产出,点击驱动的增量更合适）。
ENDED_STATUSES = ("finished", "stopped", "completed")


def auto_lane_pending(conn) -> int:
    """背景车道当前待办行数（两阶段任一维还在排队）。

    阶段 2 必须带 EXISTS 子查询，与 shallow_obs.claim 的条件完全一致 ——
    线上有大量 `obs_state='queued'` 但 `traj_level` 早已写好的遗留行，
    worker 根本不会领，算进水位会让本进程误以为队列很满、永远不投新实例。
    """
    return conn.execute(
        "SELECT COUNT(*) FROM shallow_queue sq "
        " WHERE sq.priority < ? AND ("
        "   sq.local_state='queued' OR ("
        "     sq.obs_state='queued' AND EXISTS("
        "       SELECT 1 FROM task_records tr "
        "        WHERE tr.instance_id=sq.instance_id "
        "          AND tr.config_name=sq.config_name "
        "          AND tr.traj_level IS NULL AND tr.task_done IS NOT NULL)))",
        (LANE_REALTIME,),
    ).fetchone()[0]


def pick_one_instance(conn, min_gap: int) -> dict | None:
    """挑一个「已结束 + 仍有未定级行 + 本进程还没投过」的实例。返回 None 表示没活了。

    排序：按最近活动时间倒序（最新先补）—— 与 shallow-auto 的 `ORDER BY id DESC`
    口径一致。效果是**刚跑完的实例自动插队到历史积压前面**：
    一个 running 实例结束后立刻成为「最近活动」的那个,下一轮就被选中,
    而 7/8 月的 200 多个老实例继续往后排。这是刻意的 —— 用户刚跑完的批次
    最可能马上要看结果,而几个月前的历史晚几小时无所谓。

    注意这里**不按缺口大小排序**。按缺口降序会让 6 万行的大实例一直占前面、
    小实例永远轮不到;按升序又会让大实例饿死。按时间排 + 水位控制的组合更自然:
    大实例投完水位满、自然等几轮消化,期间新结束的实例照常插队。

    **排除已在 shallow_requests 里的实例**：那些由 task_tracker 负责,
    本进程再投一遍是重复劳动（ON CONFLICT 会去重,但白跑一次 jsonl 全文件读）。

    `auto_tracker_state` 记录投过的实例,避免每轮重复选中同一个 ——
    投递完成后该实例的缺口要等 shallow-auto 慢慢消化,期间 traj_level 仍是 NULL,
    不记状态的话下一轮又会选到它、又整扫一遍 jsonl。
    """
    # 先确保状态表存在：下面的 NOT EXISTS 会查它，表不存在时整条 SQL 报错。
    # 调用方 run_once 已经建过，这里再兜一次（本函数也可能被单独调用/测试）。
    _ensure_state_table(conn)
    marks = ",".join("?" * len(ENDED_STATUSES))
    row = conn.execute(
        f"""SELECT ti.id, ti.config_path, ti.status,
                   (SELECT COUNT(*) FROM task_records tr
                     WHERE tr.instance_id = ti.id AND tr.traj_level IS NULL) gap
              FROM task_instances ti
             WHERE ti.status IN ({marks})
               -- 点击过的交给 task_tracker,本进程不碰
               AND NOT EXISTS(SELECT 1 FROM shallow_requests sr
                               WHERE sr.instance_id = ti.id)
               -- 本进程投过的不重复投（等 shallow-auto 消化完再说）
               AND NOT EXISTS(SELECT 1 FROM auto_tracker_state ats
                               WHERE ats.instance_id = ti.id)
               AND EXISTS(SELECT 1 FROM task_records tr
                           WHERE tr.instance_id = ti.id AND tr.traj_level IS NULL)
             ORDER BY COALESCE(ti.stopped_at, ti.created_at) DESC
             LIMIT 1""",
        ENDED_STATUSES,
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    if (d.get("gap") or 0) < min_gap:
        # 缺口太小：也记一笔,否则每轮都会重新选到它
        _mark_done(conn, d["id"], 0, f"缺口 {d.get('gap')} < {min_gap},跳过")
        return None
    return d


def _ensure_state_table(conn) -> None:
    """建 auto_tracker_state（记录本进程投过哪些实例）。

    与 tracker_state 分开：那张表记的是「文件读到哪了」(instance_id, file_path),
    本表记的是「这个实例投过了」(instance_id),语义不同。
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS auto_tracker_state (
               instance_id TEXT PRIMARY KEY,
               submitted   INTEGER NOT NULL DEFAULT 0,
               note        TEXT,
               created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
           )""")


def _mark_done(conn, instance_id: str, n_sub: int, note: str = "") -> None:
    conn.execute(
        "INSERT INTO auto_tracker_state (instance_id, submitted, note) VALUES (?,?,?) "
        "ON CONFLICT(instance_id) DO UPDATE SET submitted=excluded.submitted, "
        "  note=excluded.note, created_at=CURRENT_TIMESTAMP",
        (instance_id, n_sub, (note or "")[:200]))


def run_once(min_gap: int, max_pending: int = DEFAULT_MAX_PENDING) -> dict:
    """投一个历史实例。返回 {instance, gap, lines, submitted, pending}。

    **水位控制**：背景车道待办超过 max_pending 就本轮不投，等 shallow-auto 消化。
    这样队列深度自适应 —— 小实例连续投，大实例（实测最大 6 万行）投完自然等几轮。
    """
    stats = {"instance": None, "gap": 0, "lines": 0, "submitted": 0, "pending": 0}
    with get_connection() as conn:
        _ensure_state_table(conn)
        pending = auto_lane_pending(conn)
        stats["pending"] = pending
        if max_pending > 0 and pending >= max_pending:
            # 水位已满：不选实例、不读 jsonl，直接回去等下一轮
            conn.commit()
            return stats
        inst = pick_one_instance(conn, min_gap)
        if not inst:
            conn.commit()
            return stats

        iid = inst["id"]
        stats["instance"] = _short_iid(iid)
        stats["gap"] = inst.get("gap") or 0

        out_dir = resolve_output_dir(inst["config_path"] or "")
        if not out_dir:
            # 实例目录已被清理/搬迁：记一笔跳过,否则每轮都会重新选到它
            _mark_done(conn, iid, 0, "输出目录不存在")
            conn.commit()
            stats["submitted"] = -1
            return stats

        # 复用 task_tracker 的增量读 + 投递（offset/inode/头指纹那套都在里面）。
        # 本进程走背景车道,恒 priority=LANE_AUTO。
        n_lines, n_sub = _submit_incremental(conn, iid, out_dir, priority=LANE_AUTO)
        stats["lines"] = n_lines
        stats["submitted"] = n_sub
        _mark_done(conn, iid, n_sub, f"缺口{stats['gap']} 投递{n_sub}")
        conn.commit()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(
        description="历史实例投递（背景车道,每轮一个实例）")
    ap.add_argument("--once", action="store_true", help="跑一轮即退出")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help=f"轮询间隔秒（默认 {DEFAULT_INTERVAL}）")
    ap.add_argument("--min-gap", type=int, default=DEFAULT_MIN_GAP,
                    help=f"缺口小于此行数的实例跳过（默认 {DEFAULT_MIN_GAP}）")
    ap.add_argument("--max-pending", type=int, default=DEFAULT_MAX_PENDING,
                    help=f"背景车道水位上限，超过就不投新实例（默认 "
                         f"{DEFAULT_MAX_PENDING}，0=不限）")
    a = ap.parse_args()

    print(f"[auto-tracker] 启动: 每轮一个历史实例 interval={a.interval}s "
          f"min_gap={a.min_gap} max_pending={a.max_pending}", flush=True)
    ensure_shallow_extra_columns()
    write_worker_started(WORKER_NAME, os.getpid(),
                         f"started interval={a.interval}s")

    while True:
        t0 = time.time()
        st: dict = {}
        try:
            st = run_once(a.min_gap, a.max_pending)
            if st.get("instance") and st.get("submitted", 0) > 0:
                print(f"[auto-tracker] {st['instance']} 缺口={st['gap']:,} "
                      f"新行={st['lines']} 投递={st['submitted']} "
                      f"水位={st['pending']:,} {time.time()-t0:.2f}s", flush=True)
            elif st.get("submitted") == -1:
                print(f"[auto-tracker] {st['instance']} 输出目录不存在,跳过",
                      flush=True)
            elif a.max_pending > 0 and st.get("pending", 0) >= a.max_pending:
                # 水位满时每轮都打一行：否则进程看起来像挂了（无日志无产出）
                print(f"[auto-tracker] 水位={st['pending']:,} ≥ {a.max_pending:,}，"
                      f"等 shallow-auto 消化", flush=True)
            write_heartbeat(
                WORKER_NAME, os.getpid(), t0, time.time(),
                max(st.get("submitted", 0), 0), 0,
                f"实例={st.get('instance') or '无'} 缺口={st.get('gap', 0)} "
                f"水位={st.get('pending', 0)}")
        except KeyboardInterrupt:
            print("[auto-tracker] 收到中断,退出", flush=True)
            return
        except Exception as e:
            print(f"[auto-tracker] 本轮异常: {e}", flush=True)
            write_heartbeat(WORKER_NAME, os.getpid(), t0, time.time(), 0, 1,
                            f"本轮异常: {str(e)[:150]}")
        if a.once:
            break
        # 没活了就睡久一点（历史补全不急）；有活就按正常间隔
        time.sleep(max(1, a.interval) if st.get("instance") else max(1, a.interval) * 5)


if __name__ == "__main__":
    main()
