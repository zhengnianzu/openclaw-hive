# -*- coding: utf-8 -*-
"""输出分析 worker：独立进程，把 task_records 的轨迹分级 + 轨迹明细回填进 platform.db。

数据流（README_outputview.md §二）：
  浅层（分级，常驻循环）：
    task_records 中 traj_level IS NULL 的行 → 从 config_path 解析 OBS 轨迹根（logs.py:_get_obs_base_path
    同款）→ _load_per_task_entries 快路径（tsr 陈旧自动回退慢路径）→ compute_level（dropped 归 L0）
    → 回填 task_records.traj_level + UPSERT task_traj_records（status='done'）。
    finished 实例：在线 _sync_task_records 已停更，多 task 并发终止时可能竞态漏写部分行；
    先 _backfill_finished_tasks 从 OBS 轨迹枚举补全缺失行（task_records INSERT + 分级 +
    task_traj_records）。
    浅层只做一次（无 stale 回收）：task_traj_records.shallow_status 记录处理状态——
    processed(已分级)/empty(OBS 无轨迹目录,非 running 一次到位)/error(出错)。
    running 实例 OBS 未命中的行保持 NULL，下轮重新 ls_obs_dir 重试；已处理行不再重复。
  深层（按需，仅用户点开会话详情/轨迹/日志时经 API 置 status='pending' 触发）：
    status='pending' → download_task_detail + load_task_detail → 回填 5 个路径列 → done | failed。

⚠ 关键设计约束：worker 写 task_records 的 UPDATE 不得触碰 updated_at。
task_records.updated_at 语义 =「在线 _sync_task_records 最后写入时间」（instances.py:1331 每次 UPSERT
都 CURRENT_TIMESTAMP），是前端/在线同步判断行新鲜度的依据。若 worker 分级时也刷 updated_at，
会把行「更新」得看似在线刚写入，干扰该语义。故：
  - 分级回填：UPDATE ... SET traj_level=?  WHERE instance_id=? AND config_name=?（不写 updated_at）
  - 浅层处理状态写 task_traj_records.shallow_status（不依赖 updated_at，不做 stale 回算）
  - finished 补全 INSERT 新行必须写 updated_at（否则行无写入时间），但 ON CONFLICT
    分支不更新已有行的 updated_at。

用法:
  python offline/output_worker.py --once                     # 扫一遍浅层 + 深层后退出（调试/CI）
  python offline/output_worker.py --interval 60              # 常驻，每 60s 一轮（默认）
  python offline/output_worker.py --once --deep-only         # 只消费深层 pending 队列
  python offline/output_worker.py --once --instance <id>     # 只处理指定实例（调试）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from src import offline_analysis as oa            # noqa: E402
from api.core.config import settings               # noqa: E402
from api.core.database import get_connection       # noqa: E402

# 默认常驻轮询间隔（秒）
DEFAULT_INTERVAL = 60
# （已废弃）分级 stale 阈值：浅层只做一次，不再按此阈值重算。保留仅为 --stale-after 默认值兼容。
STALE_AFTER_SECONDS = 600
# finished 实例补全每轮预算：有缺口的 finished 实例可能数十个、单实例 OBS 枚举+拉 tsr
# 要几十秒。限制每轮处理数，避免一轮全量扫 OBS 卡死数小时；其余实例摊到后续轮次。
# 预算只约束 finished 补全，不约束 running/preparing（分级很快）。
BACKFILL_MAX_INSTANCES_PER_ROUND = 8
# 每实例每轮补全缺口配额：大缺口实例（数千~数万行）一轮补不完，
# 限制单实例单轮处理行数，避免一轮卡死；剩余缺口留到后续轮次。
BACKFILL_MAX_TASKS_PER_INSTANCE = 300
# 补全循环并发度：OBS 拉 tsr 是 I/O 密集，串行逐行等下载极慢。实测 16 并发 74行/s、
# 24-32 并发 85-90行/s（OBS 在 ~32 以上饱和回落）。默认 16 已达成 200 行 <3s。
BACKFILL_CONCURRENCY = int(os.environ.get("BACKFILL_CONCURRENCY", "16"))
# 浅层分级并发度：同一实例内并行处理多个 task 行（OBS 下载是 I/O 密集，串行太慢）。
# obsutil 走独立 subprocess、DB 写用独立连接（WAL），线程安全。过高会打爆 OBS 带宽。
SHALLOW_CONCURRENCY = int(os.environ.get("SHALLOW_CONCURRENCY", "16"))
# 深层下载并发度（_consume_deep 用）：同理，I/O 密集。设低些避免与浅层抢 OBS。
DEEP_CONCURRENCY = int(os.environ.get("DEEP_CONCURRENCY", "2"))


# ============ OBS 根 / 实例 → 队列 ============

def _obs_base_path(config_path: str) -> str:
    """与 logs.py:_get_obs_base_path 同款（不引入 FastAPI HTTPException）。"""
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    from omegaconf import OmegaConf
    cfg = OmegaConf.load(config_path)
    traj_bucket = cfg.run_config.obs.traj_save_bucket
    traj_path = cfg.run_config.obs.traj_save_path
    return f"{traj_bucket}/{traj_path}/"


def _pick_running_instances(conn) -> list[dict]:
    """浅层队列来源：running/preparing 的实例（其 task_records 由在线 _sync_task_records 维护）
    + 被点击登记的 finished/completed/stopped 实例（shallow_requests 表，见 POST /{instance_id}/shallow）。

    finished/completed/stopped 实例不再无条件全量拉取（历史缺口动辄数十万行，全量扫永远追不完）；
    只有用户打开详情页 POST /{id}/shallow 登记过的才进队列。worker 处理完（实例无缺口）由
    _clear_shallow_request 删登记出队（completed 同时置 output_status='done'）。

    不用时间窗口：stopped_at 是本地时间（UTC+8）而 SQLite datetime('now') 是 UTC，直接
    比较错位 8h；且按"是否有缺口"过滤是零成本（有索引/聚合），只有缺口实例才碰 OBS。
    """
    rows = conn.execute(
        "SELECT ti.id, ti.name, ti.config_path, ti.status "
        "FROM task_instances ti "
        "WHERE ti.status IN ('running','preparing') "
        "OR (ti.status IN ('finished','completed','stopped') AND EXISTS ("
        "    SELECT 1 FROM shallow_requests sr WHERE sr.instance_id = ti.id)) "
        "ORDER BY ti.created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def _has_shallow_gaps(conn, instance_id: str) -> bool:
    """该实例是否仍有浅层缺口（traj_level IS NULL 行）。用作是否删除登记/出队的判据。"""
    return conn.execute(
        "SELECT 1 FROM task_records WHERE instance_id=? AND traj_level IS NULL LIMIT 1",
        (instance_id,),
    ).fetchone() is not None


def _clear_shallow_request(conn, instance_id: str) -> None:
    """实例浅层处理完（无缺口）后删除登记，出队。幂等：无登记时静默。"""
    conn.execute("DELETE FROM shallow_requests WHERE instance_id=?", (instance_id,))


def _pick_pending_rows(conn, instance_id: str | None = None) -> list[dict]:
    """深层队列：task_traj_records.status='pending'。"""
    if instance_id:
        rows = conn.execute(
            "SELECT * FROM task_traj_records WHERE instance_id=? AND status='pending'",
            (instance_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM task_traj_records WHERE status='pending' ORDER BY updated_at"
        ).fetchall()
    return [dict(r) for r in rows]


def _instance_by_id(conn, instance_id: str) -> dict | None:
    row = conn.execute(
        "SELECT id, name, config_path FROM task_instances WHERE id=?", (instance_id,)
    ).fetchone()
    return dict(row) if row else None


def _make_obs_base_for(inst: dict) -> str:
    """取 obs_base；实例的 config.yaml 不存在时抛 FileNotFoundError（调用方标 failed / 跳过）。"""
    return _obs_base_path(inst["config_path"])


# ============ 浅层：单实例分级 ============

def _level_rows_for_instance(inst: dict) -> list[dict]:
    """枚举该实例 task_records 的（traj_name 可用）行 + 对应 task_traj_records 浅层状态。

    LEFT JOIN task_traj_records 取该 config 的 shallow_status：任一轨迹行已处理
    （processed/empty/error 非 NULL）即视为该 task 已做过浅层分析，驱动源据此跳过。
    返回 dict 列表（含 shallow_status，无轨迹行为 NULL）。
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT tr.task_idx, tr.config_name, tr.traj_level, tr.updated_at, "
            "       MAX(ttr.shallow_status) AS shallow_status "
            "FROM task_records tr "
            "LEFT JOIN task_traj_records ttr "
            "  ON ttr.instance_id=tr.instance_id AND ttr.config_name=tr.config_name "
            "WHERE tr.instance_id=? GROUP BY tr.config_name ORDER BY tr.task_idx",
            (inst["id"],),
        ).fetchall()
    return [dict(r) for r in rows]


