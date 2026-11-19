"""
批量实验（Batch Experiment）路由

一次实验 = 同一测试集 × N 个 Harness（× 重复次数），用于横向评测对比。

**执行引擎完全复用**：一个 task_instances 本来就是「一个 Harness 跑完整个测试集」
（run_config.task.task_input_path 下每个 json 一条样本），所以批量实验只做三件事：
  1. 批量建子任务（直接调用 instances.create_instance，不复制其逻辑）
  2. 把它们放进同一个队列，交给现有的 queue_scheduler 调度
  3. 聚合对比（数据全部已落库，不新增采集）

子任务创建后与手工创建的任务**完全无差别**：同样的详情页、日志、轨迹、产物。
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.database import get_connection, async_query, async_db_execute
from ..core.security import get_current_user, require_operator
from ..models.batch_experiment import (
    BatchExperimentCreate, BatchExperimentInfo, BatchItemInfo,
    BatchCompareRow, BatchMatrixResponse, BatchMatrixRow,
)
from ..models.instance import InstanceCreate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/batch-experiments", tags=["batch-experiments"])

# 子任务已到终态（不再占用队列槽位、可以计入「已完成臂数」）的状态集合。
# 与 instances 的状态机一致：completed/finished 是正常跑完，stopped 是用户停的，
# failed 是准备/启动失败。
_TERMINAL_STATUSES = ("completed", "finished", "stopped", "failed")

_SCHEDULE_MODES = ("shared", "isolated", "serial")

# 总任务数的安全上限。超过时拒绝创建而不是默默建出几万个实例 ——
# 每个子任务都要写配置文件、下载 OBS、拉沙箱，建错了清理成本极高。
_MAX_TOTAL_TASKS = 100000


# ============================================================================
# 工具
# ============================================================================

def _strip_bucket(path: str) -> str:
    """把 obs://bucket/a/b 或 bucket/a/b 归一成 a/b。

    与前端 stripBucket 行为对齐：实例的 user_config_dir 存的是不带桶前缀的
    相对路径（create_instance 里会自己拼 base.s3.bucket_name）。登记表里的
    task_path_obs 可能带 obs:// 前缀，直接灌进去会变成双重桶名。
    """
    p = str(path or "").strip()
    if p.startswith("obs://"):
        p = p[len("obs://"):]
        # 去掉桶名那一段
        parts = p.split("/", 1)
        p = parts[1] if len(parts) > 1 else ""
    return p.strip("/")


def _gen_batch_id(conn) -> str:
    """Batch-YYYYMMDD-NNN，NNN 是当日序号。

    在调用方的事务里取号：并发创建时靠 SQLite 写锁串行化，不会撞号。
    """
    day = datetime.now().strftime("%Y%m%d")
    prefix = f"Batch-{day}-"
    row = conn.execute(
        "SELECT id FROM batch_experiments WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()
    nxt = 1
    if row:
        try:
            nxt = int(str(row["id"])[len(prefix):]) + 1
        except (ValueError, TypeError):
            nxt = 1
    return f"{prefix}{nxt:03d}"


def _mask_secret(v: str) -> str:
    """与 registrations._mask_api_key 同款脱敏规则。"""
    s = str(v or "")
    if not s:
        return ""
    return (s[:4] + "****" + s[-4:]) if len(s) > 8 else "****"


def _masked_params(common: dict) -> dict:
    """公共参数入库前脱敏。真实 key 只在创建瞬间透传给 create_instance。"""
    d = json.loads(json.dumps(common))  # 深拷贝，别改到调用方的对象
    if d.get("model_api_key"):
        d["model_api_key"] = _mask_secret(d["model_api_key"])
    for ag in d.get("agents") or []:
        if isinstance(ag, dict) and ag.get("api_key"):
            ag["api_key"] = _mask_secret(ag["api_key"])
    return d


def _duration_seconds(started_at, stopped_at) -> Optional[float]:
    """运行时长（秒）。与 queues._duration_seconds 同款解析，脏数据返回 None。"""
    if not started_at or not stopped_at:
        return None
    try:
        a = datetime.fromisoformat(str(started_at).replace(" ", "T"))
        b = datetime.fromisoformat(str(stopped_at).replace(" ", "T"))
    except (ValueError, TypeError):
        return None
    sec = (b - a).total_seconds()
    return sec if sec >= 0 else None


def _done_map(instance_ids: list[str], conn) -> dict:
    """实例 → DONE 数（agent 自己打的【Task_Done】标记）。

    **done_tasks 不是 task_instances 的列**，它是派生值：本地日志口径
    （task_records.task_done）优先，该实例还没被扫过时回退 OBS 口径
    （task_traj_records.task_done）。这套回退规则与首页/详情页完全一致，
    直接复用 instances 的两个 helper，不另写 SQL——否则批量实验页算出的
    成功率会和任务详情页对不上。
    """
    from .instances import _done_counts, _task_done_counts
    ids = [i for i in instance_ids if i]
    if not ids:
        return {}
    obs = _task_done_counts(ids, conn)
    local = _done_counts(ids, conn)
    return {i: local.get(i, obs.get(i, 0)) for i in ids}


def _aggregate_rows(batch_ids: list[str]) -> dict:
    """一条 SQL 聚合出多个实验的进度，避免列表页 N+1。

    LEFT JOIN：子任务创建失败（instance_id IS NULL）的 item 也要算进 arm_count，
    否则「建了 3 个臂、挂了 1 个」在列表上看起来只有 2 个臂，用户不会发现有失败。

    DONE 数单独算（见 _done_map）：它是派生值，不在 task_instances 表上。
    """
    if not batch_ids:
        return {}
    ph = ",".join("?" * len(batch_ids))
    term_ph = ",".join("?" * len(_TERMINAL_STATUSES))
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT i.batch_id,
                       COUNT(*)                                   AS arm_count,
                       COALESCE(SUM(t.total_tasks), 0)            AS task_total,
                       COALESCE(SUM(t.completed_tasks), 0)        AS completed,
                       COALESCE(SUM(t.failed_tasks), 0)           AS failed,
                       SUM(CASE WHEN t.status = 'running'   THEN 1 ELSE 0 END) AS running,
                       SUM(CASE WHEN t.status = 'queued'    THEN 1 ELSE 0 END) AS queued,
                       SUM(CASE WHEN t.status IN ({term_ph})  THEN 1 ELSE 0 END) AS finished_arms
                  FROM batch_experiment_items i
                  LEFT JOIN task_instances t ON t.id = i.instance_id
                 WHERE i.batch_id IN ({ph})
                 GROUP BY i.batch_id""",
            (*_TERMINAL_STATUSES, *batch_ids),
        ).fetchall()
        out = {r["batch_id"]: dict(r) for r in rows}

        # DONE 数：按实验把子任务 id 归堆后一次性算，仍然是 O(1) 次查询
        links = conn.execute(
            f"""SELECT batch_id, instance_id FROM batch_experiment_items
                 WHERE batch_id IN ({ph}) AND instance_id IS NOT NULL""",
            batch_ids,
        ).fetchall()
        done = _done_map([l["instance_id"] for l in links], conn)
        for l in links:
            d = out.setdefault(l["batch_id"], {})
            d["done"] = d.get("done", 0) + done.get(l["instance_id"], 0)
    return out


