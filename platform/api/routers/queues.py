"""
队列管理路由

提供队列的 CRUD 操作和任务管理接口
"""
import asyncio
from datetime import datetime, timedelta
from typing import List
from fastapi import APIRouter, Depends, HTTPException

from ..core.security import get_current_user, require_operator
from ..core.database import async_query, async_query_one, async_db_execute, async_execute
from ..models.instance import QueueInfo, QueueCreate, QueueUpdate, InstanceInfo, QueueTaskItem
from .instances import _with_harness_type, _sync_instance_status
from ..services.queue_scheduler import effective_concurrent, global_max_concurrent

router = APIRouter(prefix="/api/queues", tags=["queues"])

TAIL_SKIP_MIN_PERCENT = 1
TAIL_SKIP_MAX_PERCENT = 20


def _clamp_tail_percent(value: int | None) -> int:
    """长尾阈值限制在 1-20%。

    分母是任务的沙箱并发量（默认 100），所以 5% ≈ 剩余 5 个任务。上限收到 20%
    是有意的：再大就相当于「还剩五分之一轮并发就补位」，退化成常规并发。
    """
    try:
        v = int(value if value is not None else 10)
    except (TypeError, ValueError):
        v = 10
    return max(TAIL_SKIP_MIN_PERCENT, min(TAIL_SKIP_MAX_PERCENT, v))


def _duration_seconds(started_at, stopped_at) -> float | None:
    """运行时长（秒）。未开始或还没结束（运行中/排队中）返回 None。

    DB 里的时间列是 ISO 字符串（有的历史行带空格分隔），统一按 fromisoformat 解析；
    解析失败按「没有时长」处理，不抛——列表接口不该因为一行脏数据整体 500。
    """
    if not started_at or not stopped_at:
        return None
    try:
        a = datetime.fromisoformat(str(started_at).replace(' ', 'T'))
        b = datetime.fromisoformat(str(stopped_at).replace(' ', 'T'))
    except (ValueError, TypeError):
        return None
    sec = (b - a).total_seconds()
    return sec if sec >= 0 else None


def _queue_info(queue: dict, counts: dict, totals: dict) -> QueueInfo:
    """把队列表行 + 计数聚合成 QueueInfo。"""
    c = counts.get(queue['id'], {})
    name = queue.get('name') or queue['id']
    return QueueInfo(
        id=queue['id'],
        name=name,
        max_concurrent=queue.get('max_concurrent') or 1,
        effective_concurrent=effective_concurrent(queue.get('max_concurrent')),
        description=queue.get('description') or '',
        tail_skip_enabled=bool(queue.get('tail_skip_enabled')),
        tail_skip_percent=queue.get('tail_skip_percent') or 10,
        created_by=queue.get('created_by'),
        created_at=queue['created_at'],
        # active 与调度器的「占用槽位」定义保持一致：preparing 同样占着队列，
        # 只数 running 会让正在下载的队列显示「空闲」并被筛选器误选。
        active_tasks=c.get('running', 0) + c.get('preparing', 0),
        queued_tasks=c.get('queued', 0),
        startable_tasks=c.get('created', 0) + c.get('stopped', 0),
        failed_tasks_in_queue=c.get('failed', 0),
        task_count=totals.get(queue['id'], 0),
    )


async def _counts_by_queue() -> dict:
    """一次聚合出所有队列的运行/排队/可启动计数，避免每队列多次查询。

    注意 failed 单独统计、不计入 startable：「启动队列」是批量操作，
    若把 failed 也放进去，一条配置错误（如 OBS 路径不存在）的任务会被反复
    拉起、反复失败，把整条队列卡死。failed 只允许用户手动点「启动」重试。
    """
    stats = await async_query(
        """SELECT queue_id, status, COUNT(*) AS count
           FROM task_instances
           WHERE queue_id IS NOT NULL
             AND status IN ('preparing', 'running', 'queued', 'created', 'stopped', 'failed')
           GROUP BY queue_id, status""",
        ()
    )
    counts = {}
    for s in stats:
        counts.setdefault(s['queue_id'], {})[s['status']] = s['count']
    return counts


