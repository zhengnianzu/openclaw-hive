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
  产物下载（沙箱交付物）：
    按实例 config 列表建 task_traj_records 行（level 允许 NULL），用本地 complete.jsonl
    标记 artifact_complete；对已完成的 task 全量 `obsutil cp -r` 整个 OBS 前缀到
    output_cache/<batch>/<leaf>/，artifacts_status: queued → downloading → done/failed/skipped。
    **会话详情直接读这个目录**，不再有独立的「深层轨迹/日志按需下载」——
    两者的落盘目录本来就相同（_cache_subdir_for），全量下载已覆盖 deep 要的全部文件。

⚠ 关键设计约束：worker 写 task_records 的 UPDATE 不得触碰 updated_at。
task_records.updated_at 语义 =「在线 _sync_task_records 最后写入时间」（instances.py:1331 每次 UPSERT
都 CURRENT_TIMESTAMP），是前端/在线同步判断行新鲜度的依据。若 worker 分级时也刷 updated_at，
会把行「更新」得看似在线刚写入，干扰该语义。故：
  - 分级回填：UPDATE ... SET traj_level=?  WHERE instance_id=? AND config_name=?（不写 updated_at）
  - 浅层处理状态写 task_traj_records.shallow_status（不依赖 updated_at，不做 stale 回算）
  - finished 补全 INSERT 新行必须写 updated_at（否则行无写入时间），但 ON CONFLICT
    分支不更新已有行的 updated_at。

用法:
  python offline/output_worker.py --once                     # 扫一遍浅层 + 产物后退出（调试/CI）
  python offline/output_worker.py --interval 60              # 常驻，每 60s 一轮（默认）
  python offline/output_worker.py --once --deep-only         # 只跑产物下载队列
  python offline/output_worker.py --once --artifacts-only    # 同上（显式）
  python offline/output_worker.py --once --instance <id>     # 只处理指定实例（调试）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from src import offline_analysis as oa            # noqa: E402
from api.core.config import settings               # noqa: E402
from api.core.database import (                       # noqa: E402
    get_connection, ensure_traj_extra_columns, ensure_shallow_extra_columns)
# 默认常驻轮询间隔（秒）
DEFAULT_INTERVAL = 60
# （已废弃）分级 stale 阈值：浅层只做一次，不再按此阈值重算。保留仅为 --stale-after 默认值兼容。
STALE_AFTER_SECONDS = 600
# 浅层每轮处理的实例数上限。已结束实例自动入队后，缺口实例可能有数百个
# （实测 400 个实例积压 159 万行），沿用 8 会让队列以「每轮 8 个」的速度爬。
# 提高到一个能在一轮内覆盖大部分活跃实例的量级；真正限速交给下面的
# BACKFILL_CONCURRENCY 和每实例行配额。
BACKFILL_MAX_INSTANCES_PER_ROUND = int(
    os.environ.get("BACKFILL_MAX_INSTANCES_PER_ROUND", "40"))
# 每实例每轮补全缺口配额。原 300 对 3 万行的实例要 100 轮才能清完
# （一轮动辄 2000s+），实际永远追不上。2000 行 × 16 并发 ≈ 25s/实例，
# 一轮 40 个实例仍在可接受范围内。
BACKFILL_MAX_TASKS_PER_INSTANCE = int(
    os.environ.get("BACKFILL_MAX_TASKS_PER_INSTANCE", "2000"))
# 补全循环并发度：OBS 拉 tsr 是 I/O 密集，串行逐行等下载极慢。实测 16 并发 74行/s、
# 24-32 并发 85-90行/s（OBS 在 ~32 以上饱和回落）。默认 16 已达成 200 行 <3s。
BACKFILL_CONCURRENCY = int(os.environ.get("BACKFILL_CONCURRENCY", "16"))
# 浅层分级并发度：同一实例内并行处理多个 task 行（OBS 下载是 I/O 密集，串行太慢）。
# obsutil 走独立 subprocess、DB 写用独立连接（WAL），线程安全。过高会打爆 OBS 带宽。
# 实测 32 是 OBS 侧的饱和点（再高回落），浅层为此单独开一个进程、与产物下载分账，
# 所以可以按饱和点取。
SHALLOW_CONCURRENCY = int(os.environ.get("SHALLOW_CONCURRENCY", "32"))

# ── 沙箱产物全量下载（_consume_artifacts）──
# 参数来自 settings/worker/config.yaml；环境变量可临时覆盖（优先级最高）。
# 这样调并发不用改代码，`./run.sh worker-restart` 即可生效。

def _load_worker_cfg() -> dict:
    """读 settings/worker/config.yaml；缺失/损坏时返回空 dict（全部走内置默认）。"""
    path = os.path.join(PLATFORM_DIR, "settings", "worker", "config.yaml")
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


_WCFG = _load_worker_cfg()


def _cfg(section: str, key: str, default, env: str | None = None):
    """取配置：环境变量 > yaml > 默认。"""
    if env:
        v = os.environ.get(env)
        if v not in (None, ""):
            try:
                return type(default)(v)
            except (TypeError, ValueError):
                pass
    sec = _WCFG.get(section) or {}
    v = sec.get(key)
    if v is None:
        return default
    try:
        return type(default)(v)
    except (TypeError, ValueError):
        return default


ARTIFACT_CONCURRENCY = _cfg("download", "concurrency", 3, "ARTIFACT_CONCURRENCY")
# 以下三项自「发现」迁到 task_tracker 后已无调用方（保留常量是为了
# settings/worker/config.yaml 里的同名键仍能解析，不会因未知键报错）。
ARTIFACT_MAX_TASKS_PER_ROUND = _cfg("download", "max_tasks_per_round", 20,
                                    "ARTIFACT_MAX_TASKS_PER_ROUND")
ARTIFACT_MAX_INSTANCES_PER_ROUND = _cfg("download", "max_instances_per_round", 4,
                                        "ARTIFACT_MAX_INSTANCES_PER_ROUND")
ARTIFACT_AUTO_DISCOVER_DAYS = _cfg("download", "auto_discover_days", 1,
                                   "ARTIFACT_AUTO_DISCOVER_DAYS")
ARTIFACT_MAX_TASK_BYTES = _cfg("download", "max_task_bytes", 2 * 1024 ** 3,
                               "ARTIFACT_MAX_TASK_BYTES")
# 积压 worker 单轮下载预算（秒）。一轮内连续领取直到队列空或超时；
# 0 表示只领一批就返回（老行为）。初值 300s 是因为单 task 中位 1.6s、
# 长尾 85s，给够时间才能把 190 万条队列真正推进，而不是每轮 8 条 + 60s 睡。
ARTIFACT_ROUND_BUDGET_SEC = _cfg("download", "round_budget_seconds", 300,
                                 "ARTIFACT_ROUND_BUDGET_SEC")

# ── 实时下载 worker（--realtime-only）──
# 与积压 worker 彻底隔离：积压 worker 只领 artifact_priority < REALTIME_PRIORITY
# 的行，实时 worker 只领 >= 阈值的行（「下载此任务 / 会话详情下载」置 100）。
# 注意用的是 **artifact_priority** 而不是 priority：后者是浅层「用户点了输出、
# 整实例插队」的语义，一个实例被点过它的全部 config 都是 100，拿它分车道会让
# 上千行涌进实时车道、真正的单任务点击反而排在后面。
# 这样用户点「下载此任务」不会被 4 万条积压拖住 —— 实测积压 worker 一轮要
# 3~40 秒，即使插队也要等当前轮跑完；实时 worker 用 2 秒间隔独立轮询。
ARTIFACT_REALTIME_PRIORITY = _cfg("realtime", "priority_threshold", 100,
                                  "ARTIFACT_REALTIME_PRIORITY")
ARTIFACT_REALTIME_INTERVAL = _cfg("realtime", "interval_seconds", 2,
                                  "ARTIFACT_REALTIME_INTERVAL")
ARTIFACT_REALTIME_CONCURRENCY = _cfg("realtime", "concurrency", 4,
                                     "ARTIFACT_REALTIME_CONCURRENCY")
ARTIFACT_DOWNLOADING_TIMEOUT_MIN = _cfg("download", "downloading_timeout_minutes", 30,
                                        "ARTIFACT_DOWNLOADING_TIMEOUT_MIN")
ARTIFACT_FAIL_RETRY_SECONDS = _cfg("download", "fail_retry_seconds", 600,
                                   "ARTIFACT_FAIL_RETRY_SECONDS")
# 最多尝试次数（含首次）。超过就判死 skipped，不再排队、不再回收。
# 之前没有上限，超时回收→排回 queued→立刻再领→再超时，死循环（实测 168 次）。
ARTIFACT_MAX_ATTEMPTS = _cfg("download", "max_attempts", 3, "ARTIFACT_MAX_ATTEMPTS")
HEARTBEAT_KEEP_ROWS = _cfg("worker", "heartbeat_keep_rows", 2000,
                           "HEARTBEAT_KEEP_ROWS")
