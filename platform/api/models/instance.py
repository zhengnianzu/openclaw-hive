from pydantic import BaseModel
from typing import Optional, List


class InstanceCreate(BaseModel):
    name: str
    task_name: str
    skill_dir: str = ""
    agent_dir: str = ""
    user_config_dir: str = ""
    user_profile_dir: str = ""
    concurrent_num: int = 100
    image_name: Optional[str] = None
    traj_save_path: Optional[str] = None
    openclaw_config: Optional[str] = None
    start_index: int = 0
    total_num: int = 0


class InstanceInfo(BaseModel):
    id: str
    name: str
    config_path: str
    status: str
    pid: Optional[int] = None
    created_by: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    stopped_at: Optional[str] = None
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    concurrent_num: int = 100


class InstanceOverview(BaseModel):
    total: int
    completed: int
    failed: int
    running: int
    pending: int
    success_rate: float
    error_breakdown: dict = {}


class ObsItem(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: Optional[str] = None
    last_modified: Optional[str] = None


class ObsListResponse(BaseModel):
    path: str
    items: List[ObsItem]