def _build_info(row: dict, agg: dict) -> BatchExperimentInfo:
    a = agg.get(row["id"], {})
    task_total = a.get("task_total", 0) or 0
    completed = a.get("completed", 0) or 0
    failed = a.get("failed", 0) or 0
    done = a.get("done", 0) or 0
    info = BatchExperimentInfo(**row)
    info.arm_count = a.get("arm_count", 0) or 0
    info.task_total = task_total
    info.completed = completed
    info.failed = failed
    info.done = done
    info.running = a.get("running", 0) or 0
    info.queued = a.get("queued", 0) or 0
    info.finished_arms = a.get("finished_arms", 0) or 0
    # 成功率用 DONE 口径（agent 自己打的【Task_Done】标记），分母是「已跑完的」
    # 而不是 total_tasks —— 否则实验刚开始时成功率会从 0 慢慢爬，看起来像全挂了。
    settled = completed + failed
    info.success_rate = round(done / settled * 100, 1) if settled else 0.0
    info.progress_percent = round(settled / task_total * 100, 1) if task_total else 0.0
    return info


def _get_batch(batch_id: str) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM batch_experiments WHERE id = ?", (batch_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实验不存在")
    return dict(row)


def _batch_instance_ids(batch_id: str) -> list[str]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT instance_id FROM batch_experiment_items "
            " WHERE batch_id = ? AND instance_id IS NOT NULL",
            (batch_id,),
        ).fetchall()
    return [r["instance_id"] for r in rows]