WORKER_INTERVAL = _cfg("worker", "interval_seconds", DEFAULT_INTERVAL,
                       "WORKER_INTERVAL")


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
    """浅层队列来源：**全部有缺口的实例**，按最近活动时间排序。

    优先级（都是「越新越先」）：
      1. running / preparing —— 正在产出，实时分级，用户此刻就在看；
      2. 已结束（finished/completed/stopped）—— 按 COALESCE(stopped_at, created_at) 倒序，
         最近结束的先补。stopped 实例里往往还有大量没跑完的任务，之前只靠用户点开详情页
         登记才入队（shallow_requests 表），结果 94 个 stopped 实例积压 75.9 万行、
         队列一条都取不到。现在自动入队。
      3. 用户点击登记的（shallow_requests）—— 置顶插队，保证「点完几秒内看到结果」。

    历史包袱：这里**不按天数截断**。目标是最终把全部缺口消化掉；用时间排序而不是
    一刀切时间窗，既保证新实例优先，又不会把老实例永久排除在外。

    stopped_at 是本地时间（UTC+8）而 SQLite datetime('now') 是 UTC，所以排序只在
    同一列内比，不做跨时区跟 now 的比较——只排序、不筛选，这个坑就不存在了。
    """
    rows = conn.execute(
        "SELECT ti.id, ti.name, ti.config_path, ti.status, ti.config_snapshot "
        "FROM task_instances ti "
        # 只取真有缺口的实例：没有缺口的不碰 OBS（_has_shallow_gaps 同款判据，
        # 走 idx_task_records_level 索引，零成本）。
        "WHERE EXISTS ("
        "    SELECT 1 FROM task_records tr "
        "    WHERE tr.instance_id = ti.id AND tr.traj_level IS NULL) "
        "ORDER BY "
        # 1) 点击登记的置顶
        "  CASE WHEN EXISTS (SELECT 1 FROM shallow_requests sr "
        "                     WHERE sr.instance_id = ti.id) THEN 0 ELSE 1 END, "
        # 2) 运行中的优先（此刻正在产出）
        "  CASE WHEN ti.status IN ('running','preparing') THEN 0 ELSE 1 END, "
        # 3) 其余按最近活动时间倒序
        "  COALESCE(ti.stopped_at, ti.created_at) DESC, "
        "  ti.created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def _has_shallow_gaps(conn, instance_id: str) -> bool:
    """该实例是否仍有浅层缺口（traj_level IS NULL 行）。用作是否删除登记/出队的判据。"""
    return conn.execute(
        "SELECT 1 FROM task_records WHERE instance_id=? AND traj_level IS NULL LIMIT 1",
        (instance_id,),
    ).fetchone() is not None


def _clear_shallow_request(conn, instance_id: str) -> None:
    """实例浅层处理完（无缺口）后删除登记，出队。幂等：无登记时静默。

    同时回收该实例的两类残留 —— 这是 92.6 万条孤儿产物行的产生机制：
      1. 仍 queued 的**背景**产物行（artifact_priority<100）置 pending_click。
         实例已经出队、没人在看它了，不该继续占用下载配额和磁盘。
         用户点击入队的行（>=100）保留 —— 那是显式要求，必须下完。
      2. tracker_state 的 offset 记录。该实例不再被 tracker 跟踪，留着只是垃圾
         （线上实测 321 行从未清理过），且下次重新点击时 pick_clicked 会
         _reset_offsets 清零重扫，不依赖这些行。
    """
    conn.execute("DELETE FROM shallow_requests WHERE instance_id=?", (instance_id,))
    conn.execute(
        "UPDATE shallow_queue SET artifact_state='pending_click', "
        "  artifact_note='实例已出队，背景产物行回收' "
        "WHERE instance_id=? AND artifact_state='queued' AND artifact_priority < ?",
        (instance_id, ARTIFACT_REALTIME_PRIORITY),
    )
    conn.execute("DELETE FROM tracker_state WHERE instance_id=?", (instance_id,))


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
        "SELECT id, name, config_path, config_snapshot, status FROM task_instances WHERE id=?",
        (instance_id,)
    ).fetchone()
    return dict(row) if row else None


def _make_obs_base_for(inst: dict) -> str:
    """取 obs_base：优先 DB config_snapshot（不依赖 /mnt 挂载），回退磁盘 config.yaml。

    优先 snapshot 的原因同 _instance_harness_type：它是建实例时固化的 config 全文
    （run_config.obs.traj_save_bucket + traj_save_path），实例迁移或 config.yaml 被
    清理后仍能定位 OBS 根——否则 logs-only 任务（无轨迹，只有日志）的深层下载会
    因配置文件缺失而整体失败，连日志都拿不到。
    """
    from src import offline_analysis as oa
    base = oa.obs_base_from_snapshot(inst.get("config_snapshot"))
    if base:
        return base
    return _obs_base_path(inst["config_path"])


def _instance_harness_type(inst: dict) -> str:
    """取实例 harness 类型：优先 DB config_snapshot，其次磁盘 config.yaml，最后回退 'openclaw'。

    优先 config_snapshot 的原因：它是建实例时固化的 config 全文（run_config.harness_type），
    不依赖 /mnt/sfs_turbo 挂载；实例迁移/文件缺失时 DB 记录仍在，类型仍可追溯。
    """
    snap = inst.get("config_snapshot")
    if snap:
        try:
            import yaml
            ht = (yaml.safe_load(snap) or {}).get("run_config", {}).get("harness_type")
            if ht:
                return str(ht)
        except Exception:
            pass
    try:
        from omegaconf import OmegaConf
        cfg = OmegaConf.load(inst["config_path"])
        ht = cfg.run_config.get("harness_type") if hasattr(cfg.run_config, "get") \
            else getattr(cfg.run_config, "harness_type", None)
        return str(ht) if ht else "openclaw"
    except Exception:
        return "openclaw"


# ============ 浅层：单实例分级 ============

