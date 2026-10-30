# -*- coding: utf-8 -*-
"""任务详情导出路由。

两个入口：
  POST /api/export/instances.xlsx    —— Dashboard 多选（跨队列 / 含未入队任务）
  GET  /api/export/queues/{qid}.xlsx —— 整条队列（按 queue_position 升序）

为什么独立成 router：queues.py 的前缀是 /api/queues（语义不符，导出要支持任意任务集合），
batch_output.py 的前缀是 /api/instances（属单实例输出域且已很长）。

端点用同步 def：FastAPI 会把同步端点丢进线程池，openpyxl 写表与 sqlite 读都是阻塞 IO，
用 async def 包同步调用反而阻塞事件循环。
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from ..core.database import get_connection, async_query_one
from ..core.security import require_operator
from ..services import task_export as tex
from ..services.queue_scheduler import effective_concurrent
from .instances import _sync_instance_status, _with_harness_type, _with_resources

router = APIRouter(prefix="/api/export", tags=["task-export"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# 单次导出上限：200 条 × (2 次 YAML 解析 + 1 次 SQL) 是毫秒级，
# 真正的风险是文件体积（快照 2~10KB × 200 ≈ 2MB）与浏览器下载体验。
MAX_EXPORT_INSTANCES = 200


class TaskExportRequest(BaseModel):
    instance_ids: List[str] = []
    include_task_records: bool = True


def _sync_readonly(row: dict) -> dict:
    """把实例行刷成最新的 completed/failed/status，但**绝不写库**。

    _sync_instance_status 默认 persist=False：请求线程只更新 10s TTL 内存缓存，
    写库是后台 refresher 的职责（无脑写会触发 database is locked）。
    这里再显式传 False，防止将来默认值变化导致导出路径意外写库。
    """
    return _sync_instance_status(_with_harness_type(dict(row)), persist=False)


def _xlsx_response(wb, filename: str) -> Response:
    return Response(
        content=tex.workbook_bytes(wb),
        media_type=XLSX_MEDIA_TYPE,
        headers={"content-disposition": tex.content_disposition(filename)},
    )


def _stamp() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d_%H%M")


@router.post("/instances.xlsx")
def export_instances_xlsx(
    req: TaskExportRequest,
    user: dict = Depends(require_operator),
):
    """导出勾选的一批任务（可按 id 集合跨队列）。"""
    ids = [i for i in (req.instance_ids or []) if i]
    if not ids:
        raise HTTPException(status_code=400, detail="请至少选择一个任务")
    if len(set(ids)) > MAX_EXPORT_INSTANCES:
        raise HTTPException(
            status_code=400,
            detail=f"一次最多导出 {MAX_EXPORT_INSTANCES} 个任务，请分批或按队列导出",
        )

    with get_connection() as conn:
        rows = tex.collect_rows(
            ids, conn, sync_status=_sync_readonly, with_resources=_with_resources,
        )
        wb = tex.build_workbook(
            rows, conn=conn, include_task_records=req.include_task_records,
        )

    filename = f"任务详情_{len(rows)}条_{_stamp()}.xlsx"
    return _xlsx_response(wb, filename)


@router.get("/queues/{queue_id}.xlsx")
def export_queue_xlsx(queue_id: str, user: dict = Depends(require_operator)):
    """导出整条队列的任务详情（按放行顺序，与队列详情弹窗同序）。"""
    with get_connection() as conn:
        queue = conn.execute("SELECT * FROM task_queues WHERE id = ?", (queue_id,)).fetchone()
        if not queue:
            raise HTTPException(status_code=404, detail=f"队列 '{queue_id}' 不存在")
        queue = dict(queue)

        id_rows = conn.execute(
            "SELECT id FROM task_instances WHERE queue_id = ? "
            "ORDER BY queue_priority DESC, queue_position ASC",
            (queue_id,),
        ).fetchall()
        ids = [r["id"] for r in id_rows]
        if not ids:
            raise HTTPException(status_code=400, detail="该队列下没有任务可导出")
        if len(ids) > MAX_EXPORT_INSTANCES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"该队列有 {len(ids)} 个任务，超过单次上限 {MAX_EXPORT_INSTANCES}，"
                    "请先在任务列表里分批勾选导出"
                ),
            )

        rows = tex.collect_rows(
            ids, conn, sync_status=_sync_readonly, with_resources=_with_resources,
        )
        wb = tex.build_workbook(
            rows,
            conn=conn,
            queue=queue,
            effective_concurrent=effective_concurrent(queue.get("max_concurrent")),
            task_count=len(ids),
            include_task_records=True,
        )

    filename = f"队列_{queue_id}_任务详情_{_stamp()}.xlsx"
    return _xlsx_response(wb, filename)


# ---------------------------------------------------------------------------
# 队列管理页的批量导出（设计稿 queue2.png 顶部「导出操作区」）
# ---------------------------------------------------------------------------

class QueueExportRequest(BaseModel):
    queue_ids: List[str] = []


@router.post("/queues.xlsx")
def export_queues_xlsx(
    req: QueueExportRequest,
    user: dict = Depends(require_operator),
):
    """导出「选中队列」的队列级汇总（一行一队列）。

    queue_ids 为空 = 导出全部队列（顶部「导出队列列表」不勾选时也是这个语义）。
    """
    ids = [i for i in (req.queue_ids or []) if i]
    with get_connection() as conn:
        if ids:
            placeholders = ",".join("?" * len(ids))
            queues = conn.execute(
                f"SELECT * FROM task_queues WHERE id IN ({placeholders}) ORDER BY created_at DESC",
                tuple(ids),
            ).fetchall()
        else:
            queues = conn.execute(
                "SELECT * FROM task_queues ORDER BY created_at DESC"
            ).fetchall()

        if not queues:
            raise HTTPException(status_code=400, detail="没有可导出的队列")

        queue_rows = [dict(q) for q in queues]
        stats = tex.collect_queue_stats(conn, [q["id"] for q in queue_rows])
        wb = tex.build_queue_summary_workbook(queue_rows, stats)

    scope = f"{len(queue_rows)}个队列" if ids else "全部队列"
    return _xlsx_response(wb, f"队列列表_{scope}_{_stamp()}.xlsx")


@router.post("/all.xlsx")
def export_all_xlsx(
    req: Optional[TaskExportRequest] = None,
    user: dict = Depends(require_operator),
):
    """「导出全部」：所有队列 + 所属全部任务，一个工作簿里出全量明细。

    任务数超过 MAX_EXPORT_INSTANCES 时不再整体拒绝（这是「导出全部」的语义），
    而是截断到上限并在概览页明确写出截断了多少，避免用户拿到一个静默残缺的文件。
    """
    include_records = True if req is None else req.include_task_records
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM task_instances").fetchone()["c"]
        if not total:
            raise HTTPException(status_code=400, detail="没有可导出的任务")

        id_rows = conn.execute(
            """SELECT id FROM task_instances
               ORDER BY queue_id IS NULL, queue_id, queue_position, created_at"""
        ).fetchall()
        all_ids = [r["id"] for r in id_rows]
        ids = all_ids[:MAX_EXPORT_INSTANCES]
        truncated = len(all_ids) > len(ids)

        queues = conn.execute("SELECT * FROM task_queues ORDER BY created_at DESC").fetchall()
        queue_rows = [dict(q) for q in queues]
        stats = tex.collect_queue_stats(conn, [q["id"] for q in queue_rows])

        rows = tex.collect_rows(
            ids, conn, sync_status=_sync_readonly, with_resources=_with_resources,
        )
        wb = tex.build_workbook(
            rows,
            conn=conn,
            include_task_records=include_records,
            all_queues=(queue_rows, stats),
            truncated_note=(
                f"任务共 {len(all_ids)} 个，本次仅导出前 {len(ids)} 个（单次上限 "
                f"{MAX_EXPORT_INSTANCES}），其余请按队列或分批导出"
            ) if truncated else None,
        )

    return _xlsx_response(wb, f"全部任务详情_{len(ids)}条_{_stamp()}.xlsx")
