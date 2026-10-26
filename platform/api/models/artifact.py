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
    status: str = "pending"   # pending/downloading/done/failed/oversize
    error: Optional[str] = None
    file_count: Optional[int] = None
    total_bytes: Optional[int] = None
    downloaded_at: Optional[float] = None
    downloaded: bool = False  # 本地目录是否已存在


class ArtifactTasksResponse(BaseModel):
    instance_id: str
    obs_base: Optional[str] = None
    tasks: List[ArtifactTaskItem] = []


class ArtifactTreeResponse(BaseModel):
    task: str
    root: str
    entries: List[ArtifactEntry] = []