# ============================================================================
# 列表 / 详情
# ============================================================================

@router.get("", response_model=list[BatchExperimentInfo])
def list_experiments(
    status: Optional[str] = Query(None, description="created|running|paused|completed|cancelled"),
    keyword: Optional[str] = Query(None, description="按实验名称 / ID 模糊搜索"),
    user: dict = Depends(get_current_user),
):
    sql = "SELECT * FROM batch_experiments"
    conds, params = [], []
    if status:
        conds.append("status = ?")
        params.append(status)
    if keyword:
        conds.append("(name LIKE ? OR id LIKE ?)")
        params += [f"%{keyword}%", f"%{keyword}%"]
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY created_at DESC"

    with get_connection() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    agg = _aggregate_rows([r["id"] for r in rows])
    return [_build_info(r, agg) for r in rows]


@router.get("/{batch_id}", response_model=BatchExperimentInfo)
def get_experiment(batch_id: str, user: dict = Depends(get_current_user)):
    row = _get_batch(batch_id)
    return _build_info(row, _aggregate_rows([batch_id]))


@router.get("/{batch_id}/items", response_model=list[BatchItemInfo])
def list_items(batch_id: str, user: dict = Depends(get_current_user)):
    """各臂进度（图3 下半）+ 任务列表 tab 的数据源。"""
    _get_batch(batch_id)
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT i.*,
                      t.name AS instance_name, t.status, t.total_tasks,
                      t.completed_tasks, t.failed_tasks,
                      t.queue_id, t.started_at, t.stopped_at
                 FROM batch_experiment_items i
                 LEFT JOIN task_instances t ON t.id = i.instance_id
                WHERE i.batch_id = ?
                ORDER BY i.id""",
            (batch_id,),
        ).fetchall()
        # done_tasks 是派生值（见 _done_map），不在表上
        done = _done_map([r["instance_id"] for r in rows], conn)

    out = []
    for r in rows:
        d = dict(r)
        # LEFT JOIN 没命中时这些列是 None，Pydantic 声明的是 int，要兜住
        for k in ("total_tasks", "completed_tasks", "failed_tasks"):
            d[k] = d.get(k) or 0
        d["done_tasks"] = done.get(d.get("instance_id"), 0)
        d["instance_name"] = d.get("instance_name") or ""
        d["status"] = d.get("status") or ""
        total = d["total_tasks"]
        settled = d["completed_tasks"] + d["failed_tasks"]
        d["progress_percent"] = round(settled / total * 100, 1) if total else 0.0
        out.append(BatchItemInfo(**d))
    return out


# ============================================================================
# 创建
# ============================================================================

@router.post("")
async def create_experiment(req: BatchExperimentCreate, user: dict = Depends(require_operator)):
    """创建实验 + 批量建子任务。

    子任务创建**顺序 await，不并发**：create_instance 内部多次 get_connection()
    写库，而 platform.db 近 10GB，WAL checkpoint 期间独占写锁。asyncio.gather
    并发建会成片撞 `database is locked`（见 queue_scheduler 的锁重试注释）。

    单个子任务失败不整体回滚：异常写进 items.create_error 继续建其余的，
    返回体带 created/failed 计数，由前端提示用户。N 可能上百，
    让一个 OBS 路径打错就废掉整次实验是不合理的。
    """
    if req.schedule_mode not in _SCHEDULE_MODES:
        raise HTTPException(status_code=400, detail=f"未知的调度策略: {req.schedule_mode}")
    if not req.arms:
        raise HTTPException(status_code=400, detail="至少选择一个 Harness")
    if req.repeat_times < 1:
        raise HTTPException(status_code=400, detail="重复次数至少为 1")

    # --- 测试集：取自任务登记 ---
    with get_connection() as conn:
        reg = conn.execute(
            "SELECT * FROM task_registrations WHERE id = ?", (req.registration_id,)
        ).fetchone()
    if not reg:
        raise HTTPException(status_code=404, detail="测试集（任务登记）不存在")
    reg = dict(reg)

    test_set_path = _strip_bucket(reg.get("task_path_obs"))
    if not test_set_path:
        raise HTTPException(
            status_code=400,
            detail=f"该登记未填写任务路径（task_path_obs），无法作为测试集使用",
        )
    sample_total = int(reg.get("data_total") or 0)

    total_tasks = len(req.arms) * req.repeat_times * max(sample_total, 0)
    if total_tasks > _MAX_TOTAL_TASKS:
        raise HTTPException(
            status_code=400,
            detail=f"总任务数 {total_tasks} 超过上限 {_MAX_TOTAL_TASKS}，请减少 Harness 数或重复次数",
        )

    common = req.common.model_dump()

    # --- 建实验行 + 队列 ---
    with get_connection() as conn:
        batch_id = _gen_batch_id(conn)

        # 调度策略 → 队列。shared/serial 共用一个队列，isolated 每臂一个。
        # serial 的 max_concurrent=1 即严格串行（与 queue 的默认语义一致）。
        shared_queue_id = None
        if req.schedule_mode in ("shared", "serial"):
            shared_queue_id = f"batch-{batch_id}".lower()
            conn.execute(
                "INSERT OR IGNORE INTO task_queues "
                "(id, name, max_concurrent, description, created_by) VALUES (?,?,?,?,?)",
                (shared_queue_id, f"{req.name}",
                 1 if req.schedule_mode == "serial" else max(1, req.max_concurrent),
                 f"批量实验 {batch_id} 自动创建", user.get("username")),
            )

        conn.execute(
            """INSERT INTO batch_experiments
               (id, name, description, registration_id, test_set_path, test_set_name,
                sample_total, repeat_times, schedule_mode, queue_id, max_concurrent,
                status, params_json, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (batch_id, req.name, req.description, req.registration_id,
             test_set_path, reg.get("task_name") or "", sample_total,
             req.repeat_times, req.schedule_mode, shared_queue_id,
             max(1, req.max_concurrent), "created",
             json.dumps(_masked_params(common), ensure_ascii=False),
             user.get("username")),
        )

    # --- 逐个建子任务 ---
    from .instances import create_instance  # 延迟导入，避免循环依赖

    created, failed = 0, 0
    for arm in req.arms:
        for rep in range(1, req.repeat_times + 1):
            label = arm.label or arm.harness_type
            suffix = f"-r{rep}" if req.repeat_times > 1 else ""

            # isolated：每个臂一条独立队列，彼此不互相阻塞
            if req.schedule_mode == "isolated":
                qid = f"batch-{batch_id}-{arm.harness_type}{suffix}".lower()
                with get_connection() as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_queues "
                        "(id, name, max_concurrent, description, created_by) VALUES (?,?,?,?,?)",
                        (qid, f"{req.name} · {label}", max(1, req.max_concurrent),
                         f"批量实验 {batch_id} 自动创建", user.get("username")),
                    )
            else:
                qid = shared_queue_id

            # task_name 用于 Pod 命名和 OBS 路径，只能是字母数字；
            # batch id 里的 '-' 保留，harness_type 里可能有 '-' 也无妨。
            task_name = f"{batch_id}{suffix}-{arm.harness_type}".replace("_", "-").lower()

            payload = dict(common)
            payload.update({
                "name": f"{req.name} · {label}{suffix}",
                "task_name": task_name,
                "harness_type": arm.harness_type,
                "harness_config_id": arm.harness_config_id,
                "harness_config_version_id": arm.harness_config_version_id,
                # 测试集由实验决定，不接受前端传入，避免与所选登记冲突
                "user_config_dir": test_set_path,
                "total_num": sample_total,
                "queue_id": qid,
            })

            instance_id, err = None, None
            try:
                inst = await create_instance(InstanceCreate(**payload), user)
                instance_id = inst.id
                created += 1
            except HTTPException as e:
                err = f"{e.status_code}: {e.detail}"
                failed += 1
                logger.warning(f"[{batch_id}] 子任务创建失败 ({label}): {err}")
            except Exception as e:
                err = str(e)[:500]
                failed += 1
                logger.exception(f"[{batch_id}] 子任务创建异常 ({label})")

            with get_connection() as conn:
                conn.execute(
                    """INSERT INTO batch_experiment_items
                       (batch_id, instance_id, harness_type, harness_config_id,
                        harness_config_version_id, arm_label, repeat_idx, create_error)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (batch_id, instance_id, arm.harness_type, arm.harness_config_id,
                     arm.harness_config_version_id, label, rep, err),
                )

    row = _get_batch(batch_id)
    return {
        "experiment": _build_info(row, _aggregate_rows([batch_id])),
        "created": created,
        "failed": failed,
        "message": (f"已创建 {created} 个子任务" if not failed
                    else f"已创建 {created} 个子任务，{failed} 个失败（见任务列表）"),
    }


# ============================================================================
# 暂停 / 恢复 / 取消 / 删除
# ============================================================================

def _experiment_queue_ids(batch_id: str) -> list[str]:
    """该实验涉及的所有队列 id（isolated 模式下有多个）。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT t.queue_id FROM batch_experiment_items i "
            " JOIN task_instances t ON t.id = i.instance_id "
            " WHERE i.batch_id = ? AND t.queue_id IS NOT NULL",
            (batch_id,),
        ).fetchall()
    return [r["queue_id"] for r in rows]