def _level_rows_for_instance(inst: dict) -> list[dict]:
    """枚举该实例 task_records 的（traj_name 可用）行 + 对应 task_traj_records 浅层状态。

    LEFT JOIN task_traj_records 取该 config 的 shallow_status：任一轨迹行已处理
    （processed/empty/error 非 NULL）即视为该 task 已做过浅层分析，驱动源据此跳过。
    返回 dict 列表（含 shallow_status，无轨迹行为 NULL）。
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT tr.task_idx, tr.config_name, tr.traj_level, tr.status, tr.updated_at, "
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
                        shallow_error: str | None = None,
                        artifact_complete: int | None = None) -> None:
    """task_traj_records UPSERT（ON CONFLICT 更新分级列，不动 status/路径列）。

    tr 为 task_records 行或 per-task entry（traj_name 从中取，缺列时回退 config_name
    leaf）；ps 为 per-task entry（keys: task/tool_calls/plain_rounds/passed_gate/has_eval/
    evaluator_completion/task_done/…），evaluator_completion 落 completion 列。
    shallow_status: 浅层处理状态（processed/empty/error），None 时保持列不改（只写一次）。

    artifact_complete: 该 config 是否在 complete.jsonl 里（1/0），None 表示不改动。
    浅层建行时顺手写入，否则这些行会一直停在 DEFAULT 0 —— 工作区列表按 complete
    过滤，结果是「任务早跑完了但界面上一个都看不到」。
    与 _discover_artifacts 用同一套 MAX() 语义：用户显式点过下载的行已被置 1，
    不能被这里的 0 覆盖回去。
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
                 shallow_status, shallow_error, artifact_complete, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
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
                -- 与 _discover_artifacts 同款 MAX()：用户点过下载的行已是 1，
                -- 不能被浅层写入的 0 覆盖回去，否则下一轮发现就把它踢出队列。
                artifact_complete = MAX(COALESCE(task_traj_records.artifact_complete, 0),
                                        COALESCE(excluded.artifact_complete, 0)),
                updated_at      = CURRENT_TIMESTAMP
            """,
            (inst["id"], task_idx, config_name, traj_name, level, harness,
             int(bool(ps.get("passed_gate"))), int(bool(ps.get("has_eval"))),
             int(bool(ps.get("task_done"))), comp,
             ps.get("tool_calls"), ps.get("plain_rounds"),
             ps.get("input_tokens"), ps.get("output_tokens"),
             ps.get("reasoning_tokens"), ps.get("total_tokens"),
             ps.get("char_len"), traj_rel, status, error,
             shallow_status, shallow_error, artifact_complete),
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


def _task_log_path(inst: dict, task_idx: int) -> str:
    """实例本地单任务日志路径 <inst_dir>/outputs/config/logs/task-<idx>.log（零 OBS 成本）。"""
    return os.path.join(os.path.dirname(inst["config_path"]),
                        "outputs", "config", "logs", f"task-{task_idx}.log")


def _read_task_log_map(inst: dict) -> dict:
    """扫实例 outputs/config/logs/task-*.log，得到 {config_name: {task_idx, status, code}}。

    finished 实例在线 _sync_task_records 可能因竞态漏写部分行（多 task 并发终止时
    8s 轮询只捕获部分文件变化），这里从任务日志补齐缺失行的 status/code/task_idx。
    日志不存在/读不到时返回空 dict（调用方退化为默认值）。

    **并行读**：单个日志 34.7ms（NFS 上 3.5 万个小文件），1 万行的实例串行要 348s，
    足以让一轮卡死十几分钟。改成 32 并发后 1 万行约 11s。obsutil 不参与、只读本地
    NFS，线程安全。
    """
    logs_dir = _logs_dir(inst)
    if not os.path.isdir(logs_dir):
        return {}
    try:
        names = []
        for fn in os.listdir(logs_dir):
            m = re.match(r"task-(\d+)\.log$", fn)
            if m:
                names.append((int(m.group(1)), fn))
    except OSError:
        return {}
    if not names:
        return {}

    def scan_one(item):
        idx, fn = item
        config_name = status = code = None
        task_done = False
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
                    if not task_done and oa.TRAJ_DONE_MARKER in line:
                        task_done = True
        except OSError:
            return None
        if not config_name:
            return None
        # status 保持原始值（None = 日志里还没有终态行，任务可能仍在跑或被 kill）。
        # 调用方自己决定怎么兜底：INSERT status 列用 "任务异常"，但 task_done 要留 NULL。
        # 不能在这里就兜底成 "任务异常"，否则「没跑完」和「真异常」再也分不开，
        # task_done 会被永久写成 0 且不再重扫。
        return (config_name, {"task_idx": idx, "status": status,
                              "code": code, "task_done": task_done})

    out = {}
    with ThreadPoolExecutor(max_workers=BACKFILL_CONCURRENCY) as pool:
        for res in pool.map(scan_one, names):
            if res:
                out[res[0]] = res[1]
    return out


def _seed_task_records_from_logs(inst: dict) -> int:
    """用实例本地 outputs/config/logs/task-*.log 补齐 task_records 缺失行。

    为什么需要：task_records 正常由在线 _sync_task_records 在实例 running 时增量写入。
    从别的机器拷来的实例、或建库早于在线同步的实例，这张表可能是空的 —— 而
    会话列表（/shallow/tasks）、漏斗（/shallow）全部读它，于是页面一片空白，
    即使 task_traj_records / OBS 里数据齐全。补全逻辑本身又只看「task_records 中
    traj_level IS NULL 的缺口行」，表空就永远没有缺口，形成死结。

    任务日志是这些行的权威来源（hive.py 每个 task 收尾都写 BEGIN config= 与
    任务执行状态= status error_code=），且完全在本地，零 OBS 成本。

    traj_level 写 NULL（不是 'failed'）：这些行还没做过浅层分级，写 NULL 正好让它们
    进入下面 _backfill_finished_tasks 的缺口队列，由 tsr / 日志兜底分级。
    已有行不动（ON CONFLICT DO NOTHING）——在线同步写入的 task_idx/status 更新更及时。

    返回新插入的行数。
    """
    log_map = _read_task_log_map(inst)
    if not log_map:
        return 0
    rows = []
    done_rows = []
    for config_name, meta in log_map.items():
        code = meta.get("code")
        if code in ("-", "", None):
            code = None
        category = code[0] if code else None
        # 日志里没有终态行（还在写 / 被 kill）→ task_done 留 NULL，下一轮再扫。
        # 不能当 0：那会把「还没跑完」误判成「没做完」，且再也不会重判。
        has_status = meta.get("status") is not None
        done_val = (1 if meta.get("task_done") else 0) if has_status else None
        # status 列保持原行为（无终态行按「任务异常」写），只有 task_done 区分 NULL。
        # 两者分开是为了：task_records.status 下游有既有依赖（缺口查询、错误分布），
        # 不能凭空多出 NULL；而 task_done 必须能表达「还没扫过」。
        rows.append((inst["id"], meta.get("task_idx"), config_name,
                     meta.get("status") or "任务异常", code, category, done_val))
        if has_status:
            done_rows.append((done_val, inst["id"], config_name))
    with get_connection() as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM task_records WHERE instance_id=?", (inst["id"],)
        ).fetchone()[0]
        conn.executemany(
            "INSERT INTO task_records "
            "(instance_id, task_idx, config_name, status, error_code, "
            " error_category, traj_level, task_done, updated_at) "
            "VALUES (?,?,?,?,?,?,NULL,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(instance_id, config_name) DO NOTHING",
            rows,
        )
        # 已有行的 task_done 单独回填：上面的 DO NOTHING 不会碰已存在的行，
        # 而在线 _sync_task_records 建的行 task_done 恒为 NULL。
        # 只填 NULL 的：扫过一次就不再重扫（增量语义）。
        if done_rows:
            conn.executemany(
                "UPDATE task_records SET task_done=? "
                "WHERE instance_id=? AND config_name=? AND task_done IS NULL",
                done_rows,
            )
        conn.commit()
        after = conn.execute(
            "SELECT COUNT(*) FROM task_records WHERE instance_id=?", (inst["id"],)
        ).fetchone()[0]
    return after - before


# ============ 单任务日志（按需，仅补齐缺失 config 的 status/code） ============

def _logs_dir(inst: dict) -> str:
    """实例本地 logs 目录。优先级与 _local_complete_records 一致：config.yaml 的
    task_output_path 优先，取不到再回退 <inst_dir>/outputs/<stem>/logs。

    原来这里（以及 _task_log_path/_read_task_log_map）硬编码 outputs/config/logs，
    而实例搬迁过之后 config.yaml 里记的是别处的绝对路径，会扫不到任何日志。
    """
    inst_dir = os.path.dirname(inst.get("config_path") or "")
    stem = os.path.splitext(os.path.basename(inst.get("config_path") or ""))[0]
    candidates = []
    try:
        from omegaconf import OmegaConf
        cfg = OmegaConf.load(inst["config_path"])
        out = str(cfg.run_config.task.task_output_path or "")
        if out:
            # task_output_path 可能是 outputs 或 outputs/<stem>，两种都试
            candidates.append(os.path.join(out, "logs"))
            candidates.append(os.path.join(out, stem, "logs"))
    except Exception:
        pass
    candidates.append(os.path.join(inst_dir, "outputs", stem, "logs"))
    candidates.append(os.path.join(inst_dir, "outputs", "config", "logs"))
    for d in candidates:
        if os.path.isdir(d):
            return d
    return candidates[-1]


def _scan_done_for_running(inst: dict) -> int:
    """只扫「task_records.task_done IS NULL」的那些 config 对应的日志，回填 DONE 标记。

    为什么单独一个函数、而不是复用 _read_task_log_map：
    _read_task_log_map 会遍历整个 logs 目录（运行中的实例可能有 1.8 万个文件），
    每轮都全量扫一遍太贵。这里先用 SQL 查出还没判过的行（通常只有本轮新增的几十上百条），
    只读这些 task-<idx>.log，成本正比于增量。

    返回本轮判定成功的行数。
    """
    with get_connection() as conn:
        pending = [dict(r) for r in conn.execute(
            "SELECT config_name, task_idx FROM task_records "
            "WHERE instance_id=? AND task_done IS NULL",
            (inst["id"],),
        ).fetchall()]
    if not pending:
        return 0

    logs_dir = _logs_dir(inst)
    if not os.path.isdir(logs_dir):
        return 0

    def scan_one(row):
        idx = row.get("task_idx")
        if idx is None:
            return None
        p = os.path.join(logs_dir, f"task-{idx}.log")
        if not os.path.isfile(p):
            return None
        try:
            with open(p, errors="replace") as f:
                txt = f.read()
        except OSError:
            return None
        # 没有终态行的日志（还在写 / 被 kill）本轮跳过，留 NULL 下轮再判。
        # 否则会把「还没跑完」永久写成 0。
        if not _FINAL_STATUS_RE.search(txt):
            return None
        return (1 if oa.TRAJ_DONE_MARKER in txt else 0, inst["id"], row["config_name"])

    with ThreadPoolExecutor(max_workers=SHALLOW_CONCURRENCY,
                            thread_name_prefix=f"done-{inst['id'][:8]}") as pool:
        updates = [r for r in pool.map(scan_one, pending) if r]
    if not updates:
        return 0
    with get_connection() as conn:
        conn.executemany(
            "UPDATE task_records SET task_done=? "
            "WHERE instance_id=? AND config_name=? AND task_done IS NULL",
            updates,
        )
        conn.commit()
    return len(updates)


def _read_task_log_for(inst: dict, task_idx: int) -> dict:
    """读单个 task-<idx>.log，返回 {task_idx, config_name, status, code, task_done}。

    task_done = 主 log 含「Task_Done」标记，供无 tsr 时的日志兜底分级判断任务是否完成。
    读不到返回空 dict。
    """
    log = _task_log_path(inst, task_idx)
    if not os.path.isfile(log):
        return {}
    config_name = status = code = None
    task_done = False
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
                if not task_done and oa.TRAJ_DONE_MARKER in line:
                    task_done = True
    except OSError:
        return {}
    return {"task_idx": task_idx, "config_name": config_name,
            "status": status or "任务异常", "code": code, "task_done": task_done}


def _fetch_tsr_entries_fast(obsutil: str, task_obs: str, origin: str,
                            instance_harness_type: str | None = None) -> list[dict]:
    """补全专用快路径：拉单个 task 的 logs/traj_stats_result.json（约 1KB）→ tsr entries。

    本地已有同源 tsr 缓存时直接读不重复下载（-f 强制覆盖会浪费 OBS 往返）。
    返回 [] = 无 tsr / 解析失败（不慢路径下载，调用方占位）。task_done 不在这里拉
    （那是详情列，留深层按需；补全只关心分级所需的 tool_calls/plain_rounds）。

    instance_harness_type: 实例 config.yaml 的 run_config.harness_type（如 openjiuwen/hermes/openclaw）。
    注入给 harness_tsr_to_entries，覆盖 tsr 自身 harness_type 字段缺失时的 harness_home 猜测逻辑，
    确保存量实例（tsr 无 harness_type 字段）也能按正确框架分级。
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
    # tsr 自身无 harness_type 字段时（存量实例），用实例级 instance_harness_type 补填，
    # 避免 harness_tsr_to_entries 回退到 harness_home 路径猜测（openjiuwen 写 ~/.openclaw 导致误判）。
    if instance_harness_type and not data.get("harness_type"):
        data = dict(data, harness_type=instance_harness_type)
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
    # 实例级 harness 类型：占位行与 compute_level 的 harness 兜底都用它，避免一律写死 openclaw
    # （存量实例 tsr 无 harness_type 字段时，靠这个值定级；也用于无 tsr 占位行的 harness 列）。
    inst_ht = _instance_harness_type(inst)
    # 先把「本地任务日志里有、DB 里没有」的 config 补成 task_records 行。
    # 必须做在查缺口之前：从别的机器拷来的实例（或在线 _sync_task_records 从未跑过的
    # finished 实例）task_records 整张表是空的，缺口查询恒返回 0 行 → 补全永远不启动
    # → 会话列表空白、漏斗全 0，而 task_traj_records 里其实有货。任务日志是这些行
    # 的权威来源（hive.py 每个 task 收尾都会写 BEGIN config= / 任务执行状态=）。
    n_seeded = _seed_task_records_from_logs(inst)
    if n_seeded:
        print(f"    [backfill] {inst['id']}: 从本地任务日志补 {n_seeded} 行 task_records",
              flush=True)

    with get_connection() as conn:
        gaps = [dict(r) for r in conn.execute(
            "SELECT config_name, task_idx, status, error_code "
            "FROM task_records WHERE instance_id=? AND traj_level IS NULL "
            "ORDER BY task_idx LIMIT ?",
            (inst["id"], BACKFILL_MAX_TASKS_PER_INSTANCE),
        ).fetchall()]
    if not gaps:
        return n_seeded

    # 补全同样要顺手写 artifact_complete：否则 finished 实例这批行建出来仍是 0，
    # 交付物列表按 complete 过滤后一条不显示。每个实例只读一次 complete.jsonl。
    try:
        _complete_set = set(_local_complete_records(inst))
    except Exception:
        _complete_set = set()

    def _is_complete(config_name: str) -> int:
        stem = config_name[:-5] if config_name.endswith(".json") else config_name
        return 1 if (stem in _complete_set or config_name in _complete_set) else 0

    # 任务异常/失败的缺口行：轨迹几乎不存在（finished 场景抽查 100% 无 OBS 目录），批量
    # failed 占位退出缺口队列（避免每轮逐行探测 OBS）。已分级/占位的行不再进入缺口查询。
    # 例外：stopped 实例（如中途中止）轨迹往往已部分上传，任务异常 ≠ 无轨迹——仍逐行探测
    # OBS（见下 _backfill_one 的 obs_leaves 集合差），避免把有真实轨迹的会话误标 failed。
    #
    # ⚠ 必须在 _list_obs_leaves 之前做：批量占位不用 obs_leaves，先做能让「缺口全是
    # 任务异常」的实例（实测 161 个 / 7.6 万行）完全不碰 OBS。此前顺序反了，这些实例
    # 每轮都白枚举一次（2~11s），然后一行都不用。
    remaining = gaps
    if inst["status"] != "stopped":
        failed_rows = [g for g in gaps if g["status"] == "任务异常"]
        if failed_rows:
            _batch_placeholder_failed(inst, failed_rows, "任务异常缺口批量占位")
            remaining = [g for g in gaps if g["status"] != "任务异常"]

    # 没有剩余待定级的行 → 一次 OBS 都不碰。
    if not remaining:
        return n_seeded

    print(f"    [backfill] {inst['id']}: 缺口 {len(gaps)} 行（其中 "
          f"{len(gaps) - len(remaining)} 行本地占位），枚举 OBS 叶子目录", flush=True)

    # 首目录探测：区分"轨迹整批在 OBS"（在线竞态漏写，逐行拉 tsr 分级）和"轨迹已失效"
    # （历史死数据，逐行探测连续未命中后整批占位）。不用天数猜测：8/18 大批量实例缺口
    # 数千行，逐行拉 tsr 会让每轮卡死分钟级。注意混合实例（同一实例部分 config 上传、
    # 部分未上传）也存在——所以首目录未命中时不整批占位，而是逐行 ls 探测，命中才拉 tsr。
    # 一次 obsutil ls -d 枚举 obs_base 全部 task 叶子目录，与缺口行做集合差：
    # 命中 → 拉 tsr 分级；未命中 → failed 占位（不可分级，退出缺口队列）。
    # 替代旧的「首目录探测 + 逐行 _obs_dir_exists + miss 计数整批占位」，
    # 把 N 次 OBS 往返降为 1 次（OBS 侧一次拿到整批目录集合）。
    obs_leaves = _list_obs_leaves(obsutil, obs_base)

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
                                    "failed", inst_ht, {}, None,
                                    traj_name=leaf, shallow_status="empty",
                                    shallow_error="OBS 无轨迹目录",
                                    artifact_complete=_is_complete(config_name))
                return 0
            # 补全专用快路径：只拉 tsr（约 1KB），不下载主 log / 不慢路径下载整轨迹。
            entries = _fetch_tsr_entries_fast(obsutil, task_obs, origin, inst_ht)
            if not entries:
                # 无有效 tsr（缺失/空壳）：先从实例本地日志兜底判分级——含「Task_Done」
                # 标记即视为任务完成，至少 L1.5（零 OBS 成本，读 task-<idx>.log）。传
                # meta["status"] 以便旧版 harness 采信「所有任务执行完成!」标识。
                fb = oa.log_fallback_grade(_task_log_path(inst, meta.get("task_idx") or 0),
                                           inst_ht, meta.get("status"))
                if fb:
                    fb_entry, fb_level = fb
                    _insert_task_record_placeholder(inst, config_name, meta, level=fb_level)
                    _upsert_traj_record(inst, {"config_name": config_name},
                                        meta.get("task_idx"), config_name,
                                        fb_level, fb_entry.get("harness", inst_ht),
                                        fb_entry, None, traj_name=leaf,
                                        shallow_status="log_fallback",
                                        shallow_error="无有效 tsr，日志兜底",
                                        artifact_complete=_is_complete(config_name))
                    return 1
                # 日志也无 Task_Done：不慢路径下载（补全不能因大轨迹卡死整轮）。写占位行：
                #   task_records: status 取日志、traj_level='failed'（不可分级，不进漏斗）
                #   task_traj_records: status='done'、无路径 → 点会话时 trigger_deep 置
                #     pending → 深层按需下载解析（否则 trigger_deep 404「无轨迹记录」）
                _insert_task_record_placeholder(inst, config_name, meta)
                _upsert_traj_record(inst, {"config_name": config_name},
                                    meta.get("task_idx"), config_name,
                                    "failed", inst_ht, {}, None,
                                    traj_name=leaf, shallow_status="error",
                                    shallow_error="OBS 有目录但无有效 tsr",
                                    artifact_complete=_is_complete(config_name))
                return 0
            entry = entries[0]
            level = oa.compute_level(entry.get("harness", inst_ht),
                                     entry.get("tool_calls", 0),
                                     entry.get("plain_rounds", 0),
                                     entry.get("evaluator_completion"))
            _insert_task_record_placeholder(inst, config_name, meta, level=level)
            for e in entries:
                # entry 不含 traj_name/config_name 列（只有 task=leaf），显式传 leaf
                leaf2 = os.path.basename(str(e.get("task") or "")).rstrip("/") or leaf
                _upsert_traj_record(inst, {"config_name": config_name},
                                    meta.get("task_idx"), config_name,
                                    level, e.get("harness", inst_ht),
                                    e, e.get("trajectory"), traj_name=leaf2,
                                    shallow_status="processed",
                                    artifact_complete=_is_complete(config_name))
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

    # 运行中实例也要扫 DONE 标记：_read_task_log_map 只在 finished/stopped 分支被调用过，
    # 导致 running 实例的 task_done 永远是 NULL，首页 DONE 列一直为空。
    # 它内部只扫「task_records 里 task_done IS NULL」的 config —— 已判过的不重扫，
    # 所以每轮成本正比于本轮新增的日志数，不是全量。
    try:
        _scan_done_for_running(inst)
    except Exception as e:
        print(f"    [warn] {inst['id']} 扫 DONE 标记失败: {e}", flush=True)

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
    # 实例级 harness 类型：占位行 harness 列与 compute_level 兜底都用它，避免写死 openclaw。
    inst_ht = _instance_harness_type(inst)
    # 每个实例只读一次 complete.jsonl（NFS 上 os.walk + 全文解析，2 万行的实例要几百 ms）。
    # 浅层建行时用它顺手写 artifact_complete —— 否则这些行永远停在 DEFAULT 0，
    # 工作区列表按 complete 过滤后一条都不显示（实测 1479 个实例踩过）。
    try:
        _complete_set = set(_local_complete_records(inst))
    except Exception:
        _complete_set = set()

    def _is_complete(config_name: str) -> int:
        """config 是否在 complete.jsonl 里。config_name 带 .json，集合里存的是去掉后缀的叶子名。"""
        stem = config_name[:-5] if config_name.endswith(".json") else config_name
        return 1 if (stem in _complete_set or config_name in _complete_set) else 0

    # 运行中的实例：只分级「已经跑完」的任务。
    #
    # 为什么：running 实例的轨迹是边跑边传的，对还没跑完的任务去 OBS 找目录/找 tsr，
    # 必然是空的——以前会把这种「还没传到」当成异常，每轮重试一遍，把整轮时间耗在
    # 空探测上（实测单轮 5192s、每轮 4 万行里大半是这种空转）。complete.jsonl 是
    # hive 在任务收尾时 append 的权威完成信号，拿它当门闸后，每个任务只在真正
    # 跑完后的那一轮被分级一次，既不会漏也不会空转。
    #
    # 注意只在 running 时收紧：preparing 也走这个分支（还没开始跑，同样没有轨迹），
    # 但 stopped/finished 已在上面的分支返回。
    if is_running and _complete_set:
        pending = [r for r in pending if _is_complete(r["config_name"])]

    def grade_log_fallback_or_failed(inst: dict, config_name: str, task_idx,
                                     ht: str, shallow_status: str,
                                     shallow_error: str,
                                     status: str | None = None) -> bool:
        """无有效 tsr 时的统一落库：先本地日志兜底（Task_Done → ≥L1.5），否则 failed 占位。

        返回 True = 日志兜底成功分级；False = failed 占位。读本地 task-<idx>.log（零 OBS
        成本），覆盖 openjiuwen 老镜像写空壳 tsr 等场景。task_records 已有行用
        _apply_level_to_task_records 回填，无行用 _insert_task_record_placeholder 补齐。
        status = 已知任务执行状态（task_records.status），传给 log_fallback_grade 以便对
        旧版 harness 采信「所有任务执行完成!」标识（需与「任务成功」互相印证）。
        """
        leaf = os.path.splitext(config_name)[0]
        fb = oa.log_fallback_grade(_task_log_path(inst, task_idx or 0), ht, status)
        if fb:
            fb_entry, fb_level = fb
            _apply_level_to_task_records(inst, config_name, fb_level, fb_entry)
            _upsert_traj_record(inst, {"config_name": config_name}, task_idx, config_name,
                                fb_level, fb_entry.get("harness", ht), fb_entry, None,
                                traj_name=leaf, shallow_status="log_fallback",
                                shallow_error="无有效 tsr，日志兜底",
                                artifact_complete=_is_complete(config_name))
            return True
        _apply_level_to_task_records(inst, config_name, "failed")
        _upsert_traj_record(inst, {"config_name": config_name}, task_idx, config_name,
                            "failed", ht, {}, None,
                            traj_name=leaf, shallow_status=shallow_status,
                            shallow_error=shallow_error,
                            artifact_complete=_is_complete(config_name))
        return False

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
                grade_log_fallback_or_failed(inst, config_name, r.get("task_idx"),
                                             inst_ht, "empty", "OBS 无轨迹目录",
                                             r.get("status"))
            return (0, 0, config_name)
        try:
            entries = oa._load_per_task_entries(obsutil, task_obs, origin,
                                                instance_harness_type=inst_ht)
            if not entries:
                grade_log_fallback_or_failed(inst, config_name, r.get("task_idx"),
                                             inst_ht, "error", "OBS 有目录但无有效 tsr",
                                             r.get("status"))
                return (0, 1, config_name)
            # 每 task 通常只有 1 条 entry（assistant 主轨迹）；多条时逐条落，等级按第一条
            entry = entries[0]
            level = oa.compute_level(entry.get("harness", inst_ht),
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
                                    level, e.get("harness", inst_ht),
                                    e, e.get("trajectory"), traj_name=leaf2,
                                    shallow_status="processed",
                                    artifact_complete=_is_complete(config_name))
            return (1, 0, config_name)
        except Exception as e:
            _upsert_traj_record(inst, {"config_name": config_name},
                                r.get("task_idx"), config_name,
                                "failed", inst_ht, {}, None,
                                traj_name=leaf, shallow_status="error",
                                shallow_error=str(e)[:300],
                                artifact_complete=_is_complete(config_name))
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