def _upsert_traj_record(inst: dict, tr: dict, task_idx, config_name,
                        level: str, harness: str, ps: dict, traj_rel: str | None,
                        status: str = "done", error: str | None = None,
                        traj_name: str | None = None,
                        shallow_status: str | None = None,
                        shallow_error: str | None = None) -> None:
    """task_traj_records UPSERT（ON CONFLICT 更新分级列，不动 status/路径列）。

    tr 为 task_records 行或 per-task entry（traj_name 从中取，缺列时回退 config_name
    leaf）；ps 为 per-task entry（keys: task/tool_calls/plain_rounds/passed_gate/has_eval/
    evaluator_completion/task_done/…），evaluator_completion 落 completion 列。
    shallow_status: 浅层处理状态（processed/empty/error），None 时保持列不改（只写一次）。
    """
    if traj_name is None:
        traj_name = tr.get("traj_name") or tr.get("config_name") or tr.get("task") or ""
    if "/" in traj_name:
        traj_name = traj_name.rstrip("/").rsplit("/", 1)[-1]
    comp = ps.get("evaluator_completion", ps.get("completion"))
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO task_traj_records
                (instance_id, task_idx, config_name, traj_name, level, harness,
                 passed_gate, has_eval, task_done, completion,
                 tool_calls, plain_rounds,
                 input_tokens, output_tokens, reasoning_tokens, total_tokens,
                 char_len, trajectory_rel, status, error,
                 shallow_status, shallow_error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(instance_id, config_name, traj_name) DO UPDATE SET
                level           = excluded.level,
                harness         = excluded.harness,
                passed_gate     = excluded.passed_gate,
                has_eval        = excluded.has_eval,
                task_done       = excluded.task_done,
                completion      = excluded.completion,
                tool_calls      = excluded.tool_calls,
                plain_rounds    = excluded.plain_rounds,
                input_tokens    = excluded.input_tokens,
                output_tokens   = excluded.output_tokens,
                reasoning_tokens= excluded.reasoning_tokens,
                total_tokens    = excluded.total_tokens,
                char_len        = excluded.char_len,
                trajectory_rel  = excluded.trajectory_rel,
                error           = excluded.error,
                updated_at      = CURRENT_TIMESTAMP
            """,
            (inst["id"], task_idx, config_name, traj_name, level, harness,
             int(bool(ps.get("passed_gate"))), int(bool(ps.get("has_eval"))),
             int(bool(ps.get("task_done"))), comp,
             ps.get("tool_calls"), ps.get("plain_rounds"),
             ps.get("input_tokens"), ps.get("output_tokens"),
             ps.get("reasoning_tokens"), ps.get("total_tokens"),
             ps.get("char_len"), traj_rel, status, error,
             shallow_status, shallow_error),
        )


def _apply_level_to_task_records(inst: dict, config_name: str, level: str,
                                 ps: dict | None = None) -> None:
    """回填 task_records.traj_level（及 ps 提供时的 evaluator 评分字段）——不触碰 updated_at。

    会话列表的 Completion/评分 列读 task_records.eval_completion/eval_score，但这两列
    在线只由 _eval_score.json 同步——离线分级路径该 JSON 常不存在，导致恒空。此处直接用
    离线 entry 已抽好的 evaluator 评分（evaluator_completion，来自 evaluator log 的
    evaluation.completion）一并回填：eval_completion 与 eval_score 同源（都是 evaluator
    给完成度的评分，非 rubric 桶加权累加）。ps 为 per-task entry（None 表示无解析结果）。
    """
    with get_connection() as conn:
        if ps:
            completion = ps.get("evaluator_completion")
            conn.execute(
                "UPDATE task_records SET traj_level=?, "
                "eval_completion=COALESCE(?, eval_completion), "
                "eval_score=COALESCE(?, eval_score), "
                "gate=COALESCE(?, gate) "
                "WHERE instance_id=? AND config_name=?",
                (level, completion, completion,
                 int(bool(ps.get("passed_gate"))) if ps.get("passed_gate") is not None else None,
                 inst["id"], config_name),
            )
        else:
            conn.execute(
                "UPDATE task_records SET traj_level=? WHERE instance_id=? AND config_name=?",
                (level, inst["id"], config_name),
            )


# ============ finished 实例补全（OBS 轨迹 → task_records 缺失行 + 分级） ============

_BEGIN_CFG_RE = re.compile(r"BEGIN config=(?P<config>\S+)")
_FINAL_STATUS_RE = re.compile(
    r"任务执行状态=(?P<status>任务成功|任务失败|任务异常)\s+error_code=(?P<code>\S+)"
)


def _read_task_log_map(inst: dict) -> dict:
    """扫实例 outputs/config/logs/task-*.log，得到 {config_name: {task_idx, status, code}}。

    finished 实例在线 _sync_task_records 可能因竞态漏写部分行（多 task 并发终止时
    8s 轮询只捕获部分文件变化），这里从任务日志补齐缺失行的 status/code/task_idx。
    日志不存在/读不到时返回空 dict（调用方退化为默认值）。
    """
    out = {}
    logs_dir = os.path.join(os.path.dirname(inst["config_path"]), "outputs", "config", "logs")
    if not os.path.isdir(logs_dir):
        return out
    for fn in sorted(os.listdir(logs_dir)):
        m = re.match(r"task-(\d+)\.log$", fn)
        if not m:
            continue
        idx = int(m.group(1))
        config_name = None
        status = None
        code = None
        try:
            with open(os.path.join(logs_dir, fn), errors="replace") as f:
                for line in f:
                    mb = _BEGIN_CFG_RE.search(line)
                    if mb:
                        config_name = mb.group("config")
                    ms = _FINAL_STATUS_RE.search(line)
                    if ms:
                        status = ms.group("status")
                        code = ms.group("code")
        except OSError:
            continue
        if config_name:
            out[config_name] = {"task_idx": idx, "status": status or "任务异常", "code": code}
    return out


# ============ 单任务日志（按需，仅补齐缺失 config 的 status/code） ============

def _read_task_log_for(inst: dict, task_idx: int) -> dict:
    """读单个 task-<idx>.log，返回 {task_idx, config_name, status, code}（读不到返回空 dict）。"""
    log = os.path.join(os.path.dirname(inst["config_path"]),
                       "outputs", "config", "logs", f"task-{task_idx}.log")
    if not os.path.isfile(log):
        return {}
    config_name = status = code = None
    try:
        with open(log, errors="replace") as f:
            for line in f:
                mb = _BEGIN_CFG_RE.search(line)
                if mb:
                    config_name = mb.group("config")
                ms = _FINAL_STATUS_RE.search(line)
                if ms:
                    status = ms.group("status")
                    code = ms.group("code")
    except OSError:
        return {}
    return {"task_idx": task_idx, "config_name": config_name,
            "status": status or "任务异常", "code": code}


def _fetch_tsr_entries_fast(obsutil: str, task_obs: str, origin: str) -> list[dict]:
    """补全专用快路径：拉单个 task 的 logs/traj_stats_result.json（约 1KB）→ tsr entries。

    本地已有同源 tsr 缓存时直接读不重复下载（-f 强制覆盖会浪费 OBS 往返）。
    返回 [] = 无 tsr / 解析失败（不慢路径下载，调用方占位）。task_done 不在这里拉
    （那是详情列，留深层按需；补全只关心分级所需的 tool_calls/plain_rounds）。
    """
    task_obs = task_obs if task_obs.endswith("/") else task_obs + "/"
    leaf = task_obs.rstrip("/").rsplit("/", 1)[-1]
    sub = oa._cache_subdir_for(task_obs)
    dest = os.path.join(origin, sub, "logs", "traj_stats_result.json")
    _t0 = time.time()
    if not os.path.isfile(dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        obj_url = task_obs + "logs/traj_stats_result.json"
        cmd = [obsutil, "cp", obj_url, dest, "-f"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=60)
        except Exception:
            print(f"    [tsr-cache] {leaf}: OBS cp 异常返回", flush=True)
            return []
        if res.returncode != 0 or not os.path.isfile(dest):
            print(f"    [tsr-cache] {leaf}: OBS 拉 tsr 失败 rc={res.returncode} "
                  f"{time.time()-_t0:.2f}s", flush=True)
            return []
        print(f"    [tsr-cache] {leaf}: OBS 拉 tsr 成功 {time.time()-_t0:.2f}s", flush=True)
    else:
        print(f"    [tsr-cache] {leaf}: 本地 tsr 缓存命中 {time.time()-_t0:.2f}s", flush=True)
    try:
        with open(dest, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    entries = oa.harness_tsr_to_entries(data, task_obs=task_obs)
    print(f"    [tsr-cache] {leaf}: 解析出 {len(entries)} 条 entry", flush=True)
    return entries


def _backfill_finished_tasks(inst: dict, origin: str, obsutil: str) -> int:
    """finished/stopped 实例浅层补全：OBS 轨迹探测 → task_records 缺失行 INSERT + 分级 + task_traj_records。

    背景：在线 _sync_task_records 只跑 running 实例且按增量变化驱动，多 task 并发终止时
    可能漏写部分行；worker 浅层只消费 task_records 已有行。结果 finished/stopped 实例的漏斗/列
    表缺行、task_traj_records 为空 → 点会话 404「无轨迹记录」。

    做法：一次 obsutil ls -d 枚举 obs_base 下全部 task 叶子目录（OBS 侧 1 次往返），
    与 task_records 中 traj_level IS NULL 的缺口行做集合差（leaf == config stem）：
      - 命中：拉 <1KB 的 logs/traj_stats_result.json 快路径分级（compute_level）。
      - 未命中/无 tsr：failed 占位（不可分级，退出缺口队列，不再每轮重扫）。
    INSERT 缺失行并 UPSERT task_traj_records（status='done'，带轨迹路径）。

    代替旧「首目录探测 + 逐行 _obs_dir_exists + 连续 miss 整批占位」：逐行探测在大
    缺口实例（数千行）下每行一次 OBS 往返，会卡死整轮；一次 ls -d 拿整批目录集合，
    逐行只对命中的行拉 tsr（这才是需要 OBS 往返的部分）。

    约束（模块 docstring ⚠）：
      - 不触碰 task_records.updated_at——INSERT 新行必须写 updated_at（否则行无写入时间），
        但 ON CONFLICT 分支不更新它（已有行保持在线写入时间）。
      - 不做 stale 回收——浅层由 task_traj_records.shallow_status 驱动且只做一次，
        无轨迹的缺口行以 failed 占位（shallow_status="empty"）退出队列，避免每轮重扫。
    幂等：traj_level 非 NULL 的行跳过；重复调用只补新增缺口。缺口行在此轮要么分级、
    要么 failed 占位——不会残留 NULL 缺口（对无轨迹的故障行退出队列，避免每轮重扫）。
    """
    obs_base = _make_obs_base_for(inst)
    with get_connection() as conn:
        gaps = [dict(r) for r in conn.execute(
            "SELECT config_name, task_idx, status, error_code "
            "FROM task_records WHERE instance_id=? AND traj_level IS NULL "
            "ORDER BY task_idx LIMIT ?",
            (inst["id"], BACKFILL_MAX_TASKS_PER_INSTANCE),
        ).fetchall()]
    if not gaps:
        return 0
    print(f"    [backfill] {inst['id']}: 缺口 {len(gaps)} 行，枚举 OBS 叶子目录", flush=True)

    # 首目录探测：区分"轨迹整批在 OBS"（在线竞态漏写，逐行拉 tsr 分级）和"轨迹已失效"
    # （历史死数据，逐行探测连续未命中后整批占位）。不用天数猜测：8/18 大批量实例缺口
    # 数千行，逐行拉 tsr 会让每轮卡死分钟级。注意混合实例（同一实例部分 config 上传、
    # 部分未上传）也存在——所以首目录未命中时不整批占位，而是逐行 ls 探测，命中才拉 tsr。
    # 一次 obsutil ls -d 枚举 obs_base 全部 task 叶子目录，与缺口行做集合差：
    # 命中 → 拉 tsr 分级；未命中 → failed 占位（不可分级，退出缺口队列）。
    # 替代旧的「首目录探测 + 逐行 _obs_dir_exists + miss 计数整批占位」，
    # 把 N 次 OBS 往返降为 1 次（OBS 侧一次拿到整批目录集合）。
    obs_leaves = _list_obs_leaves(obsutil, obs_base)

    # 任务异常/失败的缺口行：轨迹几乎不存在（finished 场景抽查 100% 无 OBS 目录），批量
    # failed 占位退出缺口队列（避免每轮逐行探测 OBS）。已分级/占位的行不再进入缺口查询。
    # 例外：stopped 实例（如中途中止）轨迹往往已部分上传，任务异常 ≠ 无轨迹——仍逐行探测
    # OBS（见下 _backfill_one 的 obs_leaves 集合差），避免把有真实轨迹的会话误标 failed。
    if inst["status"] != "stopped":
        failed_rows = [g for g in gaps if g["status"] == "任务异常"]
        if failed_rows:
            _batch_placeholder_failed(inst, failed_rows, "任务异常缺口批量占位")

    # 任务成功/未知/stopped 的缺口行：轨迹可能在 OBS（在线竞态漏写分级），逐行拉 tsr 分级。
    # OBS 集合里没有的 → 轨迹整批已失效/未上传，直接 failed 占位（不再逐行探测）。
    # 行级并发：OBS 拉 tsr 是 I/O 密集（subprocess cp），串行逐行等下载极慢；用线程池并行。
    # obs_leaves 只读、DB 写各用独立 get_connection()（WAL）、obsutil 独立子进程——线程安全。
    def _backfill_one(g: dict) -> int:
        config_name = g["config_name"]
        leaf = config_name[:-5] if config_name.endswith(".json") else config_name
        task_obs = obs_base.rstrip("/") + "/" + leaf + "/"
        # meta = {task_idx, status, code}：缺口行已在 task_records，直接用它；仅当
        # status 未知（NULL）才读单日志补齐。
        meta = {"task_idx": g["task_idx"], "status": g["status"], "code": g["error_code"]}
        if meta["status"] is None:
            meta = _read_task_log_for(inst, meta["task_idx"] or 0)
        try:
            if leaf not in obs_leaves:
                # OBS 无此轨迹目录：failed 占位（不可分级，退出缺口队列）。
                _insert_task_record_placeholder(inst, config_name, meta)
                _upsert_traj_record(inst, {"config_name": config_name},
                                    meta.get("task_idx"), config_name,
                                    "failed", "openclaw", {}, None,
                                    traj_name=leaf, shallow_status="empty",
                                    shallow_error="OBS 无轨迹目录")
                return 0
            # 补全专用快路径：只拉 tsr（约 1KB），不下载主 log / 不慢路径下载整轨迹。
            entries = _fetch_tsr_entries_fast(obsutil, task_obs, origin)
            if not entries:
                # 无有效 tsr：不慢路径下载（补全不能因大轨迹卡死整轮）。写占位行：
                #   task_records: status 取日志、traj_level='failed'（不可分级，不进漏斗）
                #   task_traj_records: status='done'、无路径 → 点会话时 trigger_deep 置
                #     pending → 深层按需下载解析（否则 trigger_deep 404「无轨迹记录」）
                _insert_task_record_placeholder(inst, config_name, meta)
                _upsert_traj_record(inst, {"config_name": config_name},
                                    meta.get("task_idx"), config_name,
                                    "failed", "openclaw", {}, None,
                                    traj_name=leaf, shallow_status="error",
                                    shallow_error="OBS 有目录但无有效 tsr")
                return 0
            entry = entries[0]
            level = oa.compute_level(entry.get("harness", "openclaw"),
                                     entry.get("tool_calls", 0),
                                     entry.get("plain_rounds", 0),
                                     entry.get("evaluator_completion"))
            _insert_task_record_placeholder(inst, config_name, meta, level=level)
            for e in entries:
                # entry 不含 traj_name/config_name 列（只有 task=leaf），显式传 leaf
                leaf2 = os.path.basename(str(e.get("task") or "")).rstrip("/") or leaf
                _upsert_traj_record(inst, {"config_name": config_name},
                                    meta.get("task_idx"), config_name,
                                    level, e.get("harness", "openclaw"),
                                    e, e.get("trajectory"), traj_name=leaf2,
                                    shallow_status="processed")
            return 1
        except Exception as e:
            print(f"    [fail] {inst['id']} 补全 {config_name}: {e}", flush=True)
            return 0

    # 跳过已批量占位的"任务异常"行，其余进线程池；stopped 例外：任务异常行未批占，全量探测。
    work = ([g for g in gaps if g["status"] != "任务异常"]
            if inst["status"] != "stopped" else gaps)
    n = 0
    if work:
        with ThreadPoolExecutor(max_workers=BACKFILL_CONCURRENCY,
                                thread_name_prefix=f"backfill-{inst['id'][:8]}") as pool:
            n = sum(fut.result() for fut in as_completed(
                {pool.submit(_backfill_one, g): g for g in work}))
    return n


def _batch_placeholder_failed(inst: dict, gaps: list[dict], why: str) -> None:
    """把 gaps 中仍为 NULL 的缺口行批量 failed 占位（task_records + task_traj_records）。"""
    with get_connection() as conn:
        conn.executemany(
            "UPDATE task_records SET traj_level='failed' "
            "WHERE instance_id=? AND config_name=? AND traj_level IS NULL",
            [(inst["id"], g["config_name"]) for g in gaps],
        )
        conn.executemany(
            """INSERT INTO task_traj_records
                   (instance_id, task_idx, config_name, traj_name, level, status,
                    shallow_status, updated_at)
               VALUES (?, ?, ?, ?, 'failed', 'done', 'empty', CURRENT_TIMESTAMP)
               ON CONFLICT(instance_id, config_name, traj_name) DO NOTHING""",
            [(inst["id"], g["task_idx"], g["config_name"],
              g["config_name"][:-5] if g["config_name"].endswith(".json") else g["config_name"])
             for g in gaps],
        )
    if why:
        print(f"    [backfill] {inst['id']} {why} {len(gaps)} 行", flush=True)


def _obs_dir_exists(obsutil: str, task_obs: str) -> bool:
    """探测 OBS 轨迹目录是否存在（obsutil ls 单目录一次往返）。"""
    task_obs = task_obs if task_obs.endswith("/") else task_obs + "/"
    cmd = [obsutil, "ls", task_obs, "-d", "-limit", "5"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30)
    except Exception:
        return False
    if res.returncode != 0:
        return False
    # obsutil ls 失败/空目录时 stdout 有提示行；有以 task_obs 开头的行即存在
    for raw in (res.stdout or "").splitlines():
        if raw.strip().startswith(task_obs):
            return True
    return False


def _list_obs_leaves(obsutil: str, obs_base: str) -> set[str]:
    """一次 obsutil ls -d 枚举 obs_base 下全部叶子目录名。

    替代逐行 _obs_dir_exists 探测：OBS 侧一次往返拿到整批 task 目录集合，与
    platform.db 的 config_name 缺口行做集合差，命中才拉 tsr。obs_base 层可能
    混杂非 task 对象（如 complete.jsonl / failed.jsonl），只保留目录（排除带
    扩展名的文件对象）。注意 task 目录不一定是 task-* 前缀——真实布局混用业务
    前缀（如 MKT-*），故按「无扩展名目录」而非「task- 前缀」过滤。
    """
    base = obs_base.rstrip("/") + "/"
    _t0 = time.time()
    try:
        lines = oa.run_obsutil_ls(obsutil, base, one_level=True)
    except Exception as e:
        print(f"    [backfill] obs 枚举失败: {e}", flush=True)
        return set()
    leaves = set()
    for ln in lines:
        ln = ln.strip()
        if ln.startswith(base):
            rest = ln[len(base):].rstrip("/")
            # 只留目录：排除带扩展名的文件对象（complete.jsonl/failed.jsonl 等）
            if rest and "/" not in rest and "." not in rest.rsplit("/", 1)[-1]:
                leaves.add(rest)
    print(f"    [backfill] OBS 枚举 {len(lines)} 行 → {len(leaves)} 个 task 目录 "
          f"{time.time()-_t0:.2f}s", flush=True)
    return leaves


def _insert_task_record_placeholder(inst: dict, config_name: str, meta: dict,
                                    level: str | None = "failed") -> None:
    """INSERT 补全的 task_records 行（已有行只更新状态/分级，不动其 updated_at）。

    level 为 None 时写 NULL（无 tsr 场景，列表显示但无分级色，点会话走深层补）；
    默认 'failed'（不可分级占位）。status/error 取任务日志 meta，缺省 '任务异常'。
    """
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO task_records
                (instance_id, task_idx, config_name, status, error_code,
                 error_category, traj_level, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(instance_id, config_name) DO UPDATE SET
                task_idx       = excluded.task_idx,
                status         = excluded.status,
                error_code     = excluded.error_code,
                error_category = excluded.error_category,
                traj_level     = excluded.traj_level
            """,
            (inst["id"], meta.get("task_idx"), config_name,
             meta.get("status", "任务异常"), meta.get("code"),
             (meta.get("code") or "")[:1] if meta.get("code") else None,
             level),
        )


