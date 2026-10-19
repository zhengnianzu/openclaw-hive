# -*- coding: utf-8 -*-
"""看板 /api/stats 的响应模型。"""
from typing import Optional

from pydantic import BaseModel


class StatsSummary(BaseModel):
    instances_total: int = 0
    active: int = 0
    running: int = 0
    preparing: int = 0
    queued: int = 0
    # 本次统计窗口内有资源占用的用户数（区别于全部的 created_by 去重）
    active_users: int = 0
    # 资源池总量（各池之和）；未配置时为 0，前端显示「未配置」
    total_cpu: float = 0.0
    total_memory_gb: float = 0.0
    used_cpu: float = 0.0
    used_cpu_limit: float = 0.0
    used_memory_gb: float = 0.0
    used_memory_gb_limit: float = 0.0
    queued_cpu: float = 0.0
    queued_memory_gb: float = 0.0
    pods: int = 0


class StatusCount(BaseModel):
    status: str
    label: str
    count: int


class HarnessCount(BaseModel):
    harness_type: str
    count: int
    cpu: float = 0.0
    memory_gb: float = 0.0
    pods: int = 0


class SandboxTypeCount(BaseModel):
    sandbox_type: str
    count: int
    cpu: float = 0.0
    memory_gb: float = 0.0
    pods: int = 0
    inferred_count: int = 0


class OsUsage(BaseModel):
    instances: int = 0
    pods: int = 0
    cpu: float = 0.0
    memory_gb: float = 0.0


class OsComparison(BaseModel):
    linux: OsUsage = OsUsage()
    windows: OsUsage = OsUsage()


class UserUsage(BaseModel):
    user: str
    instances: int = 0
    running: int = 0
    preparing: int = 0
    queued: int = 0
    pods: int = 0
    cpu: float = 0.0
    memory_gb: float = 0.0
    linux_cpu: float = 0.0
    windows_cpu: float = 0.0
    linux_memory_gb: float = 0.0
    windows_memory_gb: float = 0.0


class PoolUsageItem(BaseModel):
    namespace: str
    total_cpu: float = 0.0
    total_memory_gb: float = 0.0
    used_cpu: float = 0.0
    used_memory_gb: float = 0.0
    queued_cpu: float = 0.0
    queued_memory_gb: float = 0.0
    instances: int = 0
    configured: bool = True


class TrendPoint(BaseModel):
    ts: str
    used_cpu: float = 0.0
    used_memory_gb: float = 0.0
    running: int = 0
    pods: int = 0
    linux_cpu: float = 0.0
    windows_cpu: float = 0.0
    linux_memory_gb: float = 0.0
    windows_memory_gb: float = 0.0
    linux_pods: int = 0
    windows_pods: int = 0


class Trend(BaseModel):
    # 目前只有按启停时间估算一种来源，保留字段便于以后接真实采样
    source: str = "estimated"
    bucket_seconds: int = 300
    truncated: bool = False     # 实例数过多时只重建最近 N 条，曲线可能偏低
    points: list[TrendPoint] = []


class StatsResponse(BaseModel):
    generated_at: str
    range_hours: int
    summary: StatsSummary
    status_distribution: list[StatusCount] = []
    harness_distribution: list[HarnessCount] = []
    sandbox_type_distribution: list[SandboxTypeCount] = []
    os_resource_comparison: OsComparison = OsComparison()
    user_leaderboard: list[UserUsage] = []
    pool_usage: list[PoolUsageItem] = []
    trend: Trend = Trend()
