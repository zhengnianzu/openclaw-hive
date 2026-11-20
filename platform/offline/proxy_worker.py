# -*- coding: utf-8 -*-
"""proxy_worker：采集「启动本地代理」实例的模型用量（logs_proxy）并落库。

## 背景

创建参数 `proxy_enable` 开启后，hive 在沙箱里起 `llm_proxy_lite`，模型流量改为经
本地端口转发；代理每次请求都会往 `logs_proxy/traj/index.jsonl` 追加一行（UTF-8），
覆盖结束前 hive 再跑一次统计脚本生成 `logs_proxy/proxy_summary.txt`，最后整个
`logs_proxy` 目录随轨迹传进 OBS（hive.py:_gen_proxy_summary / _upload_traj）。

平台此前完全读不到这些数据。本 worker 把它们聚合成两张表（见
api/core/database.py 的 PROXY_TABLES_DDL）：

  task_proxy_usage        每 (instance, traj) 一行：次数 / 成败 / token / 模型列表
  task_proxy_model_usage  每 (instance, traj, model) 一行：该模型下的次数与 token

## 为什么读 index.jsonl 而不是读 proxy_summary.txt

txt 有两套编码（Windows 沙箱下是 UTF-16LE + CRLF），且是聚合后的不可逆文本；
index.jsonl 是原始逐条记录，能同时算出总数与按模型明细，口径与
`llm_proxy_lite/print_stats_summary.py` 的 success 判定完全一致。另外 txt 由
hive 在收尾时「尽力而为」生成，harness 崩溃时可能缺失，而 index.jsonl 是代理
每请求即写，不受影响。

## 读路径零 obsutil

数据源是**本地产物缓存盘**上的 `<cache>/<batch>/<leaf>/logs_proxy/` —— 沙箱产物
已由 offline/output_worker.py 整目录 `obsutil cp -r` 下载到那里。所以本 worker
不碰 OBS、不需要凭据、不会触发 exit 8。

代价：产物下载是**点击驱动**的（`task_traj_records.artifacts_status`），没下载过的
traj 本地没有 logs_proxy —— 这类 traj 直接跳过并在统计里报数，不在这里触发下载。

## 增量

`source_mtime` / `source_lines` 是消费水位。实例运行中 index.jsonl 只增不改：

  · 不存在            → 跳过（代理没起 / 产物没下载）
  · mtime 与已存相同  → 跳过（绝大多数行每轮都走这条零成本路径，只做一次 stat）
  · 行数比已存少      → 重跑覆盖，从头全量重算
  · 其余              → 全量重读该文件（单 traj 最多几十万行，百毫秒级）

`--backfill` 忽略水位，强制重算（首版回填历史实例用）。

用法：
    python offline/proxy_worker.py                        # 常驻，60s 一轮
    python offline/proxy_worker.py --once                 # 跑一轮即退出
    python offline/proxy_worker.py --once --instance <id> # 只处理指定实例
    python offline/proxy_worker.py --backfill             # 全量重算
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.core.database import (                                    # noqa: E402
    get_connection, ensure_proxy_tables)
from src import offline_analysis as oa                             # noqa: E402
from offline.heartbeat import write_heartbeat, write_worker_started  # noqa: E402
from offline.task_tracker import (                                 # noqa: E402
    resolve_output_dir, find_records, LANE_REALTIME_MIN)

# worker 名随车道变化：监控页按名字画曲线，两条车道必须分开才看得出谁在干活。
WORKER_NAMES = {"realtime": "proxy-realtime", "auto": "proxy-auto",
                "local": "proxy"}
# 兼容旧引用：纯本地模式的日志前缀
WORKER_NAME = WORKER_NAMES["local"]
DEFAULT_INTERVAL = 60
# 实时车道默认轮得更快：task 刚跑完，用户在等这几个数字。
LANE_INTERVALS = {"realtime": 10, "auto": 60}
# 单轮并发。纯本地读盘 + 小 SQL + 偶发 25KB 的 cp，8 与产物下载 worker 同量级。
CONCURRENCY = int(os.environ.get("PROXY_WORKER_CONCURRENCY", "8"))

# 队列行的重试上限与超时回收阈值（与 output_worker 的产物队列同款语义）。
MAX_ATTEMPTS = 3
RECLAIM_AFTER_SEC = 30 * 60

# 可以采集的实例状态。created 阶段还没跑、不会有 logs_proxy，排除掉省一次 stat。
INSTANCE_STATUSES = ("running", "preparing", "finished", "stopped", "completed")

# index.jsonl 里的空模型名归到这里，避免按模型统计时整行被丢掉。
UNKNOWN_MODEL = "(unknown)"

# 本地没有 logs_proxy 的 traj 归属这些原因，仅用于日志统计，不入库。
SKIP_NO_FILE = "no_file"
SKIP_NO_CACHE = "no_cache"


# ── 实例与 traj 枚举 ────────────────────────────────────────────────────────

def _proxy_instances(conn, instance_id: str | None = None) -> list[dict]:
    """`proxy_enable=1` 的实例。

    create_params 是创建瞬间的请求快照（instances.py 把 req.model_dump() 原样存入），
    proxy_enable 就在里面。json_extract 对缺字段/非法 JSON 返回 NULL，不会抛错。
    """
    sql = ("SELECT id, name, status, config_path, create_params, config_snapshot "
           "FROM task_instances "
           "WHERE json_extract(create_params, '$.proxy_enable') = 1")
    params: tuple = ()
    if instance_id:
        sql += " AND id = ?"
        params = (instance_id,)
    else:
        ph = ",".join("?" * len(INSTANCE_STATUSES))
        sql += f" AND status IN ({ph})"
        params = INSTANCE_STATUSES
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    except Exception as e:
        print(f"[{WORKER_NAME}] 查询 proxy 实例失败: {str(e)[:150]}", flush=True)
        return []


def _config_by_traj(conn, instance_id: str) -> dict[str, str]:
    """traj_name → config_name，用于把 config 冗余进用量行（尽力而为，缺了不影响采集）。"""
    try:
        rows = conn.execute(
            "SELECT traj_name, config_name FROM task_traj_records WHERE instance_id=?",
            (instance_id,)).fetchall()
        return {r[0]: r[1] for r in rows if r[0]}
    except Exception:
        return {}


def _traj_names(inst: dict, config_map: dict[str, str]) -> list[str]:
    """该实例要采集的 traj 名单。

    优先 task_traj_records（worker 建的行即真实叶子名）；实例还没被任何 worker
    发现过时，回退读本地 complete.jsonl —— 与 output_worker._seed_task_records_from_logs
    同口径：config 文件名去掉 .json 就是 OBS 叶子目录。
    """
    if config_map:
        return sorted(config_map)
    names: list[str] = []
    try:
        out_dir = resolve_output_dir(inst.get("config_path") or "")
    except Exception:
        out_dir = None
    if not out_dir:
        return names
    for record in ("complete.jsonl", "failed.jsonl"):
        path = find_records(out_dir, record)
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    name = line[:-5] if line.endswith(".json") else line
                    if name and name not in names:
                        names.append(name)
        except OSError:
            continue
    return names


def _obs_base(inst: dict) -> str | None:
    """OBS 轨迹根。快照优先（不依赖挂载/配置文件是否还在），与 artifacts 路由同口径。"""
    base = oa.obs_base_from_snapshot(inst.get("config_snapshot"))
    if base:
        return base
    config_path = inst.get("config_path") or ""
    if config_path and os.path.exists(config_path):
        try:
            from omegaconf import OmegaConf
            cfg = OmegaConf.load(config_path)
            obs = cfg.run_config.obs
            return (f"{str(obs.traj_save_bucket).rstrip('/')}/"
                    f"{str(obs.traj_save_path).strip('/')}/")
        except Exception:
            return None
    return None


def _index_path(origin: str, obs_base: str, traj: str) -> str:
    """本地缓存里该 traj 的 logs_proxy 索引文件。

    落盘位置由 output_worker 的整目录 cp -r 决定，与 deep 详情同一套
    `_cache_subdir_for` 规则（task_cache_dir 内部复用它）。
    """
    return os.path.join(oa.task_cache_dir(origin, obs_base, traj),
                        "logs_proxy", "traj", "index.jsonl")


# ── 解析与聚合 ──────────────────────────────────────────────────────────────

def _is_success(entry: dict) -> bool:
    """成功判定，与 print_stats_summary._aggregate_index_entries 逐字一致。

    新索引靠 HTTP 状态 + success 标记；旧索引没有 status_code，只能看 tok_out。
    """
    tok_out = entry.get("tok_out", 0) or 0
    return bool(entry.get("success", False)) and ("status_code" in entry or tok_out > 0)


def _aggregate(index_path: str) -> dict:
    """把 index.jsonl 聚合成一行汇总 + 按模型明细。

    逐行流式读：单文件可能几十万行，整读进内存没必要；坏行（代理写日志时被 kill
    留下的半截行）跳过而不是整份失败。
    """
    total = {"calls": 0, "err": 0, "tin": 0, "tout": 0,
             "first": None, "last": None}
    models: dict[str, dict] = {}
    n_lines = 0
    with open(index_path, "rb") as f:
        for raw in f:
            n_lines += 1
            line = raw.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(entry, dict):
                continue

            model = entry.get("model") or ""
            model = model if isinstance(model, str) and model else UNKNOWN_MODEL
            tok_in = entry.get("tok_in", 0) or 0
            tok_out = entry.get("tok_out", 0) or 0
            ok = _is_success(entry)

            ts = entry.get("ts") or ""
            if isinstance(ts, str) and ts:
                if total["first"] is None or ts < total["first"]:
                    total["first"] = ts
                if total["last"] is None or ts > total["last"]:
                    total["last"] = ts

            total["calls"] += 1
            m = models.setdefault(model, {"calls": 0, "err": 0, "tin": 0, "tout": 0})
            m["calls"] += 1
            if ok:
                total["tin"] += tok_in
                total["tout"] += tok_out
                m["tin"] += tok_in
                m["tout"] += tok_out
            else:
                # 失败调用只计输入（与 stats 脚本一致：响应没产出 output token）
                total["err"] += 1
                m["err"] += 1
                total["tin"] += tok_in
                m["tin"] += tok_in
    return {"total": total, "models": models, "lines": n_lines}


# ── 落库 ────────────────────────────────────────────────────────────────────

def _stored_state(conn, instance_id: str, traj: str) -> tuple[float | None, int | None]:
    """该 traj 上次的 (source_mtime, source_lines)；没有行返回 (None, None)。"""
    try:
        row = conn.execute(
            "SELECT source_mtime, source_lines FROM task_proxy_usage "
            "WHERE instance_id=? AND traj_name=?", (instance_id, traj)).fetchone()
    except Exception:
        return None, None
    return (row[0], row[1]) if row else (None, None)


def _save(conn, instance_id: str, traj: str, config_name: str | None,
          agg: dict, st_mtime: float) -> None:
    """一个 traj 的用量写进库。整段在一个事务里，先删后插按模型行避免残留旧模型。"""
    total = agg["total"]
    models = agg["models"]
    model_names = ",".join(sorted(models))
    now = time.time()
    conn.execute(
        "INSERT INTO task_proxy_usage ("
        "  instance_id, traj_name, config_name, calls_total, calls_success, "
        "  calls_error, tok_in, tok_out, tok_total, models, first_call_ts, "
        "  last_call_ts, source_mtime, source_lines, computed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(instance_id, traj_name) DO UPDATE SET "
        "  config_name=excluded.config_name, "
        "  calls_total=excluded.calls_total, calls_success=excluded.calls_success, "
        "  calls_error=excluded.calls_error, tok_in=excluded.tok_in, "
        "  tok_out=excluded.tok_out, tok_total=excluded.tok_total, "
        "  models=excluded.models, first_call_ts=excluded.first_call_ts, "
        "  last_call_ts=excluded.last_call_ts, source_mtime=excluded.source_mtime, "
        "  source_lines=excluded.source_lines, computed_at=excluded.computed_at",
        (instance_id, traj, config_name,
         total["calls"], total["calls"] - total["err"], total["err"],
         total["tin"], total["tout"], total["tin"] + total["tout"],
         model_names, total["first"], total["last"],
         st_mtime, agg["lines"], now))
    conn.execute(
        "DELETE FROM task_proxy_model_usage WHERE instance_id=? AND traj_name=?",
        (instance_id, traj))
    conn.executemany(
        "INSERT INTO task_proxy_model_usage ("
        "  instance_id, traj_name, model, calls, calls_error, tok_in, tok_out) "
        "VALUES (?,?,?,?,?,?,?)",
        [(instance_id, traj, name, m["calls"], m["err"], m["tin"], m["tout"])
         for name, m in sorted(models.items())])


# ── 单个 traj ───────────────────────────────────────────────────────────────

def _process_one(inst: dict, traj: str, config_name: str | None,
                 obs_base: str, origin: str, backfill: bool) -> tuple:
    """采集一个 traj。返回 (状态, 说明)。

    状态：ok / unchanged / no_file / error
    """
    path = _index_path(origin, obs_base, traj)
    try:
        st = os.stat(path)
    except OSError:
        return (SKIP_NO_FILE, traj)

    with get_connection() as conn:
        prev_mtime, prev_lines = _stored_state(conn, inst["id"], traj)
    if (not backfill and prev_mtime is not None
            and abs(st.st_mtime - prev_mtime) < 1e-6):
        # 水位命中：只读过一次 stat。绝大多数 traj 每轮都走这里。
        return ("unchanged", traj)

    try:
        agg = _aggregate(path)
    except OSError as e:
        return ("error", f"{traj}: {str(e)[:120]}")

    # mtime 变了但行数没变：内容其实没动（touch / 拷回时 mtime 被重写），
    # 只刷新水位，不重算存储 —— 省一次写事务。
    if (not backfill and prev_lines is not None and agg["lines"] == prev_lines):
        with get_connection() as conn:
            conn.execute(
                "UPDATE task_proxy_usage SET source_mtime=? "
                "WHERE instance_id=? AND traj_name=?",
                (st.st_mtime, inst["id"], traj))
        return ("unchanged", traj)
    if agg["lines"] < (prev_lines or 0) and not backfill:
        # 行数变少 = 文件被重写（重跑覆盖），照常按新内容算，无需特殊处理。
        pass

    try:
        with get_connection() as conn:
            _save(conn, inst["id"], traj, config_name, agg, st.st_mtime)
    except Exception as e:
        return ("error", f"{traj}: {str(e)[:150]}")
    return ("ok", f"{traj} calls={agg['total']['calls']} "
                  f"tok={agg['total']['tin'] + agg['total']['tout']}")


# ── 队列领取（realtime / auto 双车道） ──────────────────────────────────────

def _reclaim_stuck(conn) -> int:
    """回收 claimed 超时的行，打回 queued。worker 被 kill 时留下的僵尸行靠它自愈。"""
    cutoff = time.time() - RECLAIM_AFTER_SEC
    cur = conn.execute(
        "UPDATE task_proxy_queue SET state='queued', claimed_by=NULL, claimed_at=NULL "
        "WHERE state='claimed' AND claimed_at IS NOT NULL AND claimed_at < ?",
        (cutoff,))
    n = cur.rowcount or 0
    if n:
        conn.commit()
        print(f"[proxy] 回收超时 claimed {n} 行", flush=True)
    return n


def _claim(conn, lane: str, limit: int,
           instance_id: str | None = None) -> list[dict]:
    """CAS 领取本车道的待办行。realtime 领 [90,∞)，auto 领 [0,90) —— 区间互斥，
    两条车道的两个进程同时跑不会抢到同一行。"""
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    if lane == "realtime":
        where, params = "priority >= ?", (LANE_REALTIME_MIN,)
    else:
        where, params = "priority < ?", (LANE_REALTIME_MIN,)
    if instance_id:
        where += " AND instance_id=?"
        params = params + (instance_id,)

    out: list[dict] = []
    rows = conn.execute(
        f"SELECT id, instance_id, traj_name, config_name, attempts, priority "
        f"FROM task_proxy_queue WHERE state='queued' AND {where} "
        f"ORDER BY priority DESC, id LIMIT ?",
        params + (limit,)).fetchall()
    for r in rows:
        r = dict(r)
        cur = conn.execute(
            "UPDATE task_proxy_queue SET state='claimed', claimed_by=?, claimed_at=? "
            "WHERE id=? AND state='queued'",
            (worker_id, time.time(), r["id"]))
        if cur.rowcount == 1:          # CAS：被别人抢先则跳过
            out.append(r)
    conn.commit()
    return out


def _finish(conn, row: dict, status: str, note: str = "") -> None:
    """结算一行。失败累计 attempts，到上限转 skipped，不再无限重试。"""
    if status == "ok":
        conn.execute(
            "UPDATE task_proxy_queue SET state='done', claimed_by=NULL, "
            "claimed_at=NULL, note=? WHERE id=?", ((note or "")[:300], row["id"]))
    else:
        attempts = int(row.get("attempts") or 0) + 1
        state = "skipped" if attempts >= MAX_ATTEMPTS else "queued"
        conn.execute(
            "UPDATE task_proxy_queue SET state=?, attempts=?, claimed_by=NULL, "
            "claimed_at=NULL, note=? WHERE id=?",
            (state, attempts, (note or "")[:300], row["id"]))
    conn.commit()


def _ensure_index(inst: dict, traj: str, obs_base: str, origin: str,
                  obsutil: str) -> tuple[bool, str]:
    """把该 traj 的 `logs_proxy/traj/index.jsonl` 定向拉到本地缓存。

    只拉这一个文件，不是整个 task 目录 —— 实测整份交付物 60MB，而 index.jsonl
    只有 25KB（大头是 traj/data/*.jsonl，单条请求记录就有 7.5MB）。

    本地已有的交给 `_process_one` 的 mtime 水位判断，不会重复下载：产物被
    「点击下载」过的 task 本地本来就有 index.jsonl，这里直接命中返回。
    """
    path = _index_path(origin, obs_base, traj)
    if os.path.exists(path):
        return True, ""
    task_obs = obs_base.rstrip("/") + "/" + traj.strip("/") + "/"
    src = task_obs + "logs_proxy/traj/index.jsonl"
    dest_dir = os.path.dirname(path)
    try:
        os.makedirs(dest_dir, exist_ok=True)
        oa.run_obsutil_cp(obsutil, src, dest_dir, recursive=False)
        return True, ""
    except Exception as e:
        return False, str(e)[:200]


def _collect_one(row: dict, origin: str, obsutil: str,
                 backfill: bool = False) -> tuple:
    """采集队列里的一行。返回 (status, note)。"""
    traj = row["traj_name"]
    iid = row["instance_id"]
    with get_connection() as conn:
        inst = _instance_by_id(conn, iid)
    if not inst:
        return ("error", f"{traj}: 实例不存在")
    base = _obs_base(inst)
    if not base:
        return ("error", f"{traj}: 无法解析 OBS 轨迹根")

    ok, err = _ensure_index(inst, traj, base, origin, obsutil)
    if not ok:
        # obsutil exit 6 = 对象不存在。运行中实例的 task 刚结束后 OBS 传输需要几秒，
        # 或者代理没起来 / 老版本 hive 不传 logs_proxy。
        # 区分：实例 running/preparing 时文件不存在很正常，打回 queued（不耗重试次数），
        # 下一轮自动再试；已结束实例 3 次后才判 skipped，避免无效重试。
        from offline.task_tracker import ACTIVE_STATUSES
        still_running = inst.get("status") in ACTIVE_STATUSES
        if still_running:
            return ("retry", f"{traj}: 实例仍在运行，OBS 文件未就绪")
        return ("error", f"{traj}: 拉取 index.jsonl 失败 {err}")

    status, detail = _process_one(inst, traj, row.get("config_name"),
                                  base, origin, backfill)
    if status in (SKIP_NO_FILE, SKIP_NO_CACHE):
        # 拉取成功但文件不存在：代理没起来 / 老版本 hive 没传 logs_proxy。
        # 判 skipped 而不是 error —— 重试也不会变出文件来。
        return ("empty", detail)
    if status == "error":
        return ("error", detail)
    return ("ok", detail)


def _run_lane(origin: str, lane: str, obsutil: str,
              instance_id: str | None = None, backfill: bool = False) -> dict:
    """双车道主循环体：领取 → 采集 → 结算，一轮内连续领取直到队列空。"""
    ensure_proxy_tables()
    stats = {"claimed": 0, "ok": 0, "empty": 0, "error": 0, "errors": []}
    with get_connection() as conn:
        _reclaim_stuck(conn)

    while True:
        with get_connection() as conn:
            rows = _claim(conn, lane, CONCURRENCY, instance_id)
        if not rows:
            break
        stats["claimed"] += len(rows)

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = {pool.submit(_collect_one, r, origin, obsutil, backfill): r
                       for r in rows}
            for fut in as_completed(futures):
                row = futures[fut]
                try:
                    status, note = fut.result()
                except Exception as e:      # 单行异常不能带垮整轮
                    status, note = "error", str(e)[:200]
                with get_connection() as conn:
                    if status == "retry":
                        # 运行中实例 OBS 文件未就绪：打回 queued，不累计 attempts。
                        conn.execute(
                            "UPDATE task_proxy_queue SET state='queued', "
                            "claimed_by=NULL, claimed_at=NULL, note=? WHERE id=?",
                            ((note or "")[:300], row["id"]))
                        conn.commit()
                    elif status == "empty":
                        conn.execute(
                            "UPDATE task_proxy_queue SET state='skipped', "
                            "claimed_by=NULL, claimed_at=NULL, note=? WHERE id=?",
                            ((note or "")[:300], row["id"]))
                        conn.commit()
                    else:
                        _finish(conn, row, status, note)
                if status == "ok":
                    stats["ok"] += 1
                    print(f"    [proxy-{lane}] {note}", flush=True)
                elif status in ("empty", "retry"):
                    stats["empty"] += 1
                else:
                    stats["error"] += 1
                    stats["errors"].append(note)
    return stats


def _instance_by_id(conn, instance_id: str) -> dict | None:
    try:
        row = conn.execute(
            "SELECT id, name, status, config_path, create_params, config_snapshot "
            "FROM task_instances WHERE id=?", (instance_id,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


# ── 纯本地模式（回填 / 排障退路） ───────────────────────────────────────────

def run_once(origin: str, instance_id: str | None = None,
             backfill: bool = False) -> dict:
    """扫一遍全部 proxy 实例，返回本轮统计。"""
    ensure_proxy_tables()
    stats = {"instances": 0, "trajs": 0, "ok": 0, "unchanged": 0,
             "no_file": 0, "error": 0, "errors": []}
    if not origin:
        print(f"[{WORKER_NAME}] OUTPUT_CACHE 未配置，无法定位本地产物", flush=True)
        return stats

    with get_connection() as conn:
        insts = _proxy_instances(conn, instance_id)
        plans = []
        for inst in insts:
            config_map = _config_by_traj(conn, inst["id"])
            trajs = _traj_names(inst, config_map)
            if trajs:
                plans.append((inst, trajs, config_map))
    stats["instances"] = len(plans)
    if not plans:
        return stats

    # 并发单位是实例而不是 traj：同一实例的 traj 共用一个 obs_base，拆开反而多算路径。
    def _do(plan):
        inst, trajs, config_map = plan
        base = _obs_base(inst)
        if not base:
            return [(SKIP_NO_CACHE, t) for t in trajs]
        out = []
        for t in trajs:
            try:
                out.append(_process_one(inst, t, config_map.get(t), base, origin, backfill))
            except Exception as e:      # 单 traj 异常不能带垮整轮
                out.append(("error", f"{t}: {str(e)[:150]}"))
        return out

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(_do, p) for p in plans]
        for fut in as_completed(futures):
            try:
                results = fut.result()
            except Exception as e:
                stats["error"] += 1
                stats["errors"].append(str(e)[:150])
                continue
            for status, detail in results:
                stats["trajs"] += 1
                if status == "ok":
                    stats["ok"] += 1
                    print(f"    [proxy] {detail}", flush=True)
                elif status == "unchanged":
                    stats["unchanged"] += 1
                elif status in (SKIP_NO_FILE, SKIP_NO_CACHE):
                    stats["no_file"] += 1
                else:
                    stats["error"] += 1
                    stats["errors"].append(detail)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="采集 logs_proxy 代理用量")
    ap.add_argument("--lane", choices=("realtime", "auto", "local"), default="local",
                    help="realtime=领实时车道(priority>=90)，auto=领背景车道，"
                         "local=只扫本地缓存不碰 OBS（回填/排障）")
    ap.add_argument("--once", action="store_true", help="跑一轮即退出（调试/回填）")
    ap.add_argument("--interval", type=int, default=None,
                    help="常驻轮询间隔（秒）；默认按车道取 10/60")
    ap.add_argument("--instance", default=None, help="只处理指定实例（调试）")
    ap.add_argument("--backfill", action="store_true",
                    help="忽略水位强制重算（历史回填用）")
    ap.add_argument("--cache-dir", dest="origin", default=oa.DEFAULT_OUTPUT_CACHE,
                    help=f"本地产物缓存根（默认 {oa.DEFAULT_OUTPUT_CACHE}）")
    ap.add_argument("--obsutil", default=oa.DEFAULT_OBSUTIL,
                    help=f"obsutil 可执行文件（默认 {oa.DEFAULT_OBSUTIL}）")
    a = ap.parse_args()

    name = WORKER_NAMES[a.lane]
    interval = a.interval or LANE_INTERVALS.get(a.lane, DEFAULT_INTERVAL)
    ensure_proxy_tables()
    write_worker_started(name, os.getpid(),
                         f"started lane={a.lane} interval={interval} "
                         f"backfill={a.backfill}")

    while True:
        t0 = time.time()
        st: dict = {}
        try:
            if a.lane == "local":
                st = run_once(a.origin, a.instance, a.backfill)
                print(f"[{name}] 实例={st['instances']} traj={st['trajs']} "
                      f"更新={st['ok']} 未变={st['unchanged']} "
                      f"无数据={st['no_file']} 失败={st['error']} "
                      f"{time.time() - t0:.2f}s", flush=True)
                for err in st.get("errors", [])[:10]:
                    print(f"    [{name}] 失败: {err}", flush=True)
                write_heartbeat(name, os.getpid(), t0, time.time(),
                                st.get("ok", 0), st.get("error", 0),
                                f"traj={st.get('trajs', 0)} 无数据={st.get('no_file', 0)}")
            else:
                st = _run_lane(a.origin, a.lane, a.obsutil,
                               a.instance, a.backfill)
                print(f"[{name}] 领取={st['claimed']} 更新={st['ok']} "
                      f"无数据={st['empty']} 失败={st['error']} "
                      f"{time.time() - t0:.2f}s", flush=True)
                for err in st.get("errors", [])[:10]:
                    print(f"    [{name}] 失败: {err}", flush=True)
                write_heartbeat(name, os.getpid(), t0, time.time(),
                                st.get("ok", 0), st.get("error", 0),
                                f"领取={st.get('claimed', 0)}")
        except KeyboardInterrupt:
            print(f"[{name}] 收到中断，退出", flush=True)
            return
        except Exception as e:
            print(f"[{name}] 本轮异常: {e}", flush=True)
            write_heartbeat(name, os.getpid(), t0, time.time(), 0, 1,
                            f"本轮异常: {str(e)[:150]}")
        if a.once:
            break
        time.sleep(max(1, interval))


if __name__ == "__main__":
    main()
