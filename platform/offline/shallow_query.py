# -*- coding: utf-8 -*-
"""shallow_query：把任务描述（config json 的 queries[].text）回填进
`task_records.query_text`，供「会话列表」的内容检索用。

## 为什么要这个

任务配置是 `task_input_path` 下**每任务一个**的 json，不在库里。
原来「搜任务内容」是在请求里现场 grep 这些文件，三个问题：

  · 首次搜某实例要 0.8s（32 并发读 1 万个文件），之后靠进程内缓存
  · 20MB/实例 占在 uvicorn 进程内存里，而 uvicorn 有 4 个 worker、各一份
  · 命中几千条时要把 config_name 拼成几批 `IN (...)`，没法和其它过滤、
    排序、分页一起下沉到 SQL —— 这是架构性的别扭

落库后就是一个普通 `LIKE` 条件，毫秒级，排序分页都顺带解决。

## 存完整，不截断

实测平均 prompt **129 字**。截断到 100 字看着能省存储，实际：

    关键词          全文命中   仅前100字   漏掉
    文档             1,815       837      54%
    保存到工作区      5,824     1,965      66%
    Word            1,220       451      63%

关键信息（「保存到工作区」「整理成 Word 文档」）常在句尾，截断会让搜索变成
「有时能搜到」—— 比没有更糟。而存储只省 23%（4.2GB → 3.3GB），不划算。

## 真正的省法：按实例 LRU，只留最近 20 个

搜索是交互行为，只发生在用户正在看的实例上。全库 1092 万行里，
running/preparing + 点击过的只占 **3%（28 万行）**，存完整 text 仅 0.11GB。
超出 LRU_KEEP 个实例就把最老的 query_text 置 NULL（行还在，只是不占空间），
下次搜到它再回填。

## 触发方式

  · **同步**（api/routers/batch_output.py 的内容检索）：该实例没回填过就当场
    回填再查，0.8s 转个圈。不能纯异步 —— 用户点下搜索的瞬间库里还没数据，
    会看到「0 条」然后过几秒才出来，像搜错了。
  · **常驻**（本进程）：给 running 实例的新增任务持续回填，这样用户搜的时候
    通常已经是热的；顺带做 LRU 清理。

用法：
    python offline/shallow_query.py                 # 常驻，30s 一轮
    python offline/shallow_query.py --once          # 跑一轮即退出
    python offline/shallow_query.py --instance <id> # 只回填指定实例（调试）
    python offline/shallow_query.py --keep 20       # LRU 保留的实例数
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.core.database import get_connection, ensure_traj_extra_columns  # noqa: E402
from offline.heartbeat import write_heartbeat, write_worker_started  # noqa: E402
from offline.shallow_local import _task_input_path  # noqa: E402

WORKER_NAME = "shallow-query"
DEFAULT_INTERVAL = 30
# LRU：只保留最近这么多个实例的 query_text。超出的置 NULL（行保留）。
DEFAULT_KEEP = int(os.environ.get("SHALLOW_QUERY_KEEP", "20"))
# 读文件的并发。json.load 是 CPU 活但很轻，瓶颈在 NFS 往返 —— 实测 32 并发
# 读 1 万个文件 0.8s，再高收益有限（和 shallow_obs 的 OBS 配额不同，本地盘没限）。
READ_CONCURRENCY = int(os.environ.get("SHALLOW_QUERY_CONCURRENCY", "32"))


def extract_query_text(cfg: dict) -> str:
    """从任务配置里取「任务描述」。

    主字段是 `queries[0].text` —— 实测它就是下发给 agent 的那句话
    （如「帮我搜索一下昨天关于天津高考志愿填报的相关资讯…」）。

    多轮任务把所有 text 拼起来（有些任务 queries 有多条）。
    `queries` 是字符串列表的旧格式也认。结构不符预期返回空串 ——
    **不回退到整个 json**：那样搜「画」会被 `MAP_Linux.json` 之类的路径串误命中。
    """
    qs = cfg.get("queries")
    if not isinstance(qs, list):
        return ""
    parts = []
    for q in qs:
        if isinstance(q, dict):
            t = q.get("text")
            if isinstance(t, str) and t:
                parts.append(t)
        elif isinstance(q, str) and q:
            parts.append(q)
    return "\n".join(parts)


def backfill_instance(instance_id: str, only_missing: bool = True) -> int:
    """回填一个实例的 query_text，返回写入行数。

    only_missing=True 时只补 query_text IS NULL 的行 —— running 实例每轮都会
    调一次，不能每次把几千行全重写（白耗写锁）。
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, config_path FROM task_instances WHERE id=?",
            (instance_id,)).fetchone()
        if not row:
            return 0
        sql = ("SELECT config_name FROM task_records WHERE instance_id=?"
               + (" AND query_text IS NULL" if only_missing else ""))
        names = [r["config_name"] for r in conn.execute(sql, (instance_id,))]
    if not names:
        return 0

    d = _task_input_path({"config_path": row["config_path"]})
    if not d:
        return 0

    def _read(cn: str) -> tuple:
        p = os.path.join(d, cn)
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                return cn, extract_query_text(json.load(f))
        except (OSError, json.JSONDecodeError, ValueError):
            # 文件缺失/损坏：写空串而不是留 NULL，否则每轮都会重试这些行
            return cn, ""

    with ThreadPoolExecutor(max_workers=READ_CONCURRENCY) as pool:
        pairs = list(pool.map(_read, names))

    # 分批写：一条 executemany 几千行会长时间独占写锁（WAL 只允许一个写者，
    # 线上为此报过满屏 database is locked）。
    written = 0
    for i in range(0, len(pairs), 2000):
        chunk = pairs[i:i + 2000]
        with get_connection() as conn:
            conn.executemany(
                "UPDATE task_records SET query_text=? "
                "WHERE instance_id=? AND config_name=?",
                [(txt, instance_id, cn) for cn, txt in chunk])
            conn.commit()
        written += len(chunk)
    return written