# ============ 沙箱产物全量下载 ============

def _local_complete_records(inst: dict) -> list[str]:
    """读实例本地的 outputs/<...>/complete.jsonl，返回已完成的 config 文件名列表。

    这是「哪些任务真的跑完了」的权威来源（hive.py:368-377 每个任务结束时 append 一行，
    整批跑完才上传 OBS —— 但本地文件一直都在）。每行是一个 config 文件名，
    如 `task_000124.json`；对应 OBS 叶子目录名 = 去掉 `.json`。

    task_output_path 在 config.yaml 里是建实例时写死的绝对路径，机器搬迁后可能失效
    （实测拷来的实例指向原机器的 /root/...），所以取不到时回退到实例目录下的 outputs/。
    """
    names: list[str] = []
    out_dir = None
    try:
        from omegaconf import OmegaConf
        cfg = OmegaConf.load(inst["config_path"])
        out_dir = str(cfg.run_config.task.task_output_path or "")
    except Exception:
        out_dir = None
    if not out_dir or not os.path.isdir(out_dir):
        inst_dir = os.path.dirname(inst.get("config_path") or "")
        if inst_dir:
            out_dir = os.path.join(inst_dir, "outputs")
    if not out_dir or not os.path.isdir(out_dir):
        return []

    # complete.jsonl 可能在 outputs/ 或 outputs/<batch>/ 下（hive 的 task_output_path
    # 会随批次追加一层），两者都找
    candidates = [os.path.join(out_dir, "complete.jsonl")]
    for dirpath, dirnames, filenames in os.walk(out_dir):
        if "complete.jsonl" in filenames:
            p = os.path.join(dirpath, "complete.jsonl")
            if p not in candidates:
                candidates.append(p)
        # 只下钻两层，避免遍历巨大的 outputs 树
        if dirpath[len(out_dir):].count(os.sep) >= 2:
            dirnames[:] = []

    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    name = line.strip()
                    if not name:
                        continue
                    stem = name[:-5] if name.endswith(".json") else name
                    if stem not in names:
                        names.append(stem)
        except OSError:
            continue
    return names


