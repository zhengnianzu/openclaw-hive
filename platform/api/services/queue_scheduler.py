"""
队列调度器服务

实现事件驱动的队列任务调度逻辑：
1. 串行执行：同一队列内同时只有一个任务运行
2. 全局资源限制：检查沙箱总量不超过 MAX_TOTAL_SANDBOXES
3. 自动调度：任务启动/停止/完成时触发调度
"""
import asyncio
import logging

from ..core.config import settings
from ..core.database import (
    async_query, async_query_one, async_db_execute, async_execute, get_connection,
)

logger = logging.getLogger(__name__)

# 占用队列槽位的状态：preparing 也算占用，因为任务在 _prepare_instance（OBS 下载、
# 打包、统计任务数）期间还没变成 running，若此刻放行下一个任务，队列就不再串行了。
OCCUPYING_STATUSES = ("preparing", "running")


async def check_global_sandbox_capacity() -> int:
    """
    检查全局沙箱可用容量

    Returns:
        可用的沙箱数量（MAX_TOTAL_SANDBOXES - 当前已占用任务的 concurrent_num 总和）
    """
    # preparing 与 running 都已占住沙箱额度，两者都要计
    placeholders = ",".join("?" * len(OCCUPYING_STATUSES))
    result = await async_query_one(
        f"SELECT SUM(concurrent_num) as total FROM task_instances "
        f"WHERE status IN ({placeholders})",
        OCCUPYING_STATUSES
    )

    used_sandboxes = result.get('total', 0) or 0
    available = settings.MAX_TOTAL_SANDBOXES - used_sandboxes

    logger.info(f"Global sandbox capacity: {available}/{settings.MAX_TOTAL_SANDBOXES} available (used: {used_sandboxes})")

    return available


def _try_claim_next(queue_id: str) -> dict | None:
    """原子地「选出队首排队任务并占住它」。

    必须做成单条语句：生产是 --workers 4，多个 worker 会同时调度同一队列。
    如果拆成「先查占用、再选任务、再 UPDATE」（各自都是独立语句），两个 worker 会
    同时通过占用检查，然后各自领走一个不同的任务，串行就被破坏（实测可复现）。
    这里把「队列无占用者」当成 UPDATE 的 WHERE 条件，由 SQLite 的写锁保证原子：
    只有 rowcount 命中 1 行的调用才是赢家，其余拿不到结果直接放弃。

    Returns:
        抢到的任务行（dict），未抢到返回 None。
    """
    placeholders = ",".join("?" * len(OCCUPYING_STATUSES))
    with get_connection() as conn:
        row = conn.execute(
            f"""UPDATE task_instances
                   SET status = 'preparing'
                 WHERE id = (
                     SELECT id FROM task_instances
                      WHERE queue_id = ? AND status = 'queued'
                      ORDER BY queue_position ASC
                      LIMIT 1
                 )
                   AND NOT EXISTS (
                     SELECT 1 FROM task_instances
                      WHERE queue_id = ? AND status IN ({placeholders})
                   )
             RETURNING *""",
            (queue_id, queue_id, *OCCUPYING_STATUSES)
        ).fetchone()
        return dict(row) if row else None


async def schedule_queue(queue_id: str):
    """
    队列调度核心逻辑（MVP: 串行执行）

    当队列中有任务状态变化时调用，尝试启动下一个排队任务

    Args:
        queue_id: 队列ID
    """
    logger.info(f"Scheduling queue: {queue_id}")

    # 1. 检查全局沙箱资源（低于需求时不必尝试抢占，省一次写）
    available_capacity = await check_global_sandbox_capacity()

    # 2. 原子抢占队首排队任务（内含「队列无占用者」判断，多 worker 安全）
    queued_task = await async_execute(_try_claim_next, queue_id)

    if not queued_task:
        logger.info(f"Queue {queue_id}: no claimable task (occupied or empty)")
        return

    task_id = queued_task['id']
    task_concurrent = queued_task['concurrent_num']

    # 3. 检查资源是否足够（任务已被占为 preparing，不足则退回 queued）
    if task_concurrent > available_capacity:
        logger.warning(
            f"Insufficient sandbox capacity for task {task_id}: "
            f"needs {task_concurrent}, available {available_capacity}"
        )
        await async_db_execute(
            "UPDATE task_instances SET status = 'queued' WHERE id = ?",
            (task_id,)
        )
        return

    # 4. 启动任务：复用 instances 路由的后台准备+启动流程
    #（OBS 下载、任务数统计、PID 落库都在 _prepare_and_launch 里，不能绕开直接起 hive.sh）
    logger.info(f"Starting queued task {task_id} (queue_position: {queued_task['queue_position']})")

    try:
        from ..routers.instances import _prepare_and_launch

        asyncio.create_task(_prepare_and_launch(task_id, queued_task))
        logger.info(f"Task {task_id} handed to _prepare_and_launch from queue {queue_id}")

    except Exception as e:
        logger.error(f"Failed to start task {task_id}: {e}")
        # 回退到排队状态，下次调度时重试
        await async_db_execute(
            "UPDATE task_instances SET status = 'queued' WHERE id = ?",
            (task_id,)
        )
        return


async def resume_all_queues():
    """
    启动时对所有「有排队任务」的队列做一次调度。

    必要性：调度是事件驱动的（启动/停止/完成时触发）。若后端在「整条队列都处于
    queued、没有任何 running/preparing 兄弟任务」时重启，就没有任何事件会再触发
    调度——后台 refresher 只扫 running/preparing/终态行，看不到这些 queued 任务，
    它们会被永久搁死。这里在 startup 时兜底补一次。

    返回触发的队列数。
    """
    try:
        rows = await async_query(
            "SELECT DISTINCT queue_id FROM task_instances "
            "WHERE status = 'queued' AND queue_id IS NOT NULL",
            ()
        )
    except Exception as e:
        logger.error(f"resume_all_queues query failed: {e}")
        return 0

    for row in rows:
        qid = row["queue_id"]
        logger.info(f"Resuming queue with queued tasks: {qid}")
        asyncio.create_task(schedule_queue(qid))

    return len(rows)
