# -*- coding: utf-8 -*-
"""proxy_tracker：代理用量采集的「发现与投递」进程。

## 职责

只盯**开启了本地代理**的实例（创建参数 `create_params.proxy_enable = 1`），
每 10 秒一轮，把这些实例里**新完成的 task** 投进 `task_proxy_queue`，供
`proxy_worker --lane realtime` / `--lane auto` 领取。它不下载、不解析、不碰 OBS。

没有开启代理的实例连 `complete.jsonl` 都不会被打开 —— 这是本进程存在的前提：
上一版 proxy_worker 是「扫全部实例的本地缓存」，7167 个 traj 里只有 70 个有数据，
其余全是白扫。

## 为什么增量读 complete.jsonl

task 完成的那一刻，hive 会往本地 `complete.jsonl` 追加一行（失败的在
`failed.jsonl`）。这与 `task_tracker` 发现浅层任务用的是同一个信号源，所以直接
复用它的 `read_new_lines`：该函数已经处理好三种坑
  · inode 变了      —— 文件删除重建
  · 文件头指纹变了  —— 原地 truncate 重写（failed.jsonl 每轮结束都会重写）
  · 最后一行写了一半 —— 保留在 offset 里下轮再读，避免投出半截 config 名
水位记在 `tracker_state`（instance_id, file_path）→ inode/offset/head_hash，
与 task_tracker 共用同一张表但路径不同（不同实例目录下的不同文件），不会互相干扰。

## 「及时」的真实含义

`logs_proxy` 只有在 **task 结束**时才由 hive 的 `_upload_traj_to_obs` 传进 OBS
（该调用在 `finally` 里，且 `run()` 是按 task 调用的）。所以运行中的 task 无论
本地还是 OBS 都没有代理日志 —— 本进程能做到的是「task 一结束就立刻发现」，
延迟约等于「task 结束 → 下一轮 tracker（≤10s）」。
跑到一半就能看计数只能靠 attach 沙箱执行命令，不在本方案范围内。

## 车道

| 实例状态 | priority | 由谁领取 |
|---|---|---|
| running / preparing | 90 | proxy_worker --lane realtime（10s 轮询） |
| finished / stopped / completed | 0 | proxy_worker --lane auto（60s 轮询） |

沿用 `task_tracker` 的 LANE_* 常量，不另立一套数字。

用法：
    python offline/proxy_tracker.py              # 常驻，10s 一轮
    python offline/proxy_tracker.py --once       # 跑一轮即退出
    python offline/proxy_tracker.py --interval 5
    python offline/proxy_tracker.py --once --instance <id>   # 只处理指定实例
"""
from __future__ import annotations

import argparse
import os
import sys
import time

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.core.database import get_connection, ensure_proxy_tables   # noqa: E402
from offline.heartbeat import write_heartbeat, write_worker_started  # noqa: E402
from offline.task_tracker import (                                 # noqa: E402
    ACTIVE_STATUSES, LANE_TRACK, LANE_AUTO, _short_iid,
    read_new_lines, resolve_output_dir, find_records)

WORKER_NAME = "proxy-tracker"
DEFAULT_INTERVAL = 10

# 本进程专用的水位表。
#
# **必须与 task_tracker 的 tracker_state 分开**：水位键是
# (instance_id, file_path)，不区分读取者，而浅层 tracker 与 proxy_tracker 读的是
# 同一份 complete.jsonl / failed.jsonl。共表时先跑的那个把 offset 推到头，后跑的
# 整段读不到 —— 实测 proxy_tracker 首跑只捞到 4 行，而实际有上千行。
PROXY_STATE_TABLE = "proxy_tracker_state"

# 会产出 complete.jsonl / failed.jsonl 的实例状态。created/paused 还没跑，
# 打开它们只会得到「文件不存在」，省掉这次 syscall。
INSTANCE_STATUSES = ACTIVE_STATUSES + ("finished", "stopped", "completed")

# 采集的记录文件。两份都要读：失败任务的代理用量恰恰最有诊断价值，
# 而 hive 的 _upload_traj_to_obs 在 finally 里照跑，失败 task 同样有 logs_proxy。
RECORD_FILES = ("complete.jsonl", "failed.jsonl")


def _lane_priority(inst_status: str | None) -> int:
    """实例状态 → 车道号。运行中的走实时车道，已结束的走背景车道。"""
    return LANE_TRACK if inst_status in ACTIVE_STATUSES else LANE_AUTO


def _proxy_instances(conn, instance_id: str | None = None) -> list[dict]:
    """`proxy_enable=1` 且可能产出任务的实例。

    json_extract 对缺字段/非法 JSON 返回 NULL，不会抛错 —— 只有显式写了
    proxy_enable:true 的实例才会命中。
    """
    sql = ("SELECT id, name, status, config_path FROM task_instances "
           "WHERE json_extract(create_params, '$.proxy_enable') = 1")
    params: tuple = ()
    if instance_id:
        sql += " AND id = ?"
        params = (instance_id,)
    else:
        ph = ",".join("?" * len(INSTANCE_STATUSES))
        sql += f" AND status IN ({ph})"
        params = INSTANCE_STATUSES
    sql += " ORDER BY COALESCE(stopped_at, started_at, created_at) DESC"
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    except Exception as e:
        print(f"[{WORKER_NAME}] 查询 proxy 实例失败: {str(e)[:150]}", flush=True)
        return []