def _ls_objects_retry(obsutil: str, task_obs: str, attempts: int = 3) -> list:
    """递归列 task 对象，带重试。

    并发下 obsutil 会偶发 exit 8（请求数超限类的瞬时错误）；实测手动单跑同前缀
    立刻成功（114 个文件）。不重试的话这些 task 会被记账成 failed 而永久跳过。
    """
    last = None
    for i in range(attempts):
        try:
            return oa.ls_objects(obsutil, task_obs, recursive=True, timeout=300)
        except Exception as e:
            last = e
            if i + 1 < attempts:
                time.sleep(2 * (i + 1))       # 2s, 4s 递增
    raise last


def _cp_retry(obsutil: str, task_obs: str, dest_parent: str, attempts: int = 4) -> None:
    """递归 cp 整个 task 前缀，带重试。

    实测失败案例：
      - exit 8 → OBS 请求数超限（并发太高）
      - exit 4 + 网关空错误（Code=/RequestId= 全空）→ 瞬时故障
    两者单独重跑都能成功（实测 task_000224 手动 cp 4.09MB 成功），
    所以必须重试，否则这些 task 会被记账成 failed 而跳过。

    重试间隔递增（3s/6s/12s），给 OBS 侧限流留恢复时间。
    """
    last = None
    for i in range(attempts):
        try:
            oa.run_obsutil_cp(obsutil, task_obs, dest_parent, recursive=True)
            return
        except Exception as e:
            last = e
            if i + 1 < attempts:
                time.sleep(3 * (2 ** i))
    raise last


def _download_artifact_task(inst: dict, tr: dict, origin: str, obsutil: str) -> tuple:
    """下载单个 task 的全量产物到 output_cache/<batch>/<leaf>/。

    `obsutil cp <task_obs> <dest_parent> -r -f` 保留 leaf basename（已实测），
    落点与 deep 详情用同一个 task 目录，一次下载同时满足工作区与会话详情。
    """
    leaf = tr["traj_name"]
    obs_base = _make_obs_base_for(inst)
    task_obs = obs_base.rstrip("/") + "/" + leaf + "/"
    cache_subdir = oa._cache_subdir_for(task_obs)
    dest_parent = os.path.join(origin, os.path.dirname(cache_subdir))
    dest = os.path.join(origin, *cache_subdir.split("/"))
    os.makedirs(dest_parent, exist_ok=True)

    t0 = time.time()
    try:
        _cp_retry(obsutil, task_obs, dest_parent)
    except Exception as e:
        # 失败必须清掉半截目录：obsutil cp 有隐藏的 180s 超时，超时会被 kill，
        # 此时磁盘上留下一个不完整的 task 目录。原来直接 return，磁盘被长期占着，
        # 而且下一轮重试会往同一目录续写，最后可能把半截内容当成完整结果。
        shutil.rmtree(dest, ignore_errors=True)
        return ("fail", None, (str(e)[:500], inst["id"], tr["config_name"], leaf))

    # 统计落盘结果（原来靠下载前递归 ls 预估体积，现在改为落盘后实测 ——
    # 少一次可能长达数分钟的递归列举）
    n = 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(dest):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
                n += 1
            except OSError:
                pass

    # 一个文件都没下到：这是**正常终态**，不是故障。
    #
    # 为什么必须和 failed 分开：complete.jsonl 只收录「任务成功」的任务
    # （实测某实例 976 条成功全在里面、704 条失败一条都不在），但即便成功，
    # 沙箱也可能一份文件都没上传 —— 实测
    # obs://.../outputs-2610262015-glm53-256k/task_e9d1d1f83a2d/ 就是
    # File number: 0 / Total size 0B。
    # 若判成 failed，worker 会重试 3 轮、每轮 4 次 cp_retry，然后才判死 skipped：
    # 十几次 OBS 往返全白打，用户点过的任务还显示成「下载失败」。
    # 所以单独用一个终态 empty：明确、不再重试、界面上也不当错误。
    #
    # 也不能沿用原来直接返回 ok —— 那会写 status=done / artifact_files=0，
    # 用户看到「已下载 ✓」但工作区空无一物，且没有任何途径重新拉取。
    if n == 0:
        shutil.rmtree(dest, ignore_errors=True)
        return ("empty", None, (task_obs, inst["id"], tr["config_name"], leaf))

    elapsed = time.time() - t0
    if total > ARTIFACT_MAX_TASK_BYTES:
        # 超限：磁盘已经被占了，删掉避免长期占空间，并标 oversize 不再重试
        shutil.rmtree(dest, ignore_errors=True)
        msg = f"超过单任务上限 {ARTIFACT_MAX_TASK_BYTES} 字节（实际 {total}）"
        print(f"    [artifacts] {leaf}: 已删除（{total/1048576:.1f}MB 超上限）", flush=True)
        return ("oversize", None, (msg, inst["id"], tr["config_name"], leaf))

    print(f"    [artifacts] {leaf}: 完成 {n} 文件 / {total/1048576:.2f}MB "
          f"{elapsed:.1f}s", flush=True)
    return ("ok", (n, total, time.time(), inst["id"], tr["config_name"], leaf), None)


