# -*- coding: utf-8 -*-
"""shallow-local：浅层分析的「本地 DONE 判定」进程。

## 职责

只做一件事：判断每个 config 的任务到底做完了没有，写 `task_records.task_done`。

数据源是实例本地 `<outputs>/logs/task-<idx>.log`（hive 每个任务收尾时写入）。
日志里 agent 会在回复末尾打 `【Task_Done】`，那才是「任务真的做完了」；
而 `completed_tasks` 数的是 complete.jsonl 行数，流水线没抛异常就算成功 ——
两者能差得很远（实测某实例成功 312、DONE 0）。

## 为什么独立进程

和 OBS 定级（shallow-obs）拆开：本地读 NFS 约 900 个/秒且无配额限制，
OBS 单次 0.4~2s 且有请求数上限。挤在一个线程池里快的会被慢的拖住。
拆开后这里可以开到 64 并发。

## 领取

从 shallow_queue 用 CAS 领取（照搬 output_worker._claim_and_download 的写法）：
    UPDATE ... SET state='claimed', claimed_by=? WHERE id=? AND state='queued'
rowcount==0 表示被别的进程抢走，跳过即可。多个 local 进程同时跑是安全的。

## 判据

    有 task-<idx>.log
        含【Task_Done】            → task_done=1
        有终态行但无标记           → task_done=0
        无终态行（还在写/被 kill） → 不改，释放回 queued，下轮再判
    无 task-<idx>.log              → task_done=0，state='skipped'

用法：
    python offline/shallow_local.py                 # 常驻
    python offline/shallow_local.py --once          # 跑一轮即退出
    python offline/shallow_local.py --concurrency 64
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
from offline.output_worker import _FINAL_STATUS_RE, _logs_dir  # noqa: E402
from offline.heartbeat import write_heartbeat, write_worker_started  # noqa: E402

WORKER_NAME = "shallow-local"
DEFAULT_CONCURRENCY = 64
DEFAULT_INTERVAL = 5
CLAIM_TIMEOUT_SEC = 600          # claimed 超过这个时长没回写 → 回收（worker 被 kill）
MAX_ATTEMPTS = 3                 # 同一行最多试 3 次，超过判 skipped


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def reclaim_stuck(conn) -> int:
    """回收超时 claimed 的行（阶段 1 维）。worker 被 kill 时状态会永远停在 claimed。

    超过 MAX_ATTEMPTS 的直接判 skipped，避免「回收→重排→再超时」死循环。
    只处理 local_state 这一维（本进程的职责范围）。

    **先 SELECT 探测、命中 0 行就直接返回**，不开写事务。
    SQLite 的 WAL 只允许一个写者：无条件 UPDATE 会让「本来没活干」的 worker
    每轮也抢一次写锁。线上实测 4 个 worker（2s~5s 一轮）+ 4 个 uvicorn 的
    queue_scheduler 同时写，9.9GB 的库触发 WAL checkpoint 时独占写锁远超
    busy_timeout(15s)，于是到处报 `database is locked` —— 而 shallow-auto
    当时的待办是 0 行，纯粹是来抢锁的。
    """
    now = time.time()
    cutoff = now - CLAIM_TIMEOUT_SEC
    # 只读探测：走 idx_shallow_queue_state(state, claimed_at)，命中 0 行时 0.08s
    hit = conn.execute(
        "SELECT 1 FROM shallow_queue "
        "WHERE local_state='claimed' AND claimed_at < ? LIMIT 1",
        (cutoff,),
    ).fetchone()
    if not hit:
        return 0

    cur = conn.execute(
        "UPDATE shallow_queue SET local_state='skipped', note='claimed 超时且已达最大尝试次数' "
        "WHERE local_state='claimed' AND claimed_at < ? AND attempts >= ?",
        (cutoff, MAX_ATTEMPTS),
    )
    n_dead = cur.rowcount
    cur = conn.execute(
        "UPDATE shallow_queue SET local_state='queued', claimed_by=NULL, claimed_at=NULL "
        "WHERE local_state='claimed' AND claimed_at < ? AND attempts < ?",
        (cutoff, MAX_ATTEMPTS),
    )
    return n_dead + cur.rowcount


def claim(conn, limit: int, lane: str | None = None) -> list[dict]:
    """CAS 领取本地判定任务（阶段 1）。走 local_state 这一维，与 obs 互不干扰。

    **attempts 上限只对自动发现的行生效**（priority=0），用户点击的（priority>=100）
    不限次。原因是这两个上限的初衷不同：
      - 自动发现：判不出终态的行（日志还没写完 / 已被清理）会每次放回 queued、
        下轮又被领走，无限循环。实测 attempts 涨到 938，9488 条毒行占满 64 个
        并发槽，新任务根本领不到（每轮领 3 万条只 done 6 千）。
      - 用户点击：他就是要看这个实例的结果，卡在 3 次之后永远领不到，
        页面上表现为「点了没反应」。实测某实例 20 条 priority=100 的条目
        全是 attempts=3，被这个条件挡住，死活跑不出来。

    lane：场景车道（shallow_worker 用），决定领哪一半队列。
      'realtime' → 只领用户点过的（priority>=100），按 id 升序（同一实例内保持投递序）
      'auto'     → 只领背景行（priority<100），按 **id DESC**（最新投递优先）
      None       → 不分车道（本文件单独作为调试 CLI 跑时的旧行为）

    两条车道的 WHERE 区间互斥，CAS 不会重叠，realtime 与 auto 可同时跑而互不抢行。
    auto 用 id DESC 是刻意的：背景队列积压几十万条时，新跑完的批次应该先出结果，
    而不是排在历史行后面等几天。注意**不能再带 `priority DESC`** —— 车道已由
    WHERE 分流，再按 priority 排序只会让同优先级内的顺序失去意义。
    """
    where = ["local_state='queued'", "(priority >= 100 OR attempts < ?)"]
    params: list = [MAX_ATTEMPTS]
    order = "priority DESC, id"
    if lane == "realtime":
        where.append("priority >= 100")
        order = "id"
    elif lane == "auto":
        where.append("priority < 100")
        order = "id DESC"
    params.append(limit)
    rows = conn.execute(
        "SELECT id, instance_id, config_name, task_idx, attempts FROM shallow_queue "
        "WHERE " + " AND ".join(where) +
        f" ORDER BY {order} LIMIT ?",
        tuple(params),
    ).fetchall()
    me = worker_name()
    claimed = []
    for r in rows:
        cur = conn.execute(
            "UPDATE shallow_queue SET local_state='claimed', claimed_by=?, claimed_at=?, "
            "attempts=attempts+1, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND local_state='queued'",
            (me, time.time(), r["id"]),
        )
        if cur.rowcount == 0:
            continue                  # 被别的进程抢走
        claimed.append(dict(r))
    conn.commit()
    return claimed


def _task_input_path(inst: dict) -> str | None:
    """实例的输入配置目录（config.yaml 的 run_config.task.task_input_path）。

    hive 建任务队列时是 `enumerate(sorted(config.run_input_config_files))`，
    `config.run_input_config_files` 来自 os.scandir(task_input_path)，
    所以 **config 文件名在 sorted(目录列表) 里的下标就是 task_idx**。
    这是 task_idx 的权威来源，不依赖 task_records 有没有这行。
    """
    try:
        from omegaconf import OmegaConf
        cfg = OmegaConf.load(inst["config_path"])
        p = str(cfg.run_config.task.task_input_path or "")
        return p if p and os.path.isdir(p) else None
    except Exception:
        return None


def _task_idx_map(inst: dict) -> dict:
    """{config 文件名: task_idx}，由输入目录排序后生成。目录不可用时返回 {}。"""
    d = _task_input_path(inst)
    if not d:
        return {}
    try:
        names = sorted(f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)))
    except OSError:
        return {}
    return {name: i for i, name in enumerate(names)}


def judge_one(item: dict, inst_cache: dict) -> tuple[str, int | None, str]:
    """判一条。返回 (state, task_done, note)。

    state: done / queued(留待下轮) / skipped
    task_done: 1/0/None（None = 本轮不改）

    inst_cache 里缓存的是 (inst, logs_dir, idx_map) 三元组：
      - _logs_dir 每次调用都要 OmegaConf.load 解析 config.yaml，实测 9.1ms/次 ——
        而读一个 30KB 的日志只要 0.1ms。不缓存的话 90% 时间花在重复解析配置上。
      - idx_map 同理，只在需要回推 task_idx 时用，按实例缓存一次即可。
    """
    iid = item["instance_id"]
    cached = inst_cache.get(iid)
    if cached is None:
        with get_connection() as c:
            row = c.execute("SELECT id, config_path FROM task_instances WHERE id=?",
                            (iid,)).fetchone()
        inst = dict(row) if row else None
        # 实例不存在时也缓存 (None, None, {})，避免每条都去查一次库
        cached = (inst, _logs_dir(inst) if inst else None,
                  _task_idx_map(inst) if inst else {})
        inst_cache[iid] = cached
    inst, logs_dir, idx_map = cached
    if not inst:
        return "skipped", 0, "实例已不存在"

    idx = item.get("task_idx")
    if idx is None:
        # 回退 1：task_records 里可能已经有这行（在线同步或 worker 补的）
        with get_connection() as c:
            row = c.execute(
                "SELECT task_idx FROM task_records WHERE instance_id=? AND config_name=?",
                (iid, item["config_name"]),
            ).fetchone()
        if row:
            idx = row["task_idx"]
    if idx is None:
        # 回退 2：由输入目录排序下标还原。hive 建队列就是 enumerate(sorted(files))，
        # 所以这个映射是确定的，不依赖任何表。实测某实例 task_records 只有 2 行而
        # complete.jsonl 有 3 行，第三个 config 两边都查不到，只有这条能救。
        idx = idx_map.get(item["config_name"])
    if idx is None:
        return "skipped", 0, "task_idx 缺失，无法定位日志"

    # 把解出来的 idx 写回 item，供 apply_results 落库 ——
    # 否则「队列里 task_idx 为 NULL、靠回退 2 才解出来」的行永远不落这个值，
    # 页面「任务编号」一列只能显示 —，排查时还得手工去输入目录数下标。
    # 实测全库 23,526 行（0.22%）是这种情况。
    item["task_idx"] = idx
    path = os.path.join(logs_dir, f"task-{idx}.log")
    if not os.path.isfile(path):
        return "skipped", 0, "本地日志不存在"

    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return "queued", None, f"读日志失败: {e}"

    # 没有终态行的日志（还在写 / 被 kill）本轮不判，释放回队列。
    # 判成 0 会把「还没跑完」永久钉死，而 task_done 一旦写入就不再重扫。
    text = data.decode("utf-8", "replace")
    m = _FINAL_STATUS_RE.search(text)
    if not m:
        return "queued", None, "日志无终态行"

    # 完成标记有两代，必须都认：
    #   新版 harness → assistant 自己在回复末尾打「【Task_Done】」
    #   旧版 harness → 主脚本收尾统一打「所有任务执行完成!」
    # 只认前者时，旧版 harness 的实例 task_done 全是 0（实测某实例 100 个任务
    # 全部 task_done=0，而其中 60 个日志里明确写着「所有任务执行完成!」）。
    # 旧标记单独用不可靠（任务异常样本里也有约 6% 命中），所以必须与
    # status='任务成功' 互相印证 —— 与 oa.log_fallback_grade 的口径保持一致。
    is_success = m.group("status") == "任务成功"
    if oa.TRAJ_DONE_MARKER.encode() in data:
        done_val = 1
    elif is_success and oa.LEGACY_DONE_MARKER.encode() in data:
        done_val = 1
    else:
        done_val = 0

    # 「任务异常」的行根本没有轨迹可定级，等 obs 去拉 OBS 必然是空跑。
    # 这里直接一步到位：local_state=done 的同时把 obs_state 也置 done，
    # 由 apply_results 写 traj_level='failed'，不占 OBS 配额。
    # 实测全局 obs 待处理 8.6 万条里 7 万条是这类，清掉后队列直接少 80%。
    if not is_success:
        return "done_failed", done_val, f"status={m.group('status')}，无需 OBS 定级"

    return "done", done_val, ""


def apply_results(results: list[tuple[dict, str, int | None, str]]) -> dict:
    """把判定结果写回 task_records 与 shallow_queue。

    用 executemany 批量写，不是逐行 execute。逐行时每行两次 SQL 往返，
    实测吞吐只有 100 行/秒（远低于日志扫描的 900 行/秒）—— 瓶颈全在 DB 调用次数上。

    每批一个事务：整轮一次提交会长时间独占写锁（上次踩过：database is locked，
    WAL 涨到 265MB）。这里按调用方给的批大小提交，配合 busy_timeout。

    **双阶段状态机**：本进程只推进 local_state，不碰 obs_state。
    改前的写法把共用的 state 直接置 done，导致 shallow-obs 再也领不到这一条
    （它要求 state='queued'），DONE 判完但 traj_level 永远是 NULL。
    现在 local 完成后只把 local_state 置 done，obs 那一维保持 queued 等它来领。

    例外 "done_failed"：判定时发现任务是「任务异常/失败」，没有轨迹可定级。
    这种直接一步到位 —— local_state=done、obs_state=done 同时写 traj_level='failed'，
    不占 OBS 配额。实测全局 obs 待处理 8.6 万条里 7 万条是这类。
    """
    stat = {"done": 0, "queued": 0, "skipped": 0, "done_failed": 0}
    tr_rows, q_rows, failed_rows = [], [], []
    for item, state, done_val, note in results:
        stat[state] = stat.get(state, 0) + 1
        if state in ("done", "done_failed") and done_val is not None:
            tr_rows.append((done_val, item["instance_id"], item["config_name"]))
        if state == "done_failed":
            # 带上 task_idx 供 UPSERT 建行用（可能是 None，见下面的说明）
            failed_rows.append((item["instance_id"], item["config_name"],
                                item.get("task_idx")))
            q_rows.append(("done", (note or "")[:200] or None, item["id"]))
        else:
            q_rows.append((state, (note or "")[:200] or None, item["id"]))

    with get_connection() as conn:
        if tr_rows:
            # 不再加 `AND task_done IS NULL`：本地日志是 DONE 的权威来源，
            # 每次判定结果都覆盖写入。加这个条件时，一旦某轮判成 0 就永久钉死,
            # 之后修了判定逻辑（比如补上旧版「所有任务执行完成!」标记）也刷不回来
            # —— 实测重置 3 万条重判，一条都没更新。
            # 只对「本轮真的判过」的行写（tr_rows 里只有 state=done/done_failed），
            # 仍在写日志的行不会进 tr_rows。
            conn.executemany(
                "UPDATE task_records SET task_done=? "
                "WHERE instance_id=? AND config_name=?",
                tr_rows,
            )
            # 顺手补 task_idx：judge_one 已经按三级回退解出来了（见它末尾写回 item），
            # 不补的话页面「任务编号」一列对这些行只能显示 —。
            # 只在原值为 NULL 时写，不覆盖在线同步写入的权威值。
            idx_rows = [(it.get("task_idx"), it["instance_id"], it["config_name"])
                        for it, st, _, _ in results
                        if st in ("done", "done_failed") and it.get("task_idx") is not None]
            if idx_rows:
                conn.executemany(
                    "UPDATE task_records SET task_idx=? "
                    "WHERE instance_id=? AND config_name=? AND task_idx IS NULL",
                    idx_rows,
                )
            # 同步到 task_traj_records —— 两表实际上是 1:1（实测 traj_name 与
            # config stem 不一致的行为 0），但历史上 task_done 各写一份：
            #   task_records     ← 新架构 shallow-local 扫本地日志（权威）
            #   task_traj_records ← 旧路径从 OBS tsr / 慢路径
            # 不同步的话，页面读哪张表就看到哪一半数据（全库 119 万 vs 631 万）。
            # 这里以本地日志为准覆盖 traj 表，让两个口径收敛。
            conn.executemany(
                "UPDATE task_traj_records SET task_done=? "
                "WHERE instance_id=? AND config_name=?",
                tr_rows,
            )
        # 任务异常/失败：直接定 failed，同时把 obs_state 也收掉，不让它再进 OBS 队列
        if failed_rows:
            # **UPSERT 而不是纯 UPDATE**：task_records 的行平时由在线
            # _sync_task_records 写，但实例跑太快结束 / 在线同步漏掉的 config
            # 在这里是**没有行**的 —— 纯 UPDATE 命中 0 行却把队列置成 done，
            # 这个任务就永久消失：页面上既不算已定级、也不算未评估，
            # 连 total 都不包含它。
            #
            # 实测全库 23,516 行是这种「队列有行、task_records 没行」的孤儿；
            # 某实例 4,161 个任务里 1,739 个因此丢失，页面显示成「未执行 1739」
            # （而其实它们全部跑过且失败了）。
            #
            # task_idx 可能为 NULL（tracker 投递时 task_records 还没建行、
            # 拿不到 idx），不影响：定级与 DONE 判定都不依赖它。
            conn.executemany(
                "INSERT INTO task_records "
                "  (instance_id, config_name, task_idx, status, traj_level, updated_at) "
                "VALUES (?,?,?,'任务异常','failed',CURRENT_TIMESTAMP) "
                "ON CONFLICT(instance_id, config_name) DO UPDATE SET "
                # 已定级的不覆盖（可能是别的路径判出的真实等级）
                "  traj_level=COALESCE(task_records.traj_level, 'failed')",
                failed_rows,
            )
            conn.executemany(
                "UPDATE shallow_queue SET obs_state='done' "
                "WHERE instance_id=? AND config_name=? AND obs_state='queued'",
                [(iid, cn) for iid, cn, _ in failed_rows],
            )
        if q_rows:
            conn.executemany(
                "UPDATE shallow_queue SET local_state=?, note=?, "
                "claimed_by=NULL, claimed_at=NULL, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=?",
                q_rows,
            )
        conn.commit()
    return stat


def run_once(concurrency: int, max_seconds: float = 0) -> dict:
    """跑一批。max_seconds > 0 时在同一轮里连续领取，直到队列空或超时。

    为什么要循环：claim 的 LIMIT 是 concurrency，每轮只领 64 条就 sleep 1s，
    68 万条要跑 5 小时。而实测判 64 条只要 0.15s —— 时间全耗在轮间睡眠上。

    线程池与 inst_cache 都提到循环外：
      - 每轮新建 64 线程池本身有开销（线程创建 + 销毁）
      - inst_cache 里存的是 (inst, logs_dir)，而 _logs_dir 每次都要 OmegaConf.load
        解析 config.yaml（实测 9.1ms/次）。每轮清空等于每轮重付一次
    """
    total = {"done": 0, "queued": 0, "skipped": 0, "claimed": 0}
    t_start = time.time()
    first = True
    inst_cache: dict = {}
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        while True:
            with get_connection() as conn:
                n_reclaim = reclaim_stuck(conn) if first else 0
                items = claim(conn, concurrency)
            if first and n_reclaim:
                print(f"[local] 回收超时 claimed {n_reclaim} 条", flush=True)
            first = False
            if not items:
                break

            results = []
            futs = {pool.submit(judge_one, it, inst_cache): it for it in items}
            for fut in as_completed(futs):
                it = futs[fut]
                try:
                    state, done_val, note = fut.result()
                except Exception as e:
                    state, done_val, note = "queued", None, f"判定异常: {e}"
                results.append((it, state, done_val, note))

            st = apply_results(results)
            for k in ("done", "queued", "skipped"):
                total[k] += st.get(k, 0)
            total["claimed"] += len(items)

            if max_seconds <= 0 or (time.time() - t_start) >= max_seconds:
                break
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description="浅层分析：本地日志 DONE 判定")
    ap.add_argument("--once", action="store_true", help="跑一轮即退出")
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help=f"并发数（默认 {DEFAULT_CONCURRENCY}）")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help=f"空闲时的轮询间隔秒（默认 {DEFAULT_INTERVAL}）")
    ap.add_argument("--max-seconds", type=float, default=30,
                    help="单轮最长连续处理秒数（0=不限）。轮内连续领，避免每轮只领 "
                         "concurrency 条就睡 1s 造成空转")
    a = ap.parse_args()

    print(f"[local] 启动: concurrency={a.concurrency} interval={a.interval}s "
          f"max_seconds={a.max_seconds}", flush=True)
    # 自愈补列：本进程只写 local_state 这一维，但同表的 artifact 维由
    # task_tracker / output_worker 写入 —— 缺列时它们会静默失败，这里先补齐。
    ensure_shallow_extra_columns()
    write_worker_started(WORKER_NAME, os.getpid(),
                         f"started concurrency={a.concurrency}")
    while True:
        t0 = time.time()
        # 必须先初始化：下面异常分支之后还要读 st.get("claimed")，
        # 未赋值会 UnboundLocalError 把整个进程带崩（deep worker 踩过同样的坑）。
        st: dict = {}
        try:
            st = run_once(a.concurrency, a.max_seconds)
            if st.get("claimed"):
                print(f"[local] 领取={st['claimed']} done={st.get('done',0)} "
                      f"留队={st.get('queued',0)} 跳过={st.get('skipped',0)} "
                      f"{time.time()-t0:.2f}s", flush=True)
            # 每轮都写心跳（哪怕 0 条）：监控页靠「最后心跳时间」判断进程是否还活着，
            # 只在有产出时写会让空闲的 worker 看起来已经死了。
            write_heartbeat(
                WORKER_NAME, os.getpid(), t0, time.time(),
                st.get("done", 0), st.get("skipped", 0),
                f"领取={st.get('claimed',0)} 留队={st.get('queued',0)}",
            )
        except KeyboardInterrupt:
            print("[local] 收到中断，退出", flush=True)
            return
        except Exception as e:
            print(f"[local] 本轮异常: {e}", flush=True)
            write_heartbeat(WORKER_NAME, os.getpid(), t0, time.time(), 0, 1,
                            f"本轮异常: {str(e)[:150]}")
        if a.once:
            break
        # 队列还有活就不睡太久：只领了 concurrency 条，固定长睡会让队列空转
        time.sleep(max(1, a.interval) if not st.get("claimed") else 1)


if __name__ == "__main__":
    main()
