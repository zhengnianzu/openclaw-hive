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
    BatchCompareRow, BatchMatrixResponse, BatchMatrixRow, BatchArm,
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


def _mask_agents(agents) -> None:
    """就地脱敏一组 agent 的 api_key。"""
    for ag in agents or []:
        if isinstance(ag, dict) and ag.get("api_key"):
            ag["api_key"] = _mask_secret(ag["api_key"])


def _expand_blocks(req) -> list:
    """把矩阵的原始二维结构展开成一组臂（一个模型一个臂）。

    优先级：blocks > arms。blocks 为空时直接用旧客户端传来的 arms，保持兼容。
    块内模型列表为空 → 该 Harness 退化成一臂、吃 common 的兜底模型
    （label 用块的展示名，便于详情页区分）。
    """
    if not req.blocks:
        return req.arms

    out = []
    for b in req.blocks:
        block_label = b.label or b.harness_type
        if not b.models:
            out.append(BatchArm(
                harness_type=b.harness_type,
                harness_config_id=b.harness_config_id,
                harness_config_version_id=b.harness_config_version_id,
                label=block_label,
                image_name=b.image_name,
                sandbox_config_id=b.sandbox_config_id,
            ))
            continue
        for m in b.models:
            out.append(BatchArm(
                harness_type=b.harness_type,
                harness_config_id=b.harness_config_id,
                harness_config_version_id=b.harness_config_version_id,
                label=m.model_id or block_label,
                model_id=m.model_id,
                model_base_url=m.model_base_url,
                model_api_key=m.model_api_key,
                model_api_type=m.model_api_type,
                image_name=b.image_name,
                sandbox_config_id=b.sandbox_config_id,
            ))
    return out


def _masked_params(params: dict) -> dict:
    """实验参数入库前脱敏。真实 key 只在创建瞬间透传给 create_instance。

    key 有三个藏身处，漏一个就是明文落库：
      common.model_api_key / common.agents[].api_key
      arms[].model_api_key          （模型参数已按臂下沉）
      sim_arms[].agents[].api_key   （第二轴）
    """
    d = json.loads(json.dumps(params))  # 深拷贝，别改到调用方的对象
    if d.get("model_api_key"):
        d["model_api_key"] = _mask_secret(d["model_api_key"])
    _mask_agents(d.get("agents"))
    for arm in d.get("arms") or []:
        if isinstance(arm, dict) and arm.get("model_api_key"):
            arm["model_api_key"] = _mask_secret(arm["model_api_key"])
    for sim in d.get("sim_arms") or []:
        if isinstance(sim, dict):
            _mask_agents(sim.get("agents"))
    return d


def _sim_label(sim, sim_idx: int) -> str:
    """模拟臂的展示名。

    sim 为 None（第二轴退化）时返回空串 —— 调用方据此不拼后缀、不加 -s* 段，
    让单轴实验的 label / task_name 与改造前完全一致。
    用户没填 label 时按 agents 的名字拼，实在没有就用「模拟N」兜底。
    """
    if sim is None:
        return ""
    if (sim.label or "").strip():
        return sim.label.strip()
    names = [(a.name or "").strip() for a in (sim.agents or [])]
    names = [n for n in names if n]
    return "+".join(names) if names else f"模拟{sim_idx}"


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
                       SUM(CASE WHEN t.status IN ({term_ph})  THEN 1 ELSE 0 END) AS finished_arms,
                       -- 「启动队列」按钮据此决定可不可点
                       SUM(CASE WHEN t.status IN ('created','stopped') THEN 1 ELSE 0 END) AS startable_arms,
                       -- 实验终态由子任务推导（见 _derive_status）
                       SUM(CASE WHEN t.status = 'failed' THEN 1 ELSE 0 END) AS failed_arms,
                       SUM(CASE WHEN t.status IN ('running','preparing') THEN 1 ELSE 0 END) AS active_arms
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


def _derive_status(saved: str, info) -> str:
    """用子任务状态推导实验状态。

    库里的 batch_experiments.status 只在 pause/resume/cancel 三个动作时写入，
    跑完/全失败不会更新它 —— 所以显示口径必须现算。
    显式的人工状态（paused / cancelled）优先保留，不被推导覆盖。
    """
    if saved in ("paused", "cancelled"):
        return saved
    total = info.arm_count or 0
    if not total:
        return saved or "created"
    if info.active_arms > 0:
        return "running"
    if info.startable_arms > 0 and info.finished_arms == 0:
        return "created"          # 一个都还没启动
    if info.finished_arms + (info.failed_arms or 0) >= total:
        # 全部到终态：只要有失败就标 failed，否则 completed
        return "failed" if info.failed_arms else "completed"
    if info.finished_arms > 0:
        return "running"          # 跑了一部分，还有没开始的
    return saved or "created"