def _leaf_of(config_name: str) -> str:
    """config_name（带 .json）→ OBS 叶子目录名。与 _discover_artifacts 同口径。"""
    return config_name[:-5] if config_name.endswith(".json") else config_name


def _requeue_due_failed(limit: int = 200) -> int:
    """把退避到期的 failed 行排回 queued。

    没有这一步的话 failed 就是事实终态：`_claim_and_download` 只领
    artifact_state='queued'，而 failed 行不会再有任何东西把它们改回来
    （tracker 的普通增量刻意不动已 failed 的行，否则每轮都会把失败行打回队列）。
    原来这套重排在 _discover_artifacts 里，发现阶段删掉后必须补上，
    否则一次瞬时故障（obsutil 被 kill、NFS 抖动）就永久丢掉那个 task。

    LIMIT 是必须的：几万条同时到期时不能一次全排回，否则 worker 会被
    同一批失败行反复占满。
    """
    now = time.time()
    try:
        with get_connection() as conn:
            cur = conn.execute(
                "UPDATE shallow_queue SET artifact_state='queued', "
                "artifact_claimed_by=NULL, artifact_claimed_at=NULL, "
                "artifact_note=NULL, updated_at=CURRENT_TIMESTAMP "
                "WHERE id IN ("
                "  SELECT id FROM shallow_queue "
                "   WHERE artifact_state='failed' "
                "     AND (artifact_retry_at IS NULL OR artifact_retry_at <= ?) "
                "     AND artifact_attempts < ? "
                "   ORDER BY artifact_retry_at LIMIT ?)",
                (now, ARTIFACT_MAX_ATTEMPTS, limit))
            n = cur.rowcount
            # 重试已用尽的判死，避免它们永远停在 failed 被反复扫描
            dead = conn.execute(
                "UPDATE shallow_queue SET artifact_state='skipped', "
                "artifact_note=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE artifact_state='failed' AND artifact_attempts >= ?",
                (f"重试达上限 {ARTIFACT_MAX_ATTEMPTS} 次", ARTIFACT_MAX_ATTEMPTS)
            ).rowcount
            conn.commit()
        if n:
            print(f"    [artifacts] failed 行排回队列 {n} 条", flush=True)
        if dead:
            print(f"    [artifacts] failed 行判死 {dead} 条", flush=True)
        return n
    except Exception as e:
        print(f"    [artifacts] failed 重排失败: {str(e)[:150]}", flush=True)
        return 0


def _reclaim_stuck_hours(timeout_minutes: int | None = None) -> int:
    """回收卡死的 claimed 行。

    worker 中途被 kill 时不会回滚，行会永久停在 claimed（CAS 只认 queued，
    再也捡不起来）。用 artifact_claimed_at（epoch REAL）判断 —— 不用 updated_at，
    后者是 SQLite UTC 字符串，和其他 epoch 列混用容易出错。
    """
    timeout = ARTIFACT_DOWNLOADING_TIMEOUT_MIN if timeout_minutes is None else timeout_minutes
    cutoff = time.time() - timeout * 60
    try:
        with get_connection() as conn:
            # 超过重试上限的直接判死，不再排队 —— 否则会陷入
            # 「超时回收→排回 queued→立刻再领取→再超时」的死循环（实测出现过 168 次）。
            dead = conn.execute(
                "UPDATE shallow_queue SET artifact_state='skipped', "
                "artifact_note=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE artifact_state='claimed' "
                "AND (artifact_claimed_at IS NULL OR artifact_claimed_at < ?) "
                "AND artifact_attempts >= ?",
                (f"放弃：下载超时且已重试 {ARTIFACT_MAX_ATTEMPTS} 次", cutoff,
                 ARTIFACT_MAX_ATTEMPTS)).rowcount

            cur = conn.execute(
                "UPDATE shallow_queue SET artifact_state='queued', "
                "artifact_claimed_by=NULL, artifact_claimed_at=NULL, "
                "artifact_note=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE artifact_state='claimed' "
                "AND (artifact_claimed_at IS NULL OR artifact_claimed_at < ?) "
                "AND artifact_attempts < ?",
                (f"自动回收：claimed 超过 {timeout} 分钟未回写", cutoff,
                 ARTIFACT_MAX_ATTEMPTS))
            n = cur.rowcount
            conn.commit()
        if dead:
            print(f"    [artifacts] 放弃 {dead} 条（重试达上限 "
                  f"{ARTIFACT_MAX_ATTEMPTS} 次）", flush=True)
        if n:
            print(f"    [artifacts] 回收卡死行 {n} 条", flush=True)
        return n + dead
    except Exception as e:
        print(f"    [artifacts] 回收失败: {str(e)[:150]}", flush=True)
        return 0


def _mirror_to_traj(iid: str, cfg: str, leaf: str, status: str,
                    files=None, total=None, ts=None, error=None) -> None:
    """把下载结果镜像到 task_traj_records —— 前端 /artifacts/{id}/tasks 读的是这张表。

    这一行可能**不存在**（浅层还没为它建过行）：先 UPDATE，未命中再补一条最小行。
    不能只 UPDATE —— 那会让「tracker 发现了、浅层还没建行」的 task 下载完却查不到。
    """
    with get_connection() as c:
        cur = c.execute(
            "UPDATE task_traj_records SET artifacts_status=?, error=?, "
            "artifact_files=COALESCE(?, artifact_files), "
            "artifact_bytes=COALESCE(?, artifact_bytes), "
            "artifact_downloaded_at=COALESCE(?, artifact_downloaded_at), "
            "artifact_complete=1, updated_at=CURRENT_TIMESTAMP "
            "WHERE instance_id=? AND config_name=? AND traj_name=?",
            (status, error, files, total, ts, iid, cfg, leaf))
        if cur.rowcount == 0:
            c.execute(
                "INSERT INTO task_traj_records "
                "(instance_id, config_name, traj_name, level, status, "
                " artifacts_status, artifact_complete, artifact_files, "
                " artifact_bytes, artifact_downloaded_at, error, updated_at) "
                "VALUES (?,?,?,NULL,'done',?,1,?,?,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(instance_id, config_name, traj_name) DO UPDATE SET "
                "  artifacts_status=excluded.artifacts_status, "
                "  artifact_complete=1, "
                "  artifact_files=COALESCE(excluded.artifact_files, "
                "                           task_traj_records.artifact_files), "
                "  artifact_bytes=COALESCE(excluded.artifact_bytes, "
                "                           task_traj_records.artifact_bytes), "
                "  error=excluded.error",
                (iid, cfg, leaf, status, files, total, ts, error))
        c.commit()


def _persist_artifact_outcome(outcome: str, ok, err) -> None:
    """把单个 task 的下载结果落库。积压/实时两个 worker 共用。

    写**两处**：
      1. shallow_queue 的 artifact 维 —— 队列的权威状态，决定是否还会被领取；
      2. task_traj_records —— 镜像，前端和 GET /artifacts/{id}/tasks 读这里。
    原来是「整轮结束后 executemany 批量回写」：一轮要下多个（单个 1~158s），
    整轮几分钟都不回写，页面上 done 一直是 0、downloading 一路涨，
    看起来像「一个都没下载成功」——其实文件早已在磁盘上。
    单行 UPDATE 的成本可以忽略，换来状态实时可见。
    """
    now = time.time()
    with get_connection() as c:
        if outcome == "ok":
            n, total, ts, iid, cfg, leaf = ok
            c.execute(
                "UPDATE shallow_queue SET artifact_state='done', "
                "artifact_claimed_by=NULL, artifact_claimed_at=NULL, "
                "artifact_retry_at=NULL, artifact_note=?, "
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE instance_id=? AND config_name=?",
                (f"{n} 文件 / {total} 字节", iid, cfg))
            c.commit()
            _mirror_to_traj(iid, cfg, leaf, "done", files=n, total=total, ts=ts)
        elif outcome == "oversize":
            msg, iid, cfg, leaf = err
            c.execute(
                "UPDATE shallow_queue SET artifact_state='skipped', "
                "artifact_claimed_by=NULL, artifact_claimed_at=NULL, "
                "artifact_note=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE instance_id=? AND config_name=?",
                (msg[:500], iid, cfg))
            c.commit()
            _mirror_to_traj(iid, cfg, leaf, "skipped", error=msg)
        elif outcome == "empty":
            # OBS 前缀确实存在但一个文件都没有 —— 沙箱侧没产出，不是平台故障。
            # 标成独立终态 empty（不是 failed，也不是 done）：
            #   - 不进重试队列（failed 会被重排 3 轮）
            #   - 界面显示「无产出」而不是「下载失败」
            #   - 写 artifact_files=0 让前端能区分「下完了但是空的」
            task_obs, iid, cfg, leaf = err
            msg = f"OBS 前缀下没有文件（沙箱未产出交付物）: {task_obs}"
            c.execute(
                "UPDATE shallow_queue SET artifact_state='empty', "
                "artifact_claimed_by=NULL, artifact_claimed_at=NULL, "
                "artifact_retry_at=NULL, artifact_note=?, "
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE instance_id=? AND config_name=?",
                (msg[:500], iid, cfg))
            c.commit()
            _mirror_to_traj(iid, cfg, leaf, "empty",
                            files=0, total=0, ts=now, error=msg)
        else:
            msg, iid, cfg, leaf = err
            c.execute(
                "UPDATE shallow_queue SET artifact_state='failed', "
                "artifact_claimed_by=NULL, artifact_claimed_at=NULL, "
                # 退避：failed 的行不能立刻被重新领走，否则会「失败→重领→失败」忙等。
                # 与 _discover_artifacts 原来的 ARTIFACT_FAIL_RETRY_SECONDS 同语义。
                "artifact_retry_at=?, artifact_note=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE instance_id=? AND config_name=?",
                (now + ARTIFACT_FAIL_RETRY_SECONDS, msg[:500], iid, cfg))
            c.commit()
            _mirror_to_traj(iid, cfg, leaf, "failed", error=msg)