@router.post("/{batch_id}/pause")
async def pause_experiment(batch_id: str, user: dict = Depends(require_operator)):
    """暂停调度：队列不再放行新任务，**已 running 的不打断**。"""
    _get_batch(batch_id)
    qids = _experiment_queue_ids(batch_id)
    if qids:
        ph = ",".join("?" * len(qids))
        with get_connection() as conn:
            conn.execute(f"UPDATE task_queues SET paused = 1 WHERE id IN ({ph})", qids)
    with get_connection() as conn:
        conn.execute("UPDATE batch_experiments SET status='paused' WHERE id=?", (batch_id,))
    return {"message": "已暂停调度，运行中的任务不受影响", "queues": qids}


@router.post("/{batch_id}/resume")
async def resume_experiment(batch_id: str, user: dict = Depends(require_operator)):
    """恢复调度：清 paused 位并立即触发一次调度（否则要等下一个事件）。"""
    _get_batch(batch_id)
    qids = _experiment_queue_ids(batch_id)
    if qids:
        ph = ",".join("?" * len(qids))
        with get_connection() as conn:
            conn.execute(f"UPDATE task_queues SET paused = 0 WHERE id IN ({ph})", qids)

    from ..services.queue_scheduler import schedule_queue
    for qid in qids:
        asyncio.create_task(schedule_queue(qid))

    with get_connection() as conn:
        conn.execute("UPDATE batch_experiments SET status='running' WHERE id=?", (batch_id,))
    return {"message": "已恢复调度", "queues": qids}