def touch_instance(conn, instance_id: str) -> None:
    """标记该实例「刚被搜过」，用于 LRU 排序。

    借用 shallow_query_lru 表而不是在 task_instances 加列：这是纯缓存元数据，
    不该混进实例本体（也避免 ALTER 一张被在线路径高频读写的表）。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS shallow_query_lru ("
        "  instance_id TEXT PRIMARY KEY, touched_at REAL NOT NULL)")
    conn.execute(
        "INSERT INTO shallow_query_lru (instance_id, touched_at) VALUES (?,?) "
        "ON CONFLICT(instance_id) DO UPDATE SET touched_at=excluded.touched_at",
        (instance_id, time.time()))


def evict_lru(keep: int) -> int:
    """把超出 LRU 的实例的 query_text 置 NULL，返回清理的实例数。

    只清 query_text 这一列（行保留）—— 它是可重建的缓存，下次搜到再回填。
    """
    with get_connection() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS shallow_query_lru ("
            "  instance_id TEXT PRIMARY KEY, touched_at REAL NOT NULL)")
        old = [r["instance_id"] for r in conn.execute(
            "SELECT instance_id FROM shallow_query_lru "
            " ORDER BY touched_at DESC LIMIT -1 OFFSET ?", (keep,))]
        if not old:
            return 0
        for iid in old:
            conn.execute(
                "UPDATE task_records SET query_text=NULL "
                " WHERE instance_id=? AND query_text IS NOT NULL", (iid,))
            conn.execute("DELETE FROM shallow_query_lru WHERE instance_id=?", (iid,))
        conn.commit()
    return len(old)


def run_once(keep: int, instance_id: str | None = None) -> dict:
    """给 running/preparing 实例补新增任务的 query_text，然后做 LRU 清理。

    只跟 running —— 已结束的实例不会再有新任务，等用户搜到它时由
    batch_output 的同步回填处理（而且那条路径会 touch LRU）。
    """
    stats = {"instances": 0, "written": 0, "evicted": 0}
    if instance_id:
        targets = [instance_id]
    else:
        with get_connection() as conn:
            # 只处理「已在 LRU 里（= 用户搜过）且仍在跑」的实例：
            # 没搜过的实例不必预热，否则 running 实例一多就白跑一堆 IO。
            targets = [r["instance_id"] for r in conn.execute(
                "SELECT l.instance_id FROM shallow_query_lru l "
                "  JOIN task_instances ti ON ti.id = l.instance_id "
                " WHERE ti.status IN ('running','preparing') "
                " ORDER BY l.touched_at DESC")]
    for iid in targets:
        n = backfill_instance(iid, only_missing=True)
        if n:
            stats["instances"] += 1
            stats["written"] += n
    stats["evicted"] = evict_lru(keep)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(
        description="任务描述回填（供会话列表的内容检索）")
    ap.add_argument("--once", action="store_true", help="跑一轮即退出")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help=f"轮询间隔秒（默认 {DEFAULT_INTERVAL}）")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                    help=f"LRU 保留的实例数（默认 {DEFAULT_KEEP}）")
    ap.add_argument("--instance", default=None, help="只回填指定实例（调试）")
    a = ap.parse_args()

    print(f"[query] 启动: interval={a.interval}s keep={a.keep} "
          f"concurrency={READ_CONCURRENCY}", flush=True)
    ensure_traj_extra_columns()
    write_worker_started(WORKER_NAME, os.getpid(), f"started keep={a.keep}")

    if a.instance:
        n = backfill_instance(a.instance, only_missing=False)
        print(f"[query] {a.instance}: 回填 {n:,} 行", flush=True)
        return

    while True:
        t0 = time.time()
        st: dict = {}
        try:
            st = run_once(a.keep)
            if st.get("written") or st.get("evicted"):
                print(f"[query] 实例={st['instances']} 回填={st['written']:,} "
                      f"LRU清理={st['evicted']} {time.time()-t0:.2f}s", flush=True)
            write_heartbeat(WORKER_NAME, os.getpid(), t0, time.time(),
                            st.get("written", 0), 0,
                            f"实例={st.get('instances',0)} 清理={st.get('evicted',0)}")
        except KeyboardInterrupt:
            print("[query] 收到中断，退出", flush=True)
            return
        except Exception as e:
            print(f"[query] 本轮异常: {e}", flush=True)
            write_heartbeat(WORKER_NAME, os.getpid(), t0, time.time(), 0, 1,
                            f"本轮异常: {str(e)[:150]}")
        if a.once:
            break
        time.sleep(max(5, a.interval))


if __name__ == "__main__":
    main()