def _claim_and_download(origin: str, obsutil: str, worker_id: str,
                        concurrency: int, instance_id: str | None = None,
                        pri_min: int | None = None,
                        pri_max: int | None = None,
                        budget_seconds: float = 0) -> tuple[int, int]:
    """领取 [pri_min, pri_max] 区间内的 queued 行并下载，返回 (done, failed)。

    队列来源是 `shallow_queue.artifact_state`（task_tracker 在 complete.jsonl
    增量读时标 queued），不再自己扫 task_traj_records —— 那张表的
    「发现」是全量 UPSERT，单实例 3.6 万 config，一轮能跑 900s。

    CAS（`WHERE artifact_state='queued'` + rowcount 校验）保证两个进程
    不会重复下载同一行：后到者 rowcount=0 直接跳过。

    按区间分流是实时 worker 的核心：积压 worker 只领 priority 低的，
    实时 worker 只领用户点过的高优先级行，互不抢线程也就不会互相拖慢。

    budget_seconds > 0 时一轮内**连续领取**直到队列空或超时。
    原来每轮只领 concurrency 条就 sleep，实测「3.5s 干活 + 60s 睡」，
    93% 的时间在空转；id 游标保证不会每轮都从头扫队列。
    """
    worker_name = f"{socket.gethostname()}:{os.getpid()}"
    deadline = time.time() + budget_seconds if budget_seconds > 0 else 0
    n_done = n_failed = 0
    last_id = 0

    while True:
        work: list[tuple[dict, dict]] = []
        # 每批的 thread 数：实时队列通常很短，别一次领满把线程池撑爆
        batch = concurrency
        with get_connection() as conn:
            where = ["artifact_state='queued'", "id > ?"]
            params: list = [last_id]
            if instance_id:
                where.append("instance_id=?")
                params.append(instance_id)
            if pri_min is not None:
                where.append("artifact_priority >= ?")
                params.append(pri_min)
            if pri_max is not None:
                where.append("artifact_priority < ?")
                params.append(pri_max)
            # 游标按 id 前进，所以排序键**必须**也是 id。
            # 原来用 `ORDER BY priority DESC, id` + `id > 游标`：priority 高的行
            # id 可能很小，被排到队首取走后游标跳过它后面同 priority 的行，
            # 于是「priority 高的老行」在同一轮内会被漏掉、永远轮不到。
            # 车道分流已经由上面的 WHERE 区间完成（积压 <100 / 实时 >=100），
            # 过滤后的集合里按 id 顺序取就是正确的。
            sql = ("SELECT id, instance_id, config_name, task_idx, artifact_priority, "
                   "artifact_attempts FROM shallow_queue WHERE " +
                   " AND ".join(where) +
                   " ORDER BY id LIMIT ?")
            params.append(batch)
            rows = conn.execute(sql, tuple(params)).fetchall()
            if not rows:
                # 本段没有更多行了；若已有产出就结束，否则也结束（队列空）
                break

            inst_cache: dict[str, dict] = {}
            for r in rows:
                r = dict(r)
                last_id = max(last_id, r["id"])
                # 退避窗口内的 failed 行不领：否则会「失败→立刻重领→再失败」忙等。
                if r["artifact_attempts"] >= ARTIFACT_MAX_ATTEMPTS:
                    conn.execute(
                        "UPDATE shallow_queue SET artifact_state='skipped', "
                        "artifact_note=?, updated_at=CURRENT_TIMESTAMP "
                        "WHERE id=? AND artifact_state='queued'",
                        (f"重试达上限 {ARTIFACT_MAX_ATTEMPTS} 次", r["id"]))
                    continue
                inst = inst_cache.get(r["instance_id"])
                if inst is None:
                    inst = _instance_by_id(conn, r["instance_id"])
                    if inst:
                        inst_cache[r["instance_id"]] = inst
                if not inst:
                    conn.execute(
                        "UPDATE shallow_queue SET artifact_state='skipped', "
                        "artifact_note='实例不存在', updated_at=CURRENT_TIMESTAMP "
                        "WHERE id=? AND artifact_state='queued'", (r["id"],))
                    continue
                cur = conn.execute(
                    "UPDATE shallow_queue SET artifact_state='claimed', "
                    "artifact_claimed_by=?, artifact_claimed_at=?, "
                    "artifact_attempts=artifact_attempts+1, "
                    "updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=? AND artifact_state='queued'",
                    (worker_name, time.time(), r["id"]))
                if cur.rowcount == 0:
                    continue            # 被别的进程抢走
                # _download_artifact_task 期望 tr 有 config_name / traj_name
                r["traj_name"] = _leaf_of(r["config_name"])
                work.append((r, inst))
            conn.commit()

        if not work:
            # 这一批全是还没到重试时间的 / 已被抢走的。若本轮已有产出且
            # id 已经走到头，就结束；否则继续往前推进游标，避免死循环。
            if deadline and time.time() < deadline:
                continue
            break

        with ThreadPoolExecutor(max_workers=max(1, len(work)),
                                thread_name_prefix="art") as pool:
            futures = {pool.submit(_download_artifact_task, inst, tr, origin, obsutil): tr
                       for tr, inst in work}
            for fut in as_completed(futures):
                # 线程体里任何未捕获异常（makedirs 撞 NFS 满盘、obsutil 被 kill 等）
                # 都会在 fut.result() 处重新抛出。若不接住，整个消费函数中断，
                # 这些行会永久停在 claimed，只能等 30 分钟超时回收。
                try:
                    outcome, ok, err = fut.result()
                except Exception as e:
                    tr = futures[fut]
                    outcome = "fail"
                    ok = None
                    err = (f"线程异常: {str(e)[:300]}", tr["instance_id"],
                           tr["config_name"], tr["traj_name"])
                    print(f"    [artifacts] {tr['traj_name']}: 线程异常 {str(e)[:120]}",
                          flush=True)
                try:
                    _persist_artifact_outcome(outcome, ok, err)
                except Exception as e:
                    print(f"    [artifacts] 回写失败: {str(e)[:120]}", flush=True)
                if outcome == "ok":
                    n_done += 1
                elif outcome == "empty":
                    # 空产出不是失败：不进 failed 计数，否则心跳的「fail」会虚高，
                    # 运维会去排查一个根本不存在的问题。
                    n_done += 1
                else:
                    n_failed += 1

        # 单批模式（budget_seconds=0）：领一批就返回，保持实时 worker 的短轮询语义
        if budget_seconds <= 0:
            break
        if deadline and time.time() >= deadline:
            break

    return n_done, n_failed


def _consume_artifacts_realtime(origin: str, obsutil: str,
                                instance_id: str | None = None) -> dict:
    """实时下载：只处理用户点过（priority >= 阈值）的行，且不做发现。

    不做发现是刻意的 —— 发现要给上万个 config 跑 upsert，属于积压 worker 的活。
    实时 worker 一轮只做「查一条队列 + cp」，实测单 task 1.6s，2 秒轮询即可
    做到「点完几十秒内看到文件」。
    """
    _reclaim_stuck_hours()
    _requeue_due_failed()
    n_done, n_failed = _claim_and_download(
        origin, obsutil, "", ARTIFACT_REALTIME_CONCURRENCY,
        instance_id=instance_id,
        pri_min=ARTIFACT_REALTIME_PRIORITY)
    try:
        with get_connection() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM shallow_queue "
                "WHERE artifact_state='queued' AND artifact_priority >= ?",
                (ARTIFACT_REALTIME_PRIORITY,)).fetchone()[0]
    except Exception:
        remaining = 0
    return {"n_discovered": 0, "n_done": n_done, "n_failed": n_failed,
            "n_remaining": int(remaining or 0)}


def _consume_artifacts(origin: str, obsutil: str, instance_id: str | None = None,
                       realtime_only: bool = False) -> dict:
    """下载沙箱产物。返回 {n_discovered, n_done, n_failed, n_remaining}。

    队列来源已改为 `shallow_queue.artifact_state`，**本函数不做发现** ——
    tracker（offline/task_tracker.py）在读 complete.jsonl 增量时就把行标成
    queued，它每轮 1.7s、带 offset+inode 增量，而原来这里的全量 UPSERT
    单实例 3.6 万 config、一轮 900s（实测 done 却只有 6~8 个）。

    n_discovered 恒为 0，保留字段是为了心跳/看板兼容。

    realtime_only: 只领用户点击过的行（priority >= ARTIFACT_REALTIME_PRIORITY），
      实时 worker 只负责把用户已经点过的任务尽快下完。
    非 realtime 的进程只领 priority < 阈值 的行，两边互不抢，CAS 也不会重叠。
    """
    if realtime_only:
        return _consume_artifacts_realtime(origin, obsutil, instance_id)

    _reclaim_stuck_hours()
    _requeue_due_failed()

    # 一轮内连续领取，直到队列空或超时。原来是「每轮只领 concurrency 条 +
    # 固定 sleep(interval)」，实测 3.5s 干活 + 60s 睡眠，93% 时间空转。
    n_done, n_failed = _claim_and_download(
        origin, obsutil, "", ARTIFACT_CONCURRENCY,
        instance_id=instance_id,
        pri_max=ARTIFACT_REALTIME_PRIORITY,
        budget_seconds=ARTIFACT_ROUND_BUDGET_SEC)

    try:
        with get_connection() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM shallow_queue "
                "WHERE artifact_state='queued' AND artifact_priority < ?",
                (ARTIFACT_REALTIME_PRIORITY,)).fetchone()[0]
    except Exception:
        remaining = 0

    return {"n_discovered": 0,
            "n_done": n_done, "n_failed": n_failed,
            "n_remaining": int(remaining or 0)}


