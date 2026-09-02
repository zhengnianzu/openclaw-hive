from pydantic import BaseModel
from typing import Optional, List


class AgentConfig(BaseModel):
    name: str
    model: str = ""
    provider: str = ""
    base_url: str = ""
    api_key: str = ""
    api: str = ""
    invite_code: str = ""


class InstanceCreate(BaseModel):
    name: str
    task_name: str
    skill_dir: str = ""
    default_skills: str = ""
    agent_dir: str = ""
    user_config_dir: str = ""
    user_profile_dir: str = ""
    concurrent_num: int = 100
    image_name: Optional[str] = None
    traj_save_path: Optional[str] = None
    start_index: int = 0
    total_num: int = 0

    # openclaw.json 可配置项
    model_api_key: str = ""
    model_base_url: str = ""
    model_id: str = ""
    model_api_type: str = ""          # anthropic-messages 或 openai-completions

    # user_proxy_model.json 可配置项（兼容旧字段，映射到 user_simulator）
    user_proxy_model_name: str = ""
    user_proxy_api_key: str = ""
    user_proxy_base_url: str = ""

    # 新 agent 配置列表
    agents: List[AgentConfig] = []

    harness_type: str = "openclaw"

    code_repo_id: Optional[int] = None

    invite_code: str = ""

    harness_config_id: Optional[int] = None

    # 重跑失败场景: 传入源实例 id,创建后把源实例 complete.jsonl 搬到新实例 output,
    copy_complete_from: Optional[str] = None


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
    harness_type: str = "openclaw"
    output_status: Optional[str] = None  # NULL=未做浅层/未输出, done=浅层完成可直接用


class InstanceOverview(BaseModel):
    total: int
    completed: int
    failed: int
    running: int
    pending: int
    success_rate: float
    error_breakdown: dict = {}
    error_tree: list = []
    elapsed_seconds: Optional[float] = None
    avg_task_seconds: Optional[float] = None
    estimated_remaining_seconds: Optional[float] = None
    estimated_finish_time: Optional[str] = None


class ObsItem(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: Optional[str] = None
    last_modified: Optional[str] = None


class ObsListResponse(BaseModel):
    path: str
    items: List[ObsItem]