async def _totals_by_queue() -> dict:
    """一次聚合出各队列的任务总数（含所有状态），用于判断队列能否删除。"""
    rows = await async_query(
        """SELECT queue_id, COUNT(*) AS count
           FROM task_instances
           WHERE queue_id IS NOT NULL
           GROUP BY queue_id""",
        ()
    )
    return {r['queue_id']: r['count'] for r in rows}


@router.get("", response_model=List[QueueInfo])
async def list_queues(current_user: dict = Depends(get_current_user)):
    """列出所有队列，含运行/排队任务统计 + 任务总数"""
    queues = await async_query("SELECT * FROM task_queues ORDER BY created_at DESC", ())
    counts = await _counts_by_queue()
    totals = await _totals_by_queue()
    return [_queue_info(q, counts, totals) for q in queues]


@router.get("/check-id/{queue_id}")
async def check_queue_id(queue_id: str, current_user: dict = Depends(get_current_user)):
    """队列名去重校验（设计图「新建队列」里的实时校验）。"""
    existing = await async_query_one(
        "SELECT id FROM task_queues WHERE id = ?", (queue_id,)
    )
    return {"id": queue_id, "available": existing is None}


@router.post("", response_model=QueueInfo)
async def create_queue(
    queue: QueueCreate,
    current_user: dict = Depends(get_current_user)
):
    """
    创建新队列

    所有用户均可创建队列，队列ID必须唯一（重复时返回 400）
    """
    existing = await async_query_one(
        "SELECT id FROM task_queues WHERE id = ?",
        (queue.id,)
    )

    if existing:
        raise HTTPException(status_code=400, detail=f"队列名称已存在，请更换")

    now = datetime.now().isoformat()
    await async_db_execute(
        """
        INSERT INTO task_queues
            (id, name, max_concurrent, description, tail_skip_enabled, tail_skip_percent,
             created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (queue.id, queue.name or queue.id, max(1, queue.max_concurrent),
         queue.description, 1 if queue.tail_skip_enabled else 0,
         _clamp_tail_percent(queue.tail_skip_percent),
         current_user.get('username'), now, now)
    )

    return QueueInfo(
        id=queue.id,
        name=queue.name or queue.id,
        max_concurrent=max(1, queue.max_concurrent),
        effective_concurrent=effective_concurrent(queue.max_concurrent),
        description=queue.description,
        tail_skip_enabled=queue.tail_skip_enabled,
        tail_skip_percent=_clamp_tail_percent(queue.tail_skip_percent),
        created_by=current_user.get('username'),
        created_at=now,
        active_tasks=0,
        queued_tasks=0
    )


@router.patch("/{queue_id}", response_model=QueueInfo)
async def update_queue(
    queue_id: str,
    patch: QueueUpdate,
    current_user: dict = Depends(require_operator),
):
    """更新队列设置（长尾跳过等）。

    只允许更新已存在的队列；字段为 None 表示不改。改完立即生效——
    调度器每次抢占都会重新读任务队列行，无需重启或重新入队。
    """
    row = await async_query_one("SELECT * FROM task_queues WHERE id = ?", (queue_id,))
    if not row:
        raise HTTPException(status_code=404, detail=f"队列 '{queue_id}' 不存在")

    fields = {}
    if patch.name is not None:
        fields["name"] = patch.name or queue_id
    if patch.max_concurrent is not None:
        fields["max_concurrent"] = max(1, patch.max_concurrent)
    if patch.description is not None:
        fields["description"] = patch.description
    if patch.tail_skip_enabled is not None:
        fields["tail_skip_enabled"] = 1 if patch.tail_skip_enabled else 0
    if patch.tail_skip_percent is not None:
        fields["tail_skip_percent"] = _clamp_tail_percent(patch.tail_skip_percent)

    if fields:
        fields["updated_at"] = datetime.now().isoformat()
        cols = ", ".join(f"{k} = ?" for k in fields)
        await async_db_execute(
            f"UPDATE task_queues SET {cols} WHERE id = ?",
            (*fields.values(), queue_id),
        )

    merged = dict(row)
    merged.update(fields)
    counts = await _counts_by_queue()
    totals = await _totals_by_queue()
    return _queue_info(merged, counts, totals)

@router.get("/{queue_id}/tasks", response_model=List[QueueTaskItem])
async def list_queue_tasks(
    queue_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    队列详情：按实际运行顺序（queue_position）返回任务，
    并给出「队列位置」「预计开始时间」。
    """
    queue = await async_query_one(
        "SELECT * FROM task_queues WHERE id = ?", (queue_id,)
    )
    if not queue:
        raise HTTPException(status_code=404, detail=f"队列 '{queue_id}' 不存在")

    rows = await async_query(
        """SELECT * FROM task_instances
           WHERE queue_id = ?
           ORDER BY queue_priority DESC, queue_position ASC""",
        (queue_id,)
    )

    # 预计开始时间：在当前占用者之后，按剩余任务的估算耗时逐个顺推。
    # 用队列内已完成任务的平均耗时作为单任务估算；没有历史数据则不给预计时间
    # （avg_sec 为 None 时 est 一律为 None，避免显示成「立即开始」）。
    # 平均耗时只取正常跑完的实例：被 stop 中断的行 started_at/stopped_at 也有值，
    # 但时长没有参考意义。
    avg = await async_query_one(
        """SELECT AVG((julianday(stopped_at) - julianday(started_at)) * 86400.0) AS sec
           FROM task_instances
           WHERE queue_id = ?
             AND status IN ('completed','finished')
             AND started_at IS NOT NULL AND stopped_at IS NOT NULL""",
        (queue_id,)
    )
    avg_sec = (avg or {}).get('sec') if avg else None

    items = []
    rank = 0            # 排队名次：只统计 queued 任务，按 queue_position 升序递增
    # 当前占用者（preparing/running）的已用时长先从估算里扣掉；没有占用者则为 0。
    now = datetime.now()
    wait_offset = 0.0
    for r in rows:
        if r['status'] not in ('preparing', 'running'):
            continue
        if avg_sec is None:
            wait_offset = 0.0
            break
        started = r['started_at'] or r['prepare_started_at']
        if started:
            try:
                elapsed = (now - datetime.fromisoformat(str(started).replace(' ', 'T'))).total_seconds()
            except (ValueError, TypeError):
                elapsed = 0.0
            wait_offset += max(0.0, avg_sec - elapsed)
        else:
            wait_offset += avg_sec
        break  # 串行队列最多一个占用者

    for r in rows:
        status = r['status']
        if status == 'running':
            # 运行中的任务不占「等待」名次
            label, est = '-', None
        elif status == 'queued':
            # 名次而非绝对序号：queue_position 只增不减（每次「启动」重新入队 +1），
            # 会有空洞，直接显示绝对序号会误导（第 4 个排队的任务可能显示 9）。
            rank += 1
            label = f"{rank} (等待中)"
            if avg_sec is None:
                est = None
            else:
                est = (now + timedelta(seconds=wait_offset + avg_sec)).isoformat()
                wait_offset += avg_sec
        else:
            label, est = '-', None

        items.append(QueueTaskItem(
            id=r['id'],
            name=r['name'],
            status=status,
            queue_position=r['queue_position'] or 0,
            queue_priority=r.get('queue_priority') or 0,
            is_jumped=(r.get('queue_priority') or 0) > 0,
            position_label=label,
            created_at=r['created_at'],
            estimated_start_at=est,
            created_by=r['created_by'],
            started_at=r['started_at'],
            stopped_at=r['stopped_at'],
            duration_seconds=_duration_seconds(r['started_at'], r['stopped_at']),
            completed_tasks=r['completed_tasks'] or 0,
            failed_tasks=r['failed_tasks'] or 0,
            total_tasks=r['total_tasks'] or 0,
        ))

    return items


@router.post("/{queue_id}/tasks/{task_id}/jump")
async def jump_queue_task(
    queue_id: str,
    task_id: str,
    current_user: dict = Depends(require_operator),
):
    """插队：把排队中的任务提到本队列最优先的放行顺序。

    只调整**顺序**，不突破并发额度：队列槽位已满时插队任务照样得等，
    只是下一个空出来的槽位会归它。语义上要和「立即运行」区分开——后者会
    让 active_tasks 超过 effective_concurrent，看板资源估算和「已满」判断都会失真。

    改完立即生效：调度器每次抢占都重新读 priority，不需要重新入队或重启。
    若此刻队列正好有空槽位，主动触发一次调度，让插队立即被放行。
    """
    from ..services.queue_scheduler import jump_task, schedule_queue

    task = await async_query_one(
        "SELECT id, queue_id, status FROM task_instances WHERE id = ?", (task_id,)
    )
    if not task:
        raise HTTPException(status_code=404, detail=f"任务 '{task_id}' 不存在")
    if task["queue_id"] != queue_id:
        raise HTTPException(
            status_code=400,
            detail=f"任务 '{task_id}' 不属于队列 '{queue_id}'（当前队列: {task['queue_id']}）",
        )

    updated = await async_execute(jump_task, task_id)
    if not updated:
        # 状态不是 queued：可能已被调度器领走（preparing/running），也可能是终态/已停止
        cur = await async_query_one(
            "SELECT status FROM task_instances WHERE id = ?", (task_id,)
        )
        raise HTTPException(
            status_code=400,
            detail=f"只有「排队中」的任务可以插队，当前状态: {(cur or {}).get('status') or '未知'}",
        )

    asyncio.create_task(schedule_queue(queue_id))

    return {
        "message": f"任务 '{task_id}' 已插队，将优先放行",
        "id": task_id,
        "queue_priority": updated["queue_priority"],
    }


@router.post("/{queue_id}/tasks/{task_id}/cancel-jump")
async def cancel_queue_task_jump(
    queue_id: str,
    task_id: str,
    current_user: dict = Depends(require_operator),
):
    """取消插队：priority 置回 0，任务回到它原本的排队名次。

    插队只改 priority、不动 queue_position，所以取消后无需重排，
    任务直接落回加入队列时的位置。
    """
    from ..services.queue_scheduler import cancel_jump

    task = await async_query_one(
        "SELECT id, queue_id, status FROM task_instances WHERE id = ?", (task_id,)
    )
    if not task:
        raise HTTPException(status_code=404, detail=f"任务 '{task_id}' 不存在")
    if task["queue_id"] != queue_id:
        raise HTTPException(
            status_code=400,
            detail=f"任务 '{task_id}' 不属于队列 '{queue_id}'",
        )

    updated = await async_execute(cancel_jump, task_id)
    if not updated:
        raise HTTPException(
            status_code=400,
            detail="该任务当前没有插队（或已不在排队中），无法取消",
        )

    return {"message": f"任务 '{task_id}' 已取消插队", "id": task_id}


@router.post("/{queue_id}/stop-all")
async def stop_all_queue_tasks(
    queue_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    停止队列：把队列中「排队中」的任务全部重置为「待启动」。

    不影响正在运行的任务，也不删除任务 —— 只是让它们退出排队，
    之后可以再通过「启动队列」或单个「启动」重新入队。
    """
    queue = await async_query_one("SELECT id FROM task_queues WHERE id = ?", (queue_id,))
    if not queue:
        raise HTTPException(status_code=404, detail=f"队列 '{queue_id}' 不存在")

    pending = await async_query(
        "SELECT id FROM task_instances WHERE queue_id = ? AND status = 'queued'",
        (queue_id,)
    )

    if pending:
        await async_db_execute(
            "UPDATE task_instances SET status = 'created' "
            "WHERE queue_id = ? AND status = 'queued'",
            (queue_id,)
        )

    return {
        "message": f"已停止队列 '{queue_id}'：{len(pending)} 个排队任务已重置为待启动",
        "stopped_count": len(pending),
    }


@router.post("/{queue_id}/start-all")
async def start_all_queue_tasks(
    queue_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    启动队列：把队列中「待启动 / 已停止」的任务全部重新入队，
    由调度器按 queue_position 依次放行（第一个运行，其余排队）。
    """
    from ..services.queue_scheduler import schedule_queue

    queue = await async_query_one("SELECT id FROM task_queues WHERE id = ?", (queue_id,))
    if not queue:
        raise HTTPException(status_code=404, detail=f"队列 '{queue_id}' 不存在")

    starters = await async_query(
        "SELECT id FROM task_instances "
        "WHERE queue_id = ? AND status IN ('created', 'stopped') "
        "ORDER BY queue_position ASC",
        (queue_id,)
    )
    if not starters:
        return {"message": f"队列 '{queue_id}' 没有可启动的任务", "started_count": 0}

    # 统一置为 queued，再交给调度器 —— 调度器本身会保证串行（只放行队首）。
    # 这样启动路径与单个「启动」完全一致，不需要两套逻辑。
    await async_db_execute(
        "UPDATE task_instances SET status = 'queued' "
        "WHERE queue_id = ? AND status IN ('created', 'stopped')",
        (queue_id,)
    )

    asyncio.create_task(schedule_queue(queue_id))

    return {
        "message": f"已启动队列 '{queue_id}'：{len(starters)} 个任务已加入执行",
        "started_count": len(starters),
    }


@router.post("/{queue_id}/cancel-all")
async def cancel_all_queue_tasks(
    queue_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    （保留）一键取消队列中所有排队任务，状态改为 stopped。
    UI 已改用 stop-all；此接口保留兼容旧调用。
    """
    # 验证队列是否存在
    queue = await async_query_one(
        "SELECT id FROM task_queues WHERE id = ?",
        (queue_id,)
    )

    if not queue:
        raise HTTPException(status_code=404, detail=f"队列 '{queue_id}' 不存在")

    # 先查出待取消的任务（用于回传数量）；changes() 是连接级的，不能另开连接读取
    pending = await async_query(
        "SELECT id FROM task_instances WHERE queue_id = ? AND status = 'queued'",
        (queue_id,)
    )

    if pending:
        await async_db_execute(
            """UPDATE task_instances
               SET status = 'stopped', stopped_at = ?
               WHERE queue_id = ? AND status = 'queued'""",
            (datetime.now().isoformat(), queue_id)
        )

    return {
        "message": f"已取消队列 '{queue_id}' 中 {len(pending)} 个排队任务",
        "cancelled_count": len(pending)
    }


@router.delete("/{queue_id}")
async def delete_queue(
    queue_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    删除队列（仅允许删除空队列）。

    队列是可复用的容器，有任务关联时不该被删掉 —— 否则任务会带着一个
    已不存在的 queue_id，在筛选里变成「幽灵队列」。只允许删除一个任务都没有的队列。
    """
    queue = await async_query_one(
        "SELECT id FROM task_queues WHERE id = ?",
        (queue_id,)
    )

    if not queue:
        raise HTTPException(status_code=404, detail=f"队列 '{queue_id}' 不存在")

    # 只有空队列才能删除
    used = await async_query_one(
        "SELECT COUNT(*) AS n FROM task_instances WHERE queue_id = ?",
        (queue_id,)
    )
    if (used or {}).get("n", 0) > 0:
        raise HTTPException(
            status_code=400,
            detail=f"队列 '{queue_id}' 下还有 {used['n']} 个任务，无法删除。"
                   f"请先移除这些任务，或直接保留该队列。"
        )

    await async_db_execute("DELETE FROM task_queues WHERE id = ?", (queue_id,))

    return {"message": f"队列 '{queue_id}' 已删除"}