def submit_batch(conn, instance_id: str, traj_names: list[str],
                 priority: int) -> int:
    """把 traj 投进 task_proxy_queue，返回新增/更新的行数。

    冲突时**只升不降** priority：已结束实例的行本来是背景车道，如果它被重新
    点开变成 running，下一轮投递能把它提到实时车道；反过来 running 变 finished
    时不该把在途的实时行降级（那些马上就会被领取）。

    已是 done 的行不重置 —— 与 shallow_queue 同样的理由：重置会让同一批 traj
    被反复重采。要重采走 --backfill。
    """
    if not traj_names:
        return 0
    before = conn.total_changes
    conn.executemany(
        "INSERT INTO task_proxy_queue "
        "(instance_id, traj_name, config_name, priority, state) "
        "VALUES (?,?,?,?,'queued') "
        "ON CONFLICT(instance_id, traj_name) DO UPDATE SET "
        "  priority=MAX(task_proxy_queue.priority, excluded.priority), "
        "  config_name=excluded.config_name, "
        # 只有「从未成功过」的行才重新排队：done 的保持 done，
        # claimed 的保持 claimed（worker 正在处理，别打断它）。
        "  state=CASE WHEN task_proxy_queue.state='skipped' "
        "             THEN 'queued' ELSE task_proxy_queue.state END",
        [(instance_id, t, t + ".json", priority) for t in traj_names],
    )
    return conn.total_changes - before


def _traj_of(line: str) -> str | None:
    """complete.jsonl 的一行 → 叶子目录名。

    行内容是 config 文件名（如 `1116_frontend_patrick_bike__long_q1.json`），
    去掉 .json 就是 OBS task 目录名。与 output_worker._leaf_of 同口径。
    """
    name = line.strip().strip("/")
    if not name:
        return None
    return name[:-5] if name.endswith(".json") else name


def run_once(instance_id: str | None = None) -> dict:
    """扫一轮全部 proxy 实例，返回本轮统计。"""
    ensure_proxy_tables()
    stats = {"instances": 0, "lines": 0, "submitted": 0, "skipped_no_dir": 0,
             "details": []}
    with get_connection() as conn:
        insts = _proxy_instances(conn, instance_id)
        stats["instances"] = len(insts)
        for inst in insts:
            iid = inst["id"]
            out_dir = resolve_output_dir(inst.get("config_path") or "")
            if not out_dir:
                stats["skipped_no_dir"] += 1
                continue
            priority = _lane_priority(inst.get("status"))
            new_by_traj: list[str] = []
            n_lines = 0
            for record in RECORD_FILES:
                path = find_records(out_dir, record)
                if not path:
                    continue
                try:
                    lines = read_new_lines(conn, iid, path, PROXY_STATE_TABLE)
                except Exception as e:
                    print(f"[{WORKER_NAME}] 读 {record} 失败 {_short_iid(iid)}: "
                          f"{str(e)[:120]}", flush=True)
                    continue
                n_lines += len(lines)
                for ln in lines:
                    traj = _traj_of(ln)
                    if traj and traj not in new_by_traj:
                        new_by_traj.append(traj)
            if not new_by_traj:
                continue
            n = submit_batch(conn, iid, new_by_traj, priority)
            # 与 read_new_lines 的 offset 更新放进同一个事务：中途失败时
            # offset 不前进，下一轮会重读这些行 —— 靠 UNIQUE 去重，不会重复采集。
            conn.commit()
            stats["lines"] += n_lines
            stats["submitted"] += len(new_by_traj)
            if new_by_traj:
                stats["details"].append(
                    f"{_short_iid(iid)} +{len(new_by_traj)} lane={priority}")
        conn.commit()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="代理用量采集的发现与投递")
    ap.add_argument("--once", action="store_true", help="跑一轮即退出（调试）")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help="常驻轮询间隔（秒）")
    ap.add_argument("--instance", default=None, help="只处理指定实例（调试）")
    a = ap.parse_args()

    ensure_proxy_tables()
    write_worker_started(WORKER_NAME, os.getpid(), f"started interval={a.interval}")

    while True:
        t0 = time.time()
        st: dict = {}
        try:
            st = run_once(a.instance)
            print(f"[{WORKER_NAME}] 实例={st['instances']} 新增行={st['lines']} "
                  f"投递={st['submitted']} 无输出目录={st['skipped_no_dir']} "
                  f"{time.time() - t0:.2f}s", flush=True)
            for d in st.get("details", [])[:10]:
                print(f"    [proxy-tracker] {d}", flush=True)
            write_heartbeat(WORKER_NAME, os.getpid(), t0, time.time(),
                            st.get("submitted", 0), 0,
                            f"新行={st.get('lines', 0)}")
        except KeyboardInterrupt:
            print(f"[{WORKER_NAME}] 收到中断，退出", flush=True)
            return
        except Exception as e:
            print(f"[{WORKER_NAME}] 本轮异常: {e}", flush=True)
            write_heartbeat(WORKER_NAME, os.getpid(), t0, time.time(), 0, 1,
                            f"本轮异常: {str(e)[:150]}")
        if a.once:
            break
        time.sleep(max(1, a.interval))


if __name__ == "__main__":
    main()