# ============ 主循环 ============

def _worker_mode(deep_only: bool, shallow_only: bool,
                 artifacts_only: bool, realtime_only: bool = False) -> str:
    """本轮进程扮演的角色，写进心跳的 worker 列。"""
    if realtime_only:
        return "realtime"
    if artifacts_only:
        return "artifacts"
    if shallow_only:
        return "shallow"
    if deep_only:
        return "deep"
    return "mixed"


def _write_heartbeat(worker: str, pid: int, round_start: float, round_end: float,
                     processed: int, failed: int, note: str = "") -> None:
    """落一行心跳。**失败静默**——监控不能拖垮 worker 本身。

    只保留最近 HEARTBEAT_KEEP_ROWS 行，顺带裁剪。
    """
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO worker_heartbeats "
                "(worker, pid, round_start, round_end, duration, processed, failed, note) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (worker, pid, round_start, round_end, round(round_end - round_start, 2),
                 processed, failed, (note or "")[:300]))
            conn.execute(
                "DELETE FROM worker_heartbeats WHERE id <= "
                "(SELECT MAX(id) - ? FROM worker_heartbeats)", (HEARTBEAT_KEEP_ROWS,))
            conn.commit()
    except Exception:
        pass


def _write_worker_started(worker: str, pid: int, note: str) -> None:
    """启动标记行（round_start=0）：区分「从未运行过」与「运行过但已退出」。

    duration 留空 —— round_start=0 是哨兵值，拿它算时长会得到一个无意义的巨大数。
    """
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO worker_heartbeats "
                "(worker, pid, round_start, round_end, duration, processed, failed, note) "
                "VALUES (?,?,0,?,NULL,0,0,?)",
                (worker, pid, time.time(), note[:300]))
            conn.execute(
                "DELETE FROM worker_heartbeats WHERE id <= "
                "(SELECT MAX(id) - ? FROM worker_heartbeats)", (HEARTBEAT_KEEP_ROWS,))
            conn.commit()
    except Exception:
        pass


def _run_once(obsutil: str, origin: str,
              instance_id: str | None, deep_only: bool, shallow_only: bool = False,
              artifacts_only: bool = False, realtime_only: bool = False) -> dict:
    report: dict = {"shallow": {"n_processed": 0, "n_failed": 0},
                    # deep 恒为 0：轨迹下载已并入产物下载
                    "deep": {"n_done": 0, "n_failed": 0},
                    "artifacts": {"n_discovered": 0, "n_done": 0, "n_failed": 0,
                                  "n_remaining": 0}}

    # 实时下载独立进程：只处理用户点击过的任务，不做发现、不碰积压队列。
    if realtime_only:
        report["artifacts"] = _consume_artifacts_realtime(origin, obsutil, instance_id)
        return report

    # 产物下载独立进程：--artifacts-only 时只跑产物，其余全跳过
    if artifacts_only:
        report["artifacts"] = _consume_artifacts(origin, obsutil, instance_id)
        return report

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

    # 产物下载（含轨迹）：--shallow-only 时不跑；--deep-only 与混合都跑。
    # 注：原「深层轨迹/日志按需下载」已取消 —— 产物全量下载覆盖同一目录，
    # 会话详情直接读本地，不再需要单独下一份。
    if not shallow_only:
        report["artifacts"] = _consume_artifacts(origin, obsutil, instance_id)
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="输出分析 worker（独立进程，分级 + 明细回填 platform.db）")
    ap.add_argument("--once", action="store_true", help="扫一遍即退出（调试/CI）")
    ap.add_argument("--interval", type=int, default=WORKER_INTERVAL,
                    help=f"常驻轮询间隔秒（默认 {DEFAULT_INTERVAL}）")
    ap.add_argument("--instance", default=None, help="只处理指定 instance_id（调试）")
    ap.add_argument("--deep-only", action="store_true",
                    help="（历史名，等同 --artifacts-only）只跑产物下载队列")
    ap.add_argument("--shallow-only", action="store_true",
                    help="只跑浅层分级/补全（不下载产物）")
    ap.add_argument("--artifacts-only", action="store_true",
                    help="只跑沙箱产物全量下载（不碰分级；会话详情也靠它）")
    ap.add_argument("--realtime-only", action="store_true",
                    help="只跑实时下载：仅处理用户点击过（priority>=阈值）的任务，不做发现")
    ap.add_argument("--cache-dir", dest="origin", default=oa.DEFAULT_OUTPUT_CACHE,
                    help=f"本地缓存根（默认 {oa.DEFAULT_OUTPUT_CACHE}）")
    ap.add_argument("--obsutil", default=oa.DEFAULT_OBSUTIL)
    ap.add_argument("--stale-after", type=int, default=STALE_AFTER_SECONDS,
                    help="(已废弃) 浅层只做一次，不再按此阈值 stale 重算")
    a = ap.parse_args()

    origin = os.path.abspath(a.origin)
    os.makedirs(origin, exist_ok=True)

    if a.realtime_only:
        print(f"[worker] 只跑实时下载（用户点击优先）: origin={origin} "
              f"interval={ARTIFACT_REALTIME_INTERVAL}s "
              f"concurrency={ARTIFACT_REALTIME_CONCURRENCY}", flush=True)
    elif a.deep_only or a.artifacts_only:
        print(f"[worker] 只跑沙箱产物下载队列: origin={origin}", flush=True)
    elif a.shallow_only:
        print(f"[worker] 只跑浅层分级/补全: origin={origin}", flush=True)
    else:
        print(f"[worker] 输出分析 worker 启动: interval={a.interval}s origin={origin}", flush=True)

    _write_worker_started(_worker_mode(a.deep_only, a.shallow_only,
                                       a.artifacts_only, a.realtime_only),
                          os.getpid(), f"started interval={a.interval}s")

    # 轻量自愈：补 task_traj_records / shallow_queue 的新增可选列 + 业务索引。
    # worker 是独立进程，不调用 init_db()，只靠 uvicorn 启动时迁移。新增一列
    # 后如果只重启 worker，SQL 会直接报 no such column，症状是全轮失败、
    # 日志只有一句笼统错误。shallow_queue 的 artifact_state 维尤其关键 ——
    # 没有它整个产物队列都领不出来。
    ensure_traj_extra_columns()
    ensure_shallow_extra_columns()

    while True:
        t0 = time.time()
        mode = _worker_mode(a.deep_only, a.shallow_only, a.artifacts_only, a.realtime_only)
        # 必须先初始化：异常路径下面还要读 rep["artifacts"]，未赋值会 UnboundLocalError
        # 把整个进程带崩（实测 deep worker 直接退出，线上只剩 shallow 在跑）。
        rep: dict = {}
        try:
            rep = _run_once(a.obsutil, origin, a.instance, a.deep_only,
                            a.shallow_only, a.artifacts_only, a.realtime_only)
            processed = (rep["shallow"]["n_processed"] + rep["deep"]["n_done"]
                         + rep["artifacts"]["n_done"] + rep["artifacts"]["n_discovered"])
            failed = (rep["shallow"]["n_failed"] + rep["deep"]["n_failed"]
                      + rep["artifacts"]["n_failed"])
            note = (f"浅层新={rep['shallow']['n_processed']} "
                    f"深层done={rep['deep']['n_done']} "
                    f"产物发现={rep['artifacts']['n_discovered']} "
                    f"done={rep['artifacts']['n_done']}")
            _write_heartbeat(mode, os.getpid(), t0, time.time(), processed, failed, note)
            if processed or failed:
                print(f"[worker] 本轮: 浅层(新={rep['shallow']['n_processed']} "
                      f"fail={rep['shallow']['n_failed']}) | "
                      f"深层(done={rep['deep']['n_done']} fail={rep['deep']['n_failed']}) | "
                      f"产物(发现={rep['artifacts']['n_discovered']} "
                      f"done={rep['artifacts']['n_done']} fail={rep['artifacts']['n_failed']}) | "
                      f"{time.time()-t0:.1f}s", flush=True)
        except KeyboardInterrupt:
            print("[worker] 收到中断，退出", flush=True)
            return
        except Exception as e:
            print(f"[worker] 本轮异常: {e}", flush=True)
            _write_heartbeat(mode, os.getpid(), t0, time.time(), 0, 1,
                             f"本轮异常: {str(e)[:200]}")
        if a.once:
            break
        # 实时 worker 用自己更短的间隔（默认 2s）：它只处理用户点击的任务，
        # 队列短、响应要快，不能沿用积压 worker 的 60s 退避。
        if a.realtime_only:
            time.sleep(max(0.5, ARTIFACT_REALTIME_INTERVAL))
            continue
        # 队列还有活就不睡：每轮只领 ARTIFACT_CONCURRENCY 条，固定 sleep(interval)
        # 会让 93% 的时间空转（实测每轮 3.5s 干活 + 60s 睡眠 = 171 条/小时，
        # 而单 task 只要 1.3s）。只在队列真的空了才按 interval 退避。
        # 注意用小睡而不是完全不睡：连续失败的行会被反复领取，
        # 完全不睡会变成忙等打爆 CPU 和 OBS 配额。
        if (rep.get("artifacts") or {}).get("n_remaining", 0) > 0:
            time.sleep(1)
        else:
            time.sleep(max(1, a.interval))


if __name__ == "__main__":
    main()
