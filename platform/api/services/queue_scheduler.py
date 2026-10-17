"""
队列调度器服务

实现事件驱动的队列任务调度逻辑：
1. 串行执行：同一队列内同时只有一个任务运行
2. 队列间并行：不同队列互不影响，各自独立调度
3. 自动调度：任务启动/停止/完成时触发调度

全局沙箱上限（原 MAX_TOTAL_SANDBOXES）已取消：队列调度只受「同队列串行」约束，
concurrent_num 仅作为实例参数透传，不再参与放行判断。
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
                   SET status = 'preparing', pid = NULL, started_at = NULL,
                       error_summary = NULL,
                       prepare_started_at = datetime('now')
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

    # 原子抢占队首排队任务（内含「队列无占用者」判断，多 worker 安全）
    queued_task = await async_execute(_try_claim_next, queue_id)

    if not queued_task:
        logger.info(f"Queue {queue_id}: no claimable task (occupied or empty)")
        return

    task_id = queued_task['id']

    # 启动任务：复用 instances 路由的后台准备+启动流程
    #（OBS 下载、任务数统计、PID 落库都在 _prepare_and_launch 里，不能绕开直接起 hive.sh）
    logger.info(f"Starting queued task {task_id} (queue_position: {queued_task['queue_position']})")

    try:
        from ..routers.instances import _prepare_and_launch

        # 保存句柄并挂 done callback：create_task 不会因协程内部异常而抛出，
        # 否则协程崩溃只会留下 "Task exception was never retrieved"，队列静默卡住。
        task = asyncio.create_task(_prepare_and_launch(task_id, queued_task))
        task.add_done_callback(_log_task_failure(task_id))
        logger.info(f"Task {task_id} handed to _prepare_and_launch from queue {queue_id}")

    except Exception as e:
        logger.error(f"Failed to start task {task_id}: {e}")
        # 回退到排队状态，下次调度时重试
        await async_db_execute(
            "UPDATE task_instances SET status = 'queued' WHERE id = ?",
            (task_id,)
        )
        return


def _log_task_failure(task_id: str):
    """create_task 的异常兜底：记录 + 把实例标为 failed。

    协程内部异常不会传给 create_task 的调用方，只会留下
    "Task exception was never retrieved"。置为 failed 后同队列不再被它占住，
    find_schedulable_queues 的兜底扫描会放行下一个任务。
    """
    def _cb(task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        logger.error(f"Background launch of task {task_id} crashed: {exc}", exc_info=exc)
        try:
            with get_connection() as conn:
                conn.execute(
                    "UPDATE task_instances SET status='failed', error_summary=? "
                    "WHERE id=? AND status='preparing'",
                    (f"后台启动协程异常: {str(exc)[:400]}", task_id),
                )
        except Exception as e:
            logger.error(f"Failed to reset crashed task {task_id}: {e}")
    return _cb


def find_schedulable_queues() -> list[str]:
    """找出「有排队任务、但队列内没有任何占用者」的队列。

    兜底用：调度是事件驱动的，若某次调度因异常丢失（SQLite busy、进程重启、
    协程崩溃），整条队列会静默停在 queued 永远不会自己推进。refresher 每轮扫一次，
    把这类队列重新交给 schedule_queue。幂等、多 worker 安全（schedule_queue 自身原子）。
    """
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT DISTINCT q.queue_id
                     FROM task_instances q
                    WHERE q.status = 'queued' AND q.queue_id IS NOT NULL
                      AND NOT EXISTS (
                        SELECT 1 FROM task_instances o
                         WHERE o.queue_id = q.queue_id
                           AND o.status IN ('preparing','running')
                      )"""
            ).fetchall()
        return [r["queue_id"] for r in rows]
    except Exception as e:
        logger.error(f"find_schedulable_queues failed: {e}")
        return []


def recover_stuck_preparing(timeout_seconds: int | None = None) -> set[str]:
    """回收「卡住的 preparing」实例，返回需要重新调度的队列 id 集合。

    与 recover_orphan_preparing 的区别：
      - 那个只在**后端启动时**跑一次，且只认 pid/started_at 都为 NULL；
      - 这个挂在后台 refresher 的每轮里持续跑，覆盖「协程中途消失」的场景
        （worker 被 reload、事件循环异常、外部下载永久挂起）。

    判定：status='preparing' 且 pid IS NULL（本次准备还没拉起进程），
    且 prepare_started_at 距现在超过阈值。

    prepare_started_at 由进入 preparing 的 UPDATE 用 SQLite 的 datetime('now')（UTC）写入，
    与本函数的 datetime('now') 基准一致。**不能**用 created_at：它是任务创建时间（且由
    Python 以本地时间写入），排队久的任务一进 preparing 就会被判超时，真正卡住的实例
    反而要等 8 小时以上。

    旧数据没有 prepare_started_at 时退回 created_at（这类行只可能是升级前遗留的卡住实例）。

    幂等、多 worker 安全：UPDATE 带 status='preparing' 条件，重复执行只会命中一次。
    """
    from ..core.config import settings

    limit = timeout_seconds if timeout_seconds is not None else settings.PREPARE_TIMEOUT_SECONDS
    queues: set[str] = set()
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT id, queue_id, prepare_started_at FROM task_instances
                    WHERE status='preparing' AND pid IS NULL
                      AND julianday(COALESCE(prepare_started_at, created_at))
                          < julianday('now', ?)""",
                (f"-{int(limit)} seconds",),
            ).fetchall()
            for r in rows:
                cur = conn.execute(
                    "UPDATE task_instances SET status='failed', error_summary=? "
                    "WHERE id=? AND status='preparing' AND pid IS NULL",
                    (f"准备阶段超过 {limit}s 未完成（判定为卡住），已标记为失败", r["id"]),
                )
                if cur.rowcount and r["queue_id"]:
                    queues.add(r["queue_id"])
                if cur.rowcount:
                    logger.warning(f"回收卡住的 preparing 实例 {r['id']} -> failed")
    except Exception as e:
        logger.error(f"recover_stuck_preparing failed: {e}")
    return queues


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