def _build_info(row: dict, agg: dict) -> BatchExperimentInfo:
    a = agg.get(row["id"], {})
    task_total = a.get("task_total", 0) or 0
    completed = a.get("completed", 0) or 0
    failed = a.get("failed", 0) or 0
    done = a.get("done", 0) or 0
    info = BatchExperimentInfo(**row)
    # 队列名/长尾跳过在 task_queues 上，由 _get_batch 联表带出
    info.queue_name = row.get("queue_name") or ""
    info.tail_skip_enabled = bool(row.get("tail_skip_enabled") or 0)
    info.tail_skip_percent = row.get("tail_skip_percent") or 10
    info.arm_count = a.get("arm_count", 0) or 0
    info.task_total = task_total
    info.completed = completed
    info.failed = failed
    info.done = done
    info.running = a.get("running", 0) or 0
    info.queued = a.get("queued", 0) or 0
    info.finished_arms = a.get("finished_arms", 0) or 0
    info.startable_arms = a.get("startable_arms", 0) or 0
    info.failed_arms = a.get("failed_arms", 0) or 0
    info.active_arms = a.get("active_arms", 0) or 0
    # 状态由子任务推导：库里那列只在 pause/resume/cancel 时写，普通跑完没人更新。
    # 不推导的话，4 个子任务全 failed 了实验还显示「待开始」。
    info.status = _derive_status(info.status, info)
    # 成功率用 DONE 口径（agent 自己打的【Task_Done】标记），分母是「已跑完的」
    # 而不是 total_tasks —— 否则实验刚开始时成功率会从 0 慢慢爬，看起来像全挂了。
    settled = completed + failed
    info.success_rate = round(done / settled * 100, 1) if settled else 0.0
    info.progress_percent = round(settled / task_total * 100, 1) if task_total else 0.0
    return info