def _process_instance(inst: dict, origin: str, obsutil: str) -> dict:
    """处理一个实例的浅层队列：finished/stopped 从 OBS 轨迹补全（快路径，不慢路径下载）；
    running/preparing 分级 task_records 未回填行（仅一次，不做 stale 回收）。"""
    if inst["status"] in ("finished", "stopped"):
        # finished/stopped：实例已结束轨迹不变，补全从 OBS 轨迹快路径补齐在线竞态漏写的行。
        # 补全已覆盖全部 OBS 轨迹（有 tsr 分级 / 无 tsr 占位），直接 return，不落通用 pending
        # 逻辑——通用分支对无 tsr 行会走 _load_per_task_entries 慢路径下载整轨迹。
        n_backfilled = _backfill_finished_tasks(inst, origin, obsutil)
        return {"n_processed": n_backfilled, "n_failed": 0}

    rows = _level_rows_for_instance(inst)
    # 驱动源：尚未做过浅层分析（shallow_status IS NULL）的行。若已分析过（processed/empty/error）
    # 直接跳过，不重复处理。traj_level IS NULL（未分级）或 ='failed'（可能被在线同步误标，
    # 而该行从未被 worker 浅层分析过）都需重新过一遍 OBS 定级；真实定级过的行（L0-L3）
    # 其 shallow_status 已为 processed，不再进入。
    pending = [r for r in rows if not r["shallow_status"]
               and (not r["traj_level"] or r["traj_level"] == "failed")]
    if not pending:
        return {"n_processed": 0, "n_failed": 0}

    obs_base = _make_obs_base_for(inst)
    # 已按 task_idx 排序；用 OBS 目录枚举校对 config_name ↔ traj_name（config stem 可能 ≠ task 目录名）
    tnames = {}
    try:
        for t in oa.list_task_dirs(obsutil, obs_base):
            tnames[t.rstrip("/").rsplit("/", 1)[-1]] = t
    except Exception as e:
        print(f"    [warn] {inst['id']} 枚举 OBS task 目录失败: {e}", flush=True)
    is_running = inst["status"] == "running"

    def _grade_one_task(r: dict) -> tuple[int, int, str]:
        """处理一个 task 行：OBS 下载/解析（I/O 密集）+ 写库。返回 (ok|fail, 0, config_name)。

        供 ThreadPoolExecutor 并行调用——OBS 下载走独立 subprocess、DB 写用独立
        get_connection()（WAL），均线程安全。返回 (count, 0, name) 或 (0, 1, name)。
        shallow_status 落 task_traj_records：处理一次后不再重复（OBS 未命中时 running
        保持 NULL 下轮重试，其他状态标 empty 一次到位）。
        """
        config_name = r["config_name"]
        stem = os.path.splitext(config_name)[0]
        leaf = stem
        # 确定 task OBS 目录：优先按 traj 名（OBS 侧 leaf == config stem 的概率高）
        task_obs = None
        if stem in tnames:
            task_obs = tnames[stem]
        elif config_name in tnames:
            task_obs = tnames[config_name]
        if not task_obs:
            # OBS 侧无对应 task 目录。running 实例轨迹可能尚未上传 → 保持 NULL 下轮重试；
            # 其他状态（preparing 终态/stopped）轨迹已失效 → 标 empty 一次到位，不反复重扫。
            if not is_running:
                _apply_level_to_task_records(inst, config_name, "failed")
                _upsert_traj_record(inst, {"config_name": config_name},
                                    r.get("task_idx"), config_name,
                                    "failed", "openclaw", {}, None,
                                    traj_name=leaf, shallow_status="empty",
                                    shallow_error="OBS 无轨迹目录")
            return (0, 0, config_name)
        try:
            entries = oa._load_per_task_entries(obsutil, task_obs, origin)
            if not entries:
                _apply_level_to_task_records(inst, config_name, "failed")
                _upsert_traj_record(inst, {"config_name": config_name},
                                    r.get("task_idx"), config_name,
                                    "failed", "openclaw", {}, None,
                                    traj_name=leaf, shallow_status="error",
                                    shallow_error="OBS 有目录但无有效 tsr")
                return (0, 1, config_name)
            # 每 task 通常只有 1 条 entry（assistant 主轨迹）；多条时逐条落，等级按第一条
            entry = entries[0]
            level = oa.compute_level(entry.get("harness", "openclaw"),
                                     entry.get("tool_calls", 0),
                                     entry.get("plain_rounds", 0),
                                     entry.get("evaluator_completion"))
            # task_records 与 task_traj_records 同步落（task_records 的 traj_level 仍可能为 NULL，
            # 因在线 _sync_task_records 用 COALESCE 保留旧值，worker 是唯一写 traj_level 的地方）
            _apply_level_to_task_records(inst, config_name, level, entry)
            for e in entries:
                # entry 不含 traj_name/config_name 列（只有 task=leaf），显式传入 leaf
                leaf2 = os.path.basename(str(e.get("task") or "")).rstrip("/") or stem
                _upsert_traj_record(inst, e, r.get("task_idx"), config_name,
                                    level, e.get("harness", "openclaw"),
                                    e, e.get("trajectory"), traj_name=leaf2,
                                    shallow_status="processed")
            return (1, 0, config_name)
        except Exception as e:
            _upsert_traj_record(inst, {"config_name": config_name},
                                r.get("task_idx"), config_name,
                                "failed", "openclaw", {}, None,
                                traj_name=leaf, shallow_status="error",
                                shallow_error=str(e)[:300])
            print(f"    [fail] {inst['id']} {config_name}: {e}", flush=True)
            return (0, 1, config_name)

    # 行级并行：OBS 下载是 I/O 密集，串行逐行等下载极慢；用线程池并行加速。
    # tnames 只读、DB 各写独立连接、obsutil 独立子进程，线程安全。并发度受 SHALLOW_CONCURRENCY 限制。
    n_ok = n_fail = 0
    with ThreadPoolExecutor(max_workers=SHALLOW_CONCURRENCY,
                            thread_name_prefix=f"shallow-{inst['id'][:8]}") as pool:
        futures = {pool.submit(_grade_one_task, r): r for r in pending}
        for fut in as_completed(futures):
            ok, fail, name = fut.result()
            n_ok += ok
            n_fail += fail
    return {"n_processed": n_ok, "n_failed": n_fail}


