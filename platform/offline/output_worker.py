# -*- coding: utf-8 -*-
"""输出分析 worker：独立进程，把 task_records 的轨迹分级 + 轨迹明细回填进 platform.db。

数据流（README_outputview.md §二）：
  浅层（分级，常驻循环）：
    task_records 中 traj_level IS NULL 的行 → 从 config_path 解析 OBS 轨迹根（logs.py:_get_obs_base_path
    同款）→ _load_per_task_entries 快路径（tsr 陈旧自动回退慢路径）→ compute_level（dropped 归 L0）
    → 回填 task_records.traj_level + UPSERT task_traj_records（status='done'）。
    stale 回收：traj_level 已回填但 updated_at 超过 10min 的行 → 重设 traj_level=NULL，重入队。
  深层（按需，仅用户点开会话详情/轨迹/日志时经 API 置 status='pending' 触发）：
    status='pending' → download_task_detail + load_task_detail → 回填 5 个路径列 → done | failed。

⚠ 关键设计约束：worker 写 task_records 的 UPDATE 不得触碰 updated_at。
task_records.updated_at 语义 =「在线 _sync_task_records 最后写入时间」（instances.py:1331 每次 UPSERT
都 CURRENT_TIMESTAMP）；stale 判定依赖它。若 worker 分级时也刷 updated_at，在线 8s 刷新会持续
把已回填行「更新」成永远新鲜 → 分级永不 stale 重算。故：
  - 分级回填：UPDATE ... SET traj_level=?  WHERE instance_id=? AND config_name=?（不写 updated_at）
  - 重入队：  UPDATE ... SET traj_level=NULL   （同样不写 updated_at，避免给自己制造 10min 倒计时）

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
import sys
import time

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from src import offline_analysis as oa            # noqa: E402
from api.core.config import settings               # noqa: E402
from api.core.database import get_connection       # noqa: E402

# 默认常驻轮询间隔（秒）
DEFAULT_INTERVAL = 60
# 分级 stale 阈值：traj_level 已回填但超过该时长的行重设 NULL 重入队（OBS 侧可能更新了轨迹）
STALE_AFTER_SECONDS = 600


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
    """浅层队列来源：running/preparing 的实例（其 task_records 由在线 _sync_task_records 维护）。"""
    rows = conn.execute(
        "SELECT id, name, config_path FROM task_instances "
        "WHERE status IN ('running','preparing') ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


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
    """枚举该实例 task_records 的（traj_name 可用）行。返回 dict 列表。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT task_idx, config_name, traj_level, updated_at "
            "FROM task_records WHERE instance_id=? ORDER BY task_idx",
            (inst["id"],),
        ).fetchall()
    return [dict(r) for r in rows]