def _get_batch(batch_id: str) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            """SELECT b.*,
                      q.name             AS queue_name,
                      q.tail_skip_enabled AS tail_skip_enabled,
                      q.tail_skip_percent AS tail_skip_percent
                 FROM batch_experiments b
                 LEFT JOIN task_queues q ON q.id = b.queue_id
                WHERE b.id = ?""",
            (batch_id,),
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
    status: Optional[str] = Query(None, description="created|running|paused|completed|cancelled|failed"),
    keyword: Optional[str] = Query(None, description="按实验名称 / ID 模糊搜索"),
    user: dict = Depends(get_current_user),
):
    # 联表带出队列名/长尾配置：列表页要显示队列名称，也是「启动队列」按钮的依据
    # 注意：batch_experiments.status 只在 pause/resume/cancel 时写库，
    # running/completed/failed 是由子任务状态推导出来的（_derive_status）。
    # 因此 SQL 层只能可靠地筛 paused/cancelled；其余状态在 Python 层推导后再过滤。
    sql = ("""SELECT b.*,
                     q.name             AS queue_name,
                     q.tail_skip_enabled AS tail_skip_enabled,
                     q.tail_skip_percent AS tail_skip_percent
                FROM batch_experiments b
                LEFT JOIN task_queues q ON q.id = b.queue_id""")
    conds, params = [], []
    # paused/cancelled 在库里是准确的，可以在 SQL 层直接筛
    if status in ("paused", "cancelled"):
        conds.append("b.status = ?")
        params.append(status)
    if keyword:
        conds.append("(b.name LIKE ? OR b.id LIKE ?)")
        params += [f"%{keyword}%", f"%{keyword}%"]
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY b.created_at DESC"

    with get_connection() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    agg = _aggregate_rows([r["id"] for r in rows])
    infos = [_build_info(r, agg) for r in rows]
    # 其余状态（created/running/completed/failed）在推导后过滤
    if status and status not in ("paused", "cancelled"):
        infos = [i for i in infos if i.status == status]
    return infos


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

    子任务 = arms × sim_arms × repeat_times 的笛卡尔积（两个矩阵轴）。
    sim_arms 为空时第二轴退化，等价于改造前的单轴行为。

    子任务创建**顺序 await，不并发**：create_instance 内部多次 get_connection()
    写库，而 platform.db 近 10GB，WAL checkpoint 期间独占写锁。asyncio.gather
    并发建会成片撞 `database is locked`（见 queue_scheduler 的锁重试注释）。
    两轴相乘后子任务更多，更不能改成并发。

    单个子任务失败不整体回滚：异常写进 items.create_error 继续建其余的，
    返回体带 created/failed 计数，由前端提示用户。N 可能上百，
    让一个 OBS 路径打错就废掉整次实验是不合理的。
    """
    if req.queue.max_concurrent < 1:
        raise HTTPException(status_code=400, detail="队列并发量至少为 1")
    if not req.arms:
        raise HTTPException(status_code=400, detail="至少选择一个 Harness")
    if req.repeat_times < 1:
        raise HTTPException(status_code=400, detail="重复次数至少为 1")

    # --- 测试集 + OBS 目录：全部取自任务登记（数据集管理） ---
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

    # OBS 目录由登记带出，不接受前端传入 —— 同一份数据集在不同实验里口径必须一致。
    # default_skills 是逗号分隔的技能名，不是路径，别 strip_bucket。
    obs_from_reg = {
        "user_config_dir": test_set_path,
        "skill_dir": _strip_bucket(reg.get("skill_dir_obs")),
        "agent_dir": _strip_bucket(reg.get("agent_dir_obs")),
        "user_profile_dir": _strip_bucket(reg.get("user_folder_obs")),
        "default_skills": (reg.get("default_skills") or "").strip(),
    }

    # 数据集的「轨迹保存路径」在批量里只能当前缀用：它是给「一个任务」预设的，
    # 直接原样套给 N 个子任务会让它们全部写进同一个 OBS 目录、轨迹互相覆盖
    # （与 task_name 必须唯一是同一类问题）。留空则完全交给后端按
    # {harness}_trajs/traj_{task_name} 自动生成。
    traj_prefix = (reg.get("traj_save_path") or "").strip().rstrip("/")

    # 矩阵的两个维度在这里才展开：Harness 块 × 块内模型 → 一个配置一个臂。
    # 前端提交的是原始二维结构（blocks），不是压平后的 arms —— 这样
    # params_json 能保留原始配置，详情页才还原得出「两个 Harness、两个模型」。
    arms = _expand_blocks(req)

    # sim_arms 为空 → 第二轴退化成长度 1（用 [None] 占位），子任务吃 harness
    # 模板自带的 user_proxy_model.json。这是旧客户端行为，必须保持。
    sim_axis = list(req.sim_arms) if req.sim_arms else [None]

    total_tasks = len(arms) * len(sim_axis) * req.repeat_times * max(sample_total, 0)
    if total_tasks > _MAX_TOTAL_TASKS:
        raise HTTPException(
            status_code=400,
            detail=(f"总任务数 {total_tasks} 超过上限 {_MAX_TOTAL_TASKS}，"
                    f"请减少 Harness 数、用户模拟配置数或重复次数"),
        )

    common = req.common.model_dump()

    # --- 建实验行 + 队列 ---
    with get_connection() as conn:
        batch_id = _gen_batch_id(conn)
        # 展示序号：取当前最大值 +1，只增不复用（删掉的号留空洞）。
        # 与 INSERT 同一连接同一事务，并发创建不会撞号。
        next_seq = (conn.execute(
            "SELECT MAX(seq) AS m FROM batch_experiments"
        ).fetchone()["m"] or 0) + 1

        # 整批一条队列。吞吐 = 队列并发 × 独立并发(common.concurrent_num)，
        # 队列并发在这里落库（真正放行时 queue_scheduler 还会用全局上限封顶）。
        max_concurrent = max(1, req.queue.max_concurrent)
        shared_queue_id = f"batch-{batch_id}".lower()
        conn.execute(
            "INSERT OR IGNORE INTO task_queues "
            "(id, name, max_concurrent, description, tail_skip_enabled, "
            " tail_skip_percent, created_by) VALUES (?,?,?,?,?,?,?)",
            (shared_queue_id,
             # 队列名默认用实验 ID：实验名可能带「（副本）」，复制几次后缀会
             # 层层叠加（"xxx 副本 副本 副本"），而 ID 是唯一且稳定的。
             (req.queue.name or "").strip() or batch_id,
             max_concurrent,
             (req.queue.description or "").strip() or f"批量实验 {batch_id} 自动创建",
             1 if req.queue.tail_skip_enabled else 0,
             max(1, min(20, int(req.queue.tail_skip_percent or 10))),
             user.get("username")),
        )

        conn.execute(
            """INSERT INTO batch_experiments
               (id, name, description, registration_id, test_set_path, test_set_name,
                sample_total, repeat_times, schedule_mode, queue_id, max_concurrent,
                status, params_json, created_by, seq)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (batch_id, req.name, req.description, req.registration_id,
             test_set_path, reg.get("task_name") or "", sample_total,
             req.repeat_times, "shared", shared_queue_id,
             max_concurrent, "created",
             json.dumps(_masked_params({
                 **common,
                 "arms": [a.model_dump() for a in arms],
                 "blocks": [b.model_dump() for b in req.blocks],
                 "sim_arms": [s.model_dump() for s in req.sim_arms],
             }), ensure_ascii=False),
             user.get("username"), next_seq),
        )

    # --- 逐个建子任务 ---
    from .instances import create_instance  # 延迟导入，避免循环依赖

    created, failed = 0, 0
    # 配置序号：arm × sim × repeat 展开后的第几个子任务，从 1 开始。
    # 它同时决定展示名（实验名-配置N）与 task_name（batch-cN）—— 必须是全局唯一
    # 的单调序号，否则同一 harness 配不同模型时 task_name 会撞（轨迹互相覆盖）。
    cfg_no = 0
    for arm in arms:
        for sim_idx, sim in enumerate(sim_axis, start=1):
            for rep in range(1, req.repeat_times + 1):
                cfg_no += 1
                arm_label = arm.label or arm.harness_type
                sim_label = _sim_label(sim, sim_idx)
                # 对比页的列头/图表 series 名都用 arm_label。
                # 模拟臂才是真正的第二维，跑满时才拼进 label；单轴实验保持简洁。
                label = f"{arm_label} · {sim_label}" if sim_label else arm_label

                suffix = f"-r{rep}" if req.repeat_times > 1 else ""

                # 整批共用一条队列（见上面建队列处）。不再有 per-arm 队列。
                qid = shared_queue_id

                # task_name 用于 Pod 命名和 OBS 路径，只能含字母数字与短横线。
                # 用配置序号而不是 harness_type：同 harness 多模型时会撞名。
                task_name = f"{batch_id}-c{cfg_no}".replace("_", "-").lower()

                payload = dict(common)
                # 臂的模型参数：仅非空时覆盖 common 的兜底值
                for _f in ("model_base_url", "model_api_key", "model_id", "model_api_type"):
                    _v = getattr(arm, _f, "") or ""
                    if _v:
                        payload[_f] = _v

                # 块内沙箱参数：镜像 / 沙箱配置跟 Harness 走。
                # sandbox_config_id 允许 0（= 不替换模板），所以判 None 而不是 falsy。
                _img = (arm.image_name or "").strip()
                if _img:
                    payload["image_name"] = _img
                if arm.sandbox_config_id is not None:
                    payload["sandbox_config_id"] = arm.sandbox_config_id
                # 模拟臂的 agents：非空时整组替换（空则沿用 common.agents 兜底）
                if sim is not None and sim.agents:
                    payload["agents"] = [a.model_dump() for a in sim.agents]

                payload.update({
                    # 展示名按「实验名-配置N」编号，一眼能对上是第几个配置
                    "name": f"{req.name}-配置{cfg_no}{suffix}",
                    "task_name": task_name,
                    "harness_type": arm.harness_type,
                    "harness_config_id": arm.harness_config_id,
                    "harness_config_version_id": arm.harness_config_version_id,
                    "total_num": sample_total,
                    "queue_id": qid,
                    # 测试集与 OBS 目录由登记决定，不接受前端传入
                    **obs_from_reg,
                    # 数据集设了前缀才拼；否则留空，走后端的 task_name 自动生成
                    **({"traj_save_path": f"{traj_prefix}/traj_{task_name}"}
                       if traj_prefix else {}),
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
                            harness_config_version_id, arm_label, sim_label, sim_idx,
                            repeat_idx, create_error)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (batch_id, instance_id, arm.harness_type, arm.harness_config_id,
                         arm.harness_config_version_id, label, sim_label,
                         sim_idx if sim is not None else 0, rep, err),
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


@router.post("/{batch_id}/rerun-failed")
async def rerun_failed(batch_id: str, user: dict = Depends(require_operator)):
    """重跑失败：只把 status='failed' 的子任务重新拉起来，成功的和平跑的都不动。

    与「启动队列」的区别：后者会把 created/stopped 的一起入队，这里**只挑 failed**，
    避免已经跑出结果的任务被重跑一遍浪费资源。
    """
    _get_batch(batch_id)
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT t.id FROM batch_experiment_items i
                 JOIN task_instances t ON t.id = i.instance_id
                WHERE i.batch_id = ? AND t.status = 'failed'
                ORDER BY i.id""",
            (batch_id,),
        ).fetchall()
    ids = [r["id"] for r in rows]
    if not ids:
        return {"message": "没有失败的任务需要重跑", "restarted": 0, "errors": []}

    from .instances import start_instance

    started, errors = 0, []
    for iid in ids:
        try:
            await start_instance(iid, user)
            started += 1
        except HTTPException as e:
            errors.append(f"{iid}: {e.detail}")
        except Exception as e:
            errors.append(f"{iid}: {str(e)[:200]}")

    # 队列若被暂停（暂停后再跑），先解除，否则起来也放行不了
    qids = _experiment_queue_ids(batch_id)
    if qids and started:
        ph = ",".join("?" * len(qids))
        with get_connection() as conn:
            conn.execute(f"UPDATE task_queues SET paused = 0 WHERE id IN ({ph})", qids)
        from ..services.queue_scheduler import schedule_queue
        for qid in qids:
            asyncio.create_task(schedule_queue(qid))

    return {
        "message": (f"已重新拉起 {started} 个失败任务" if not errors
                    else f"已重新拉起 {started} 个，{len(errors)} 个失败"),
        "restarted": started,
        "errors": errors,
    }


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


@router.post("/{batch_id}/copy")
async def copy_experiment(batch_id: str, user: dict = Depends(require_operator)):
    """复制实验：按原参数重建一份新的实验（含子任务）。

    key 类字段在入库时已脱敏（见 _masked_params），所以复制出来的子任务**不能**
    沿用快照里的 key —— 那样会拿一串 `sk-x***7890` 去跑。这里保留 key 以外的
    全部参数，key 留空让后端回落到 harness 模板默认值；需要 key 的实验请在
    复制后到新建页重新填。
    """
    row = _get_batch(batch_id)
    with get_connection() as conn:
        items = conn.execute(
            """SELECT harness_type, harness_config_id, harness_config_version_id,
                      arm_label, sim_label, sim_idx
                 FROM batch_experiment_items WHERE batch_id = ? ORDER BY id""",
            (batch_id,),
        ).fetchall()

    # 重建 arms：优先用 params_json 里存的原参数（保留块内模型/镜像等），
    # 它没有则退回 items 表（只有 harness 三件套）。
    try:
        saved = json.loads(row.get("params_json") or "{}")
    except (ValueError, TypeError):
        saved = {}
    saved_arms = saved.get("arms") or []

    def _strip_keys(d: dict) -> dict:
        out = {k: v for k, v in d.items() if k not in ("_uid", "_key")}
        if out.get("model_api_key"):
            out["model_api_key"] = ""
        return out

    arms = [_strip_keys(a) for a in saved_arms if isinstance(a, dict)]
    if not arms:
        arms = [{
            "harness_type": it["harness_type"],
            "harness_config_id": it["harness_config_id"],
            "harness_config_version_id": it["harness_config_version_id"],
            "label": it["arm_label"] or it["harness_type"],
        } for it in (dict(r) for r in items)]

    # blocks 也一起带过去：/copy 端点走的是 create_experiment，那边优先用 blocks，
    # 少了它就会退回摊平的 arms，复制出来的实验会丢失原始二维结构。
    blocks = [b for b in (saved.get("blocks") or []) if isinstance(b, dict)]
    for b in blocks:
        for m in (b.get("models") or []):
            if isinstance(m, dict) and m.get("model_api_key"):
                m["model_api_key"] = ""

    common = {k: v for k, v in saved.items()
              if k not in ("arms", "sim_arms", "blocks")}
    if isinstance(common.get("agents"), list):
        common["agents"] = [dict(a, api_key="") if isinstance(a, dict) else a
                            for a in common["agents"]]

    with get_connection() as conn:
        q = conn.execute("SELECT * FROM task_queues WHERE id = ?",
                         (row.get("queue_id"),)).fetchone() if row.get("queue_id") else None
    queue = {
        "name": f"{row['name']} 副本",
        "max_concurrent": (q["max_concurrent"] if q else row.get("max_concurrent")) or 10,
        "tail_skip_enabled": bool(q["tail_skip_enabled"]) if q else False,
        "tail_skip_percent": (q["tail_skip_percent"] if q else 10) or 10,
        "description": (q["description"] if q else "") or "",
    }

    req = BatchExperimentCreate(
        name=f"{row['name']}（副本）",
        description=row.get("description") or "",
        registration_id=row.get("registration_id"),
        blocks=blocks,
        arms=arms,
        sim_arms=[],
        repeat_times=row.get("repeat_times") or 1,
        queue=queue,
        common=common,
    )
    return await create_experiment(req, user)


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
                      i.sim_label, i.sim_idx,
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
            sim_label=d.get("sim_label") or "",
            sim_idx=d.get("sim_idx") or 0,
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