@router.post("/{batch_id}/cancel")
async def cancel_experiment(batch_id: str, user: dict = Depends(require_operator)):
    """取消实验：停掉所有非终态子任务，队列置暂停避免又被放行。"""
    _get_batch(batch_id)

    qids = _experiment_queue_ids(batch_id)
    if qids:
        ph = ",".join("?" * len(qids))
        with get_connection() as conn:
            conn.execute(f"UPDATE task_queues SET paused = 1 WHERE id IN ({ph})", qids)

    term_ph = ",".join("?" * len(_TERMINAL_STATUSES))
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT t.id, t.status FROM batch_experiment_items i
                  JOIN task_instances t ON t.id = i.instance_id
                 WHERE i.batch_id = ? AND t.status NOT IN ({term_ph})""",
            (batch_id, *_TERMINAL_STATUSES),
        ).fetchall()

    from .instances import stop_instance

    stopped, errors = 0, []
    for r in rows:
        try:
            await stop_instance(r["id"], user)
            stopped += 1
        except HTTPException as e:
            # 任务可能在这几毫秒里自己跑完了（stop 会 400）——不是错误，跳过
            errors.append(f"{r['id']}: {e.detail}")
        except Exception as e:
            errors.append(f"{r['id']}: {str(e)[:200]}")

    with get_connection() as conn:
        conn.execute(
            "UPDATE batch_experiments SET status='cancelled', finished_at=CURRENT_TIMESTAMP "
            " WHERE id=?", (batch_id,),
        )
    return {"message": f"已取消实验，停止 {stopped} 个任务", "stopped": stopped, "errors": errors}


@router.delete("/{batch_id}")
def delete_experiment(batch_id: str, user: dict = Depends(require_operator)):
    """删除实验记录。

    **只删实验与编组关系，子任务（task_instances）一律保留** —— 它们是真实跑过的
    任务，有日志、轨迹、产物，删实验不该连带销毁这些。要删任务请去任务详情页。
    """
    _get_batch(batch_id)
    with get_connection() as conn:
        conn.execute("DELETE FROM batch_experiment_items WHERE batch_id = ?", (batch_id,))
        conn.execute("DELETE FROM batch_experiments WHERE id = ?", (batch_id,))
    return {"message": "已删除实验（子任务保留）"}


# ============================================================================
# 结果对比
# ============================================================================

def _compare_rows(batch_id: str) -> list[BatchCompareRow]:
    """按臂聚合对比指标。一条 SQL，不在 Python 里循环查。

    评分口径：只对 eval_score 非空的行求均值，未评估的单独计数。
    **绝不能把未评估当 0 分** —— 浅层/质检 worker 没跑完时那会把均值拉到接近 0，
    看起来像模型全错了。
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT i.id AS item_id, i.instance_id, i.arm_label, i.harness_type,
                      t.total_tasks, t.completed_tasks, t.failed_tasks,
                      t.started_at, t.stopped_at,
                      (SELECT AVG(eval_score) FROM task_records
                        WHERE instance_id = t.id AND eval_score IS NOT NULL) AS avg_score,
                      (SELECT COUNT(*) FROM task_records
                        WHERE instance_id = t.id AND eval_score IS NOT NULL) AS scored_count,
                      (SELECT COUNT(*) FROM task_records
                        WHERE instance_id = t.id AND eval_score IS NULL)     AS unscored_count
                 FROM batch_experiment_items i
                 JOIN task_instances t ON t.id = i.instance_id
                WHERE i.batch_id = ?
                ORDER BY i.id""",
            (batch_id,),
        ).fetchall()
        done_map = _done_map([r["instance_id"] for r in rows], conn)

    out = []
    for r in rows:
        d = dict(r)
        total = d.get("total_tasks") or 0
        completed = d.get("completed_tasks") or 0
        failed = d.get("failed_tasks") or 0
        done = done_map.get(d.get("instance_id"), 0)
        settled = completed + failed

        elapsed = _duration_seconds(d.get("started_at"), d.get("stopped_at"))
        # 平均耗时的分母用「已结算的任务数」而非 total_tasks：实验跑到一半时
        # 用 total 当分母会把均值算得畸低。
        avg = (elapsed / settled) if (elapsed and settled) else None

        avg_score = d.get("avg_score")
        out.append(BatchCompareRow(
            item_id=d["item_id"],
            instance_id=d.get("instance_id"),
            arm_label=d.get("arm_label") or "",
            harness_type=d.get("harness_type") or "",
            total_tasks=total,
            completed_tasks=completed,
            failed_tasks=failed,
            done_tasks=done,
            completed_rate=round(completed / settled * 100, 1) if settled else 0.0,
            done_rate=round(done / settled * 100, 1) if settled else 0.0,
            avg_task_seconds=round(avg, 1) if avg is not None else None,
            elapsed_seconds=round(elapsed, 1) if elapsed is not None else None,
            avg_score=round(avg_score, 2) if avg_score is not None else None,
            scored_count=d.get("scored_count") or 0,
            unscored_count=d.get("unscored_count") or 0,
            # Token：本期不计算，前端渲染为 '—'。
            # 数据源 task_traj_records.total_tokens 已就绪，补的时候加个子查询即可。
            total_tokens=None,
        ))
    return out


@router.get("/{batch_id}/compare", response_model=list[BatchCompareRow])
def compare_experiment(batch_id: str, user: dict = Depends(get_current_user)):
    _get_batch(batch_id)
    return _compare_rows(batch_id)


@router.get("/{batch_id}/matrix", response_model=BatchMatrixResponse)
def matrix_experiment(
    batch_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: dict = Depends(get_current_user),
):
    """逐样本横向对比矩阵：行 = 样本（config_name），列 = 臂。

    **分页必须按 DISTINCT config_name 做**，不能直接 LIMIT 明细行：
    一个样本在 N 个臂下有 N 行，直接 LIMIT 会把同一个样本截断到两页，
    矩阵就会出现半行。所以先分页取本页的 config_name 集合，再取这些行的明细。
    """
    _get_batch(batch_id)
    inst_ids = _batch_instance_ids(batch_id)
    if not inst_ids:
        return BatchMatrixResponse(total=0, page=page, page_size=page_size,
                                   columns=[], rows=[])

    ph = ",".join("?" * len(inst_ids))
    offset = (page - 1) * page_size
    with get_connection() as conn:
        total = conn.execute(
            f"SELECT COUNT(DISTINCT config_name) AS n FROM task_records "
            f" WHERE instance_id IN ({ph})",
            inst_ids,
        ).fetchone()["n"] or 0

        names = [r["config_name"] for r in conn.execute(
            f"""SELECT DISTINCT config_name FROM task_records
                 WHERE instance_id IN ({ph})
                 ORDER BY config_name LIMIT ? OFFSET ?""",
            (*inst_ids, page_size, offset),
        ).fetchall()]

        cells = []
        if names:
            nph = ",".join("?" * len(names))
            cells = conn.execute(
                f"""SELECT config_name, instance_id, eval_score, traj_level,
                           task_done, status
                      FROM task_records
                     WHERE instance_id IN ({ph}) AND config_name IN ({nph})""",
                (*inst_ids, *names),
            ).fetchall()

    grouped: dict[str, dict] = {n: {} for n in names}
    for c in cells:
        grouped[c["config_name"]][c["instance_id"]] = {
            "instance_id": c["instance_id"],
            "eval_score": c["eval_score"],
            "traj_level": c["traj_level"],
            "task_done": c["task_done"],
            "status": c["status"],
        }

    return BatchMatrixResponse(
        total=total, page=page, page_size=page_size,
        columns=_compare_rows(batch_id),
        rows=[BatchMatrixRow(config_name=n, cells=grouped[n]) for n in names],
    )