def _upsert_traj_record(inst: dict, tr: dict, task_idx, config_name,
                        level: str, harness: str, ps: dict, traj_rel: str | None,
                        status: str = "done", error: str | None = None,
                        traj_name: str | None = None) -> None:
    """task_traj_records UPSERT（ON CONFLICT 更新分级列，不动 status/路径列）。

    tr 为 task_records 行或 per-task entry（traj_name 从中取，缺列时回退 config_name
    leaf）；ps 为 per-task entry（keys: task/tool_calls/plain_rounds/passed_gate/has_eval/
    evaluator_completion/task_done/…），evaluator_completion 落 completion 列。
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
                 char_len, trajectory_rel, status, error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
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
             ps.get("char_len"), traj_rel, status, error),
        )


def _apply_level_to_task_records(inst: dict, config_name: str, level: str) -> None:
    """回填 task_records.traj_level——不触碰 updated_at（见模块 docstring 的 ⚠ 约束）。"""
    with get_connection() as conn:
        conn.execute(
            "UPDATE task_records SET traj_level=? WHERE instance_id=? AND config_name=?",
            (level, inst["id"], config_name),
        )


def _reset_stale_levels(inst: dict, stale_after: float) -> int:
    """stale 回收：traj_level 已回填但 updated_at 超阈值 → 重设 NULL 重入队。不触碰 updated_at。"""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE task_records SET traj_level=NULL "
            "WHERE instance_id=? AND traj_level IS NOT NULL "
            "AND updated_at < datetime('now', ?)",
            (inst["id"], f"-{int(stale_after)} seconds"),
        )
        return cur.rowcount or 0


def _process_instance(inst: dict, origin: str, stale_after: float,
                      obsutil: str) -> dict:
    """处理一个实例的浅层队列：分级 task_records 未回填行 + stale 回收。"""
    n_reset = _reset_stale_levels(inst, stale_after)
    rows = _level_rows_for_instance(inst)
    pending = [r for r in rows if not r["traj_level"]]
    if not pending:
        return {"n_reset": n_reset, "n_processed": 0, "n_failed": 0}

    obs_base = _make_obs_base_for(inst)
    # 已按 task_idx 排序；用 OBS 目录枚举校对 config_name ↔ traj_name（config stem 可能 ≠ task 目录名）
    tnames = {}
    try:
        for t in oa.list_task_dirs(obsutil, obs_base):
            tnames[t.rstrip("/").rsplit("/", 1)[-1]] = t
    except Exception as e:
        print(f"    [warn] {inst['id']} 枚举 OBS task 目录失败: {e}", flush=True)

    n_ok = n_fail = 0
    for r in pending:
        config_name = r["config_name"]
        # 确定 task OBS 目录：优先按 traj 名（OBS 侧 leaf == config stem 的概率高）
        stem = os.path.splitext(config_name)[0]
        task_obs = None
        if stem in tnames:
            task_obs = tnames[stem]
        elif config_name in tnames:
            task_obs = tnames[config_name]
        if not task_obs:
            # OBS 侧无对应 task 目录 → 任务失败态：标 failed 级占位，不反复重试
            _apply_level_to_task_records(inst, config_name, "failed")
            continue
        try:
            entries = oa._load_per_task_entries(obsutil, task_obs, origin)
            if not entries:
                _apply_level_to_task_records(inst, config_name, "failed")
                continue
            # 每 task 通常只有 1 条 entry（assistant 主轨迹）；多条时逐条落，等级按第一条
            entry = entries[0]
            level = oa.compute_level(entry.get("harness", "openclaw"),
                                     entry.get("tool_calls", 0),
                                     entry.get("plain_rounds", 0),
                                     entry.get("evaluator_completion"))
            # task_records 与 task_traj_records 同步落（task_records 的 traj_level 仍可能为 NULL，
            # 因在线 _sync_task_records 用 COALESCE 保留旧值，worker 是唯一写 traj_level 的地方）
            _apply_level_to_task_records(inst, config_name, level)
            for e in entries:
                # entry 不含 traj_name/config_name 列（只有 task=leaf），显式传入 leaf
                leaf = os.path.basename(str(e.get("task") or "")).rstrip("/") or stem
                _upsert_traj_record(inst, e, r.get("task_idx"), config_name,
                                    level, e.get("harness", "openclaw"),
                                    e, e.get("trajectory"), traj_name=leaf)
            n_ok += 1
        except Exception as e:
            n_fail += 1
            print(f"    [fail] {inst['id']} {config_name}: {e}", flush=True)
    return {"n_reset": n_reset, "n_processed": n_ok, "n_failed": n_fail}


# ============ 深层：pending → 下载/解析 → 回填路径列 ============

def _consume_deep(origin: str, obsutil: str, instance_id: str | None = None) -> dict:
    """消费全部 pending 行。返回 {n_done, n_failed}。"""
    with get_connection() as conn:
        rows = _pick_pending_rows(conn, instance_id)
        inst_cache: dict[str, dict] = {}

        n_done = n_fail = 0
        for tr in rows:
            inst = inst_cache.get(tr["instance_id"])
            if inst is None:
                inst = _instance_by_id(conn, tr["instance_id"])
                if inst:
                    inst_cache[tr["instance_id"]] = inst
            if not inst:
                # 实例已删：直接收尾，避免永久残留 pending
                conn.execute(
                    "UPDATE task_traj_records SET status='failed', error='实例不存在', "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (tr["id"],),
                )
                n_fail += 1
                continue
            # 标记 downloading（防多 worker 重复消费；本进程单线程，常驻轮询幂等）
            conn.execute(
                "UPDATE task_traj_records SET status='downloading', updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='pending'",
                (tr["id"],),
            )
            conn.commit()
            try:
                obs_base = _make_obs_base_for(inst)
                traj_name = tr.get("traj_name") or tr.get("config_name") or ""
                if "/" in traj_name:
                    traj_name = traj_name.rstrip("/").rsplit("/", 1)[-1]
                task_obs = obs_base.rstrip("/") + "/" + traj_name + "/"
                # 预检 OBS 目录存在性：不存在（轨迹已清理/路径不符）直接判 failed，
                # 避免每次轮询都下载空目录、永久 pending/重试
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

                conn.execute(
                    "UPDATE task_traj_records SET "
                    "status='done', error=NULL, updated_at=CURRENT_TIMESTAMP, "
                    "assistant_traj_path=?, evaluator_traj_path=?, "
                    "task_log_path=?, gateway_log_path=?, eval_log_path=? "
                    "WHERE id=?",
                    (traj_paths.get("assistant"), traj_paths.get("evaluator"),
                     log_path, gw_path, ev_path, tr["id"]),
                )
                conn.commit()
                n_done += 1
            except Exception as e:
                conn.execute(
                    "UPDATE task_traj_records SET status='failed', error=?, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (str(e)[:500], tr["id"]),
                )
                conn.commit()
                n_fail += 1
        return {"n_done": n_done, "n_failed": n_fail}


# ============ 主循环 ============

def _run_once(obsutil: str, origin: str, stale_after: float,
              instance_id: str | None, deep_only: bool) -> dict:
    report: dict = {"shallow": {"n_reset": 0, "n_processed": 0, "n_failed": 0},
                    "deep": {"n_done": 0, "n_failed": 0}}

    if not deep_only:
        with get_connection() as conn:
            insts = _pick_running_instances(conn)
        for inst in insts:
            if instance_id and inst["id"] != instance_id:
                continue
            try:
                r = _process_instance(inst, origin, stale_after, obsutil)
            except Exception as e:
                r = {"n_reset": 0, "n_processed": 0, "n_failed": 0}
                print(f"    [fail] 实例 {inst['id']} 分级失败: {e}", flush=True)
            for k in ("n_reset", "n_processed", "n_failed"):
                report["shallow"][k] += r[k]
            if r["n_processed"] or r["n_failed"] or r["n_reset"]:
                print(f"  [shallow] {inst['id']} 新分级={r['n_processed']} "
                      f"stale重入={r['n_reset']} 失败={r['n_failed']}", flush=True)

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
    ap.add_argument("--cache-dir", dest="origin", default=oa.DEFAULT_OUTPUT_CACHE,
                    help=f"本地缓存根（默认 {oa.DEFAULT_OUTPUT_CACHE}）")
    ap.add_argument("--obsutil", default=oa.DEFAULT_OBSUTIL)
    ap.add_argument("--stale-after", type=int, default=STALE_AFTER_SECONDS,
                    help="分级 stale 阈值秒（默认 600）")
    a = ap.parse_args()

    origin = os.path.abspath(a.origin)
    os.makedirs(origin, exist_ok=True)

    if a.deep_only:
        print(f"[worker] 只消费深层队列: origin={origin}", flush=True)
    else:
        print(f"[worker] 输出分析 worker 启动: interval={a.interval}s stale_after={a.stale_after}s "
              f"origin={origin}", flush=True)

    while True:
        t0 = time.time()
        try:
            rep = _run_once(a.obsutil, origin, a.stale_after, a.instance, a.deep_only)
            if rep["shallow"]["n_processed"] or rep["shallow"]["n_failed"] or rep["shallow"]["n_reset"] \
                    or rep["deep"]["n_done"] or rep["deep"]["n_failed"]:
                print(f"[worker] 本轮: 浅层(新={rep['shallow']['n_processed']} "
                      f"stale={rep['shallow']['n_reset']} fail={rep['shallow']['n_failed']}) | "
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
