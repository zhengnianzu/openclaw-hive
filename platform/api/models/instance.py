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


class QueueInfo(BaseModel):
    """队列信息模型"""
    id: str
    name: str
    max_concurrent: int = 1    # 队列并发量（展示用；当前调度为严格串行）
    description: str = ""
    created_by: Optional[str] = None
    created_at: str
    active_tasks: int = 0      # 派生字段：运行中的任务数
    queued_tasks: int = 0      # 派生字段：排队中的任务数
    startable_tasks: int = 0   # 派生字段：待启动/已停止、可被「启动队列」拉起的任务数
    failed_tasks_in_queue: int = 0  # 派生字段：准备/启动失败的任务数（需手动重试）
    task_count: int = 0        # 派生字段：该队列下的任务总数（用于判断能否删除）


class QueueCreate(BaseModel):
    """创建队列请求模型"""
    id: str                        # 队列ID，用户输入，必须唯一
    name: str = ""                 # 队列显示名称（留空时用 id 兜底）
    max_concurrent: int = 1        # 并发量，默认 1
    description: str = ""


class QueueTaskItem(BaseModel):
    """队列详情中的任务行（对应设计图「队列详情」表格）"""
    id: str
    name: str
    status: str
    queue_position: int = 0
    position_label: str = ""           # 如 "1 (等待中)" / "-"
    created_at: str
    estimated_start_at: Optional[str] = None   # 预计开始时间


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

    # 沙箱配置（settings/sandbox/ 下的派生文件），不选则沿用模板默认
    sandbox_config_id: Optional[int] = None

    # 重跑失败场景: 传入源实例 id,创建后把源实例 complete.jsonl 搬到新实例 output,
    copy_complete_from: Optional[str] = None

    # 队列支持
    queue_id: Optional[str] = None


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
    # 准备/拉起进程失败的原因（status='failed' 时展示给用户）。
    # 用户点「启动」重试时会清空。
    error_summary: Optional[str] = None
    queue_id: Optional[str] = None      # 所属队列ID
    queue_position: int = 0              # 在队列中的位置
    # 单沙箱资源（从 config_snapshot 解析；内存单位 GiB）。前端用
    # concurrent_num × 这些值算首页看板的核数/内存占用。
    cpu_request: float = 0.0
    cpu_limit: float = 0.0
    memory_request: float = 0.0
    memory_limit: float = 0.0
    sandbox_namespace: str = ""         # 资源池标识（= sandbox_namespace）


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