# ============ 深层：pending → 下载/解析 → 回填路径列 ============

def _short_traj(traj_name: str) -> str:
    """日志用短名：取最后一段（与 _consume_deep 内层 traj_name 归一化保持一致）。"""
    return traj_name.rstrip("/").rsplit("/", 1)[-1]


def _consume_deep(origin: str, obsutil: str, instance_id: str | None = None) -> dict:
    """消费全部 pending 行。返回 {n_done, n_failed}。

    行级并发：OBS 下载/解析是 I/O 密集（subprocess cp / ls），串行逐行等下载极慢，
    用线程池并行。DB 操作全部在主线程（连接非线程安全）——pending 读取、downloading
    标记、结果批量回填都在主线程；线程池只做下载 + 解析返回结果。

    downloading 标记在主线程一次性批量执行（防多 worker 重复消费），线程池拿到的是
    已标记的行，下载完回填。若某行标记失败（已被别的 worker 抢先标记），该行跳过，
    不做本线程消费。
    """
    with get_connection() as conn:
        rows = _pick_pending_rows(conn, instance_id)
        inst_cache: dict[str, dict] = {}

        n_done = n_fail = 0
        n_submitted = len(rows)
        target = f"instance {instance_id}" if instance_id else "全局"
        # 结果批量回填：成功/失败攒到线程池收尾统一 executemany + 一次 commit。
        batch_done: list[tuple] = []   # (assistant, eval, log, gw, ev, id)
        batch_failed: list[tuple] = []  # (error, id)
        if n_submitted:
            print(f"    [deep] {target}: 提交 {n_submitted} 条待下载", flush=True)

        # 主线程阶段 1：跳过实例已删的行；其余批量标记 downloading（原子防重复消费）。
        # 标记只对 status='pending' 生效——已被别的 worker 抢先标记的行标记失败即跳过。
        work: list[tuple[dict, dict]] = []  # (tr, inst)
        deleted_ids: list[int] = []
        for tr in rows:
            traj_name = _short_traj(tr.get("traj_name") or tr.get("config_name") or "")
            inst = inst_cache.get(tr["instance_id"])
            if inst is None:
                inst = _instance_by_id(conn, tr["instance_id"])
                if inst:
                    inst_cache[tr["instance_id"]] = inst
            if not inst:
                # 实例已删：直接收尾，避免永久残留 pending
                deleted_ids.append(tr["id"])
                print(f"    [deep] {traj_name}: 跳过(实例已删)", flush=True)
                n_fail += 1
                continue
            # 标记 downloading（防多 worker 重复消费；原子：仅 pending→downloading）
            cur = conn.execute(
                "UPDATE task_traj_records SET status='downloading', updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='pending'",
                (tr["id"],),
            )
            if cur.rowcount == 0:
                # 已被别的 worker/上一轮抢占：跳过本行，避免重复下载
                print(f"    [deep] {traj_name}: 跳过(已被抢占)", flush=True)
                continue
            work.append((tr, inst))
        if deleted_ids:
            conn.executemany(
                "UPDATE task_traj_records SET status='failed', error='实例不存在', "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                [(i,) for i in deleted_ids],
            )
        if work:
            conn.commit()  # 标记落盘后线程池才开跑（防重复消费窗口）

        if work:
            # 线程池阶段 2：只做 OBS 下载/解析（I/O 密集），返回 (ok行成功元组|None, err, tr)。
            def _download_one(tr: dict, inst: dict) -> tuple:
                traj_name = _short_traj(tr.get("traj_name") or tr.get("config_name") or "")
                t_dl = time.time()
                print(f"    [deep] {traj_name}: 开始下载", flush=True)
                try:
                    obs_base = _make_obs_base_for(inst)
                    task_obs = obs_base.rstrip("/") + "/" + traj_name + "/"
                    # 预检 OBS 目录存在性：不存在（轨迹已清理/路径不符）直接判 failed，
                    # 避免永久 pending/重试
                    listing = oa.run_obsutil_ls(obsutil, task_obs, one_level=False)
                    if not listing:
                        raise FileNotFoundError(
                            f"OBS 目录不存在或无对象: {task_obs}")
                    task_dir = oa.download_task_detail(obsutil, task_obs, origin)
                    detail = oa.load_task_detail(task_dir, traj_name)

                    traj_paths = {}
                    for t in detail.get("trajectories") or []:
                        role = t.get("role")
                        if role == "assistant" and not traj_paths.get("assistant"):
                            traj_paths["assistant"] = t["path"]
                        elif role == "evaluator" and not traj_paths.get("evaluator"):
                            traj_paths["evaluator"] = t["path"]
                    log_path = (detail.get("log") or {}).get("path")
                    gw_path = (detail.get("gateway") or {}).get("path")
                    ev_path = (detail.get("eval_use_log") or {}).get("path")

                    ap = os.path.basename(traj_paths.get("assistant") or "") or "无assistant轨迹"
                    print(f"    [deep] {traj_name}: 完成 {time.time()-t_dl:.1f}s "
                          f"assistant={ap}", flush=True)
                    # 成功元组
                    return ("ok", (traj_paths.get("assistant"), traj_paths.get("evaluator"),
                                   log_path, gw_path, ev_path, tr["id"]), None)
                except Exception as e:
                    print(f"    [deep] {traj_name}: 失败 - {str(e)[:200]}", flush=True)
                    return ("fail", None, (str(e)[:500], tr["id"]))

            with ThreadPoolExecutor(max_workers=DEEP_CONCURRENCY,
                                    thread_name_prefix="deep") as pool:
                futures = {pool.submit(_download_one, tr, inst): tr
                           for tr, inst in work}
                for fut in as_completed(futures):
                    outcome, ok, err = fut.result()
                    if outcome == "ok":
                        batch_done.append(ok)
                        n_done += 1
                    else:
                        batch_failed.append(err)
                        n_fail += 1

        # 主线程阶段 3：批量回填 + commit。
        with get_connection() as conn:
            if batch_done:
                conn.executemany(
                    "UPDATE task_traj_records SET "
                    "status='done', error=NULL, updated_at=CURRENT_TIMESTAMP, "
                    "assistant_traj_path=?, evaluator_traj_path=?, "
                    "task_log_path=?, gateway_log_path=?, eval_log_path=? "
                    "WHERE id=?",
                    batch_done,
                )
            if batch_failed:
                conn.executemany(
                    "UPDATE task_traj_records SET status='failed', error=?, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    batch_failed,
                )
            if batch_done or batch_failed:
                conn.commit()
        if n_submitted:
            print(f"    [deep] {target}: 本轮完成 提交={n_submitted} "
                  f"done={n_done} failed={n_fail}", flush=True)
        return {"n_done": n_done, "n_failed": n_fail}


