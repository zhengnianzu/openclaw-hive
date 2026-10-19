# -*- coding: utf-8 -*-
"""资源看板聚合端点。

一次请求返回看板全部数据集，避免前端为了几张图重复拉全量 /api/instances。

口径与首页「当前资源使用情况」完全一致：
- used_*  = status ∈ {running, preparing}，单实例占用 = concurrent_num × 单沙箱资源
- queued_* 单列，不计入已用
- 沙箱 OS / 资源池 / harness 都复用 instances 的解析链路（含 _RES_CACHE）

趋势没有历史采样表，用实例的 started_at/stopped_at 扫描线重建，响应标注
source="estimated"。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Query

from ..core.database import async_query
from ..core.security import require_operator
from ..models.stats import (
    StatsResponse, StatsSummary, StatusCount, HarnessCount, SandboxTypeCount,
    OsComparison, UserUsage, PoolUsageItem, Trend, TrendPoint,
)
from ..services import stats as stats_service
from . import instances, resource_pools

router = APIRouter(prefix="/api/stats", tags=["stats"])

# 趋势重建的行数上限：每行要解析一次 config_snapshot，行数过多会拖慢请求。
# 超出时只取最近 N 条，并在响应里标记 truncated。
TREND_MAX_ROWS = 5000


def _enrich(rows: list) -> list:
    """与 /api/instances 同一套派生：状态同步 + harness_type + 资源/池/OS。"""
    return [
        instances._with_resources(
            instances._with_harness_type(instances._sync_instance_status(dict(r), persist=False))
        )
        for r in rows
    ]


@router.get("", response_model=StatsResponse)
async def get_stats(
    hours: int = Query(24, ge=1, le=720),
    user: dict = Depends(require_operator),
):
    # 1) 全量状态计数：纯 SQL 聚合，不把整表读进 Python
    status_rows = await async_query(
        "SELECT status, COUNT(*) AS c FROM task_instances GROUP BY status"
    )
    status_counts = {r["status"]: r["c"] for r in status_rows}
    total = sum(status_counts.values())

    # 2) 活跃集合：只有这些行会占资源/排队，行数很少
    placeholders = ",".join("?" * len(stats_service.OCCUPYING))
    active_rows = await async_query(
        f"SELECT * FROM task_instances WHERE status IN ({placeholders}, 'queued') "
        "ORDER BY created_at DESC",
        (*stats_service.OCCUPYING,),
    )
    active = _enrich(active_rows)

    # 3) 趋势重建窗口内启动过的实例（只取需要的列）
    now = datetime.now()
    start_ts = now.timestamp() - hours * 3600
    start_text = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d %H:%M:%S")
    # created_at 早于窗口但仍在跑的实例也必须带上，否则曲线会平白少一截
    trend_rows = await async_query(
        "SELECT id, status, started_at, stopped_at, prepare_started_at, "
        "       concurrent_num, config_snapshot, create_params "
        "FROM task_instances "
        "WHERE (started_at IS NOT NULL OR prepare_started_at IS NOT NULL) "
        "  AND (created_at >= ? OR status IN ('running', 'preparing')) "
        "ORDER BY COALESCE(started_at, prepare_started_at) DESC LIMIT ?",
        (start_text, TREND_MAX_ROWS + 1),
    )
    truncated = len(trend_rows) > TREND_MAX_ROWS
    trend_rows = trend_rows[:TREND_MAX_ROWS]
    # 只需要资源字段；不做状态同步（裁剪后的行缺 pid/total_tasks，且趋势只认时间戳）
    trend_enriched = [instances._with_resources(dict(r)) for r in trend_rows]

    step = stats_service.bucket_seconds(hours)
    points = stats_service.reconstruct_trend(
        trend_enriched, start_ts, now.timestamp(), step
    )

    summary = stats_service.aggregate_summary(active)
    summary["instances_total"] = total
    summary["active_users"] = sum(
        1 for u in stats_service.user_leaderboard(active) if u["instances"] or u["queued"]
    )
    pools = stats_service.pool_usage(active, resource_pools.load_pools())
    summary["total_cpu"] = sum(p["total_cpu"] for p in pools if p["configured"])
    summary["total_memory_gb"] = sum(
        p["total_memory_gb"] for p in pools if p["configured"]
    )

    return StatsResponse(
        generated_at=now.isoformat(timespec="seconds"),
        range_hours=hours,
        summary=StatsSummary(**summary),
        status_distribution=[
            StatusCount(**x) for x in stats_service.status_distribution(status_counts)
        ],
        harness_distribution=[
            HarnessCount(**x) for x in stats_service.harness_distribution(active)
        ],
        sandbox_type_distribution=[
            SandboxTypeCount(**x) for x in stats_service.sandbox_type_distribution(active)
        ],
        os_resource_comparison=OsComparison(
            **stats_service.os_resource_comparison(active)
        ),
        user_leaderboard=[
            UserUsage(**x) for x in stats_service.user_leaderboard(active)
        ],
        pool_usage=[
            PoolUsageItem(**x) for x in pools
        ],
        trend=Trend(
            source="estimated",
            bucket_seconds=step,
            truncated=truncated,
            points=[TrendPoint(**p) for p in points],
        ),
    )
