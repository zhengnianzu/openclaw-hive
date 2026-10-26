# -*- coding: utf-8 -*-
"""沙箱产物浏览（实例详情「工作区」tab）的响应模型。"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class ArtifactEntry(BaseModel):
    rel: str                  # task 目录内相对路径，如 workspace/index.html
    name: str                 # 文件名
    size: int = 0
    mtime: Optional[float] = None


class ArtifactTaskItem(BaseModel):
    traj_name: str
    # 与 worker 状态机同词表：queued/downloading/done/failed/skipped
    # （oversize 归到 skipped，见 offline/output_worker.py 的 _download_artifact_task）
    status: str = "queued"
    error: Optional[str] = None
    file_count: Optional[int] = None
    total_bytes: Optional[int] = None
    downloaded_at: Optional[float] = None
    downloaded: bool = False  # 本地目录是否已存在
    # 该 task 是否已经跑完（= artifact_complete，来自 complete.jsonl）。
    # False 表示沙箱里还没有产出，OBS 上也就没有交付物 —— 前端必须置灰且不可点，
    # 否则点下去只会下到一个空目录。列表里保留这些行是为了让用户看到整体进度
    # （已完成 1419 / 共 7161），而不是对着空列表猜发生了什么。
    complete: bool = True


class ArtifactTasksResponse(BaseModel):
    instance_id: str
    obs_base: Optional[str] = None
    tasks: List[ArtifactTaskItem] = []


class ArtifactTreeResponse(BaseModel):
    task: str
    root: str
    entries: List[ArtifactEntry] = []
    # 文件数超过上限或遍历超时会被截断 —— 必须显式告知，不能静默少列
    truncated: bool = False