# ============ 主循环 ============

def _run_once(obsutil: str, origin: str,
              instance_id: str | None, deep_only: bool, shallow_only: bool = False) -> dict:
    report: dict = {"shallow": {"n_processed": 0, "n_failed": 0},
                    "deep": {"n_done": 0, "n_failed": 0}}

    # 浅层/深层互斥（解耦成两个进程）：--shallow-only 只浅层，--deep-only 只深层，
    # 两者都不加 = 混合（向后兼容，两者都跑）。深层只读 task_traj_records.status='pending'，
    # 浅层只写 task_records.traj_level / task_traj_records 分级列，二者通过 DB 状态机协作。
    if not deep_only:
        with get_connection() as conn:
            insts = _pick_running_instances(conn)
        n_backfilled_inst = 0
        for inst in insts:
            if instance_id and inst["id"] != instance_id:
                continue
            # 每轮预算：finished/stopped 补全（_backfill_finished_tasks 枚举 OBS）有配额，
            # 跑满后本轮的 finished/stopped 全部跳过（running/preparing 不受限）。
            if (inst["status"] in ("finished", "stopped")
                    and n_backfilled_inst >= BACKFILL_MAX_INSTANCES_PER_ROUND):
                continue
            try:
                r = _process_instance(inst, origin, obsutil)
            except Exception as e:
                r = {"n_processed": 0, "n_failed": 0}
                print(f"    [fail] 实例 {inst['id']} 分级失败: {e}", flush=True)
            if inst["status"] in ("finished", "completed", "stopped"):
                n_backfilled_inst += 1
                # 点击驱动：registered 实例处理完（无剩余缺口）即删登记出队；
                # completed 同时置 output_status='done'（浅层完成，点击输出下次直接用）。
                # 仍有缺口（一轮限 300 行没清完）保留登记，下轮继续。
                with get_connection() as conn:
                    if not _has_shallow_gaps(conn, inst["id"]):
                        _clear_shallow_request(conn, inst["id"])
                        if inst["status"] == "completed":
                            conn.execute(
                                "UPDATE task_instances SET output_status='done' "
                                "WHERE id=?", (inst["id"],))
            for k in ("n_processed", "n_failed"):
                report["shallow"][k] += r[k]
            if r["n_processed"] or r["n_failed"]:
                print(f"  [shallow] {inst['id']} 新分级={r['n_processed']} "
                      f"失败={r['n_failed']}", flush=True)

    # 深层：--shallow-only 时不跑（避免与独立深层 worker 重复消费）；混合与 --deep-only 都跑。
    if not shallow_only:
        d = _consume_deep(origin, obsutil, instance_id)
        report["deep"] = d
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="输出分析 worker（独立进程，分级 + 明细回填 platform.db）")
    ap.add_argument("--once", action="store_true", help="扫一遍即退出（调试/CI）")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help=f"常驻轮询间隔秒（默认 {DEFAULT_INTERVAL}）")
    ap.add_argument("--instance", default=None, help="只处理指定 instance_id（调试）")
    ap.add_argument("--deep-only", action="store_true", help="只消费深层 pending 队列")
    ap.add_argument("--shallow-only", action="store_true", help="只跑浅层分级/补全（不做深层下载）")
    ap.add_argument("--cache-dir", dest="origin", default=oa.DEFAULT_OUTPUT_CACHE,
                    help=f"本地缓存根（默认 {oa.DEFAULT_OUTPUT_CACHE}）")
    ap.add_argument("--obsutil", default=oa.DEFAULT_OBSUTIL)
    ap.add_argument("--stale-after", type=int, default=STALE_AFTER_SECONDS,
                    help="(已废弃) 浅层只做一次，不再按此阈值 stale 重算")
    a = ap.parse_args()

    origin = os.path.abspath(a.origin)
    os.makedirs(origin, exist_ok=True)

    if a.deep_only:
        print(f"[worker] 只消费深层队列: origin={origin}", flush=True)
    elif a.shallow_only:
        print(f"[worker] 只跑浅层分级/补全: origin={origin}", flush=True)
    else:
        print(f"[worker] 输出分析 worker 启动: interval={a.interval}s origin={origin}", flush=True)

    while True:
        t0 = time.time()
        try:
            rep = _run_once(a.obsutil, origin, a.instance, a.deep_only, a.shallow_only)
            if rep["shallow"]["n_processed"] or rep["shallow"]["n_failed"] \
                    or rep["deep"]["n_done"] or rep["deep"]["n_failed"]:
                print(f"[worker] 本轮: 浅层(新={rep['shallow']['n_processed']} "
                      f"fail={rep['shallow']['n_failed']}) | "
                      f"深层(done={rep['deep']['n_done']} fail={rep['deep']['n_failed']}) | "
                      f"{time.time()-t0:.1f}s", flush=True)
        except KeyboardInterrupt:
            print("[worker] 收到中断，退出", flush=True)
            return
        except Exception as e:
            print(f"[worker] 本轮异常: {e}", flush=True)
        if a.once:
            break
        time.sleep(max(1, a.interval))


if __name__ == "__main__":
    main()
