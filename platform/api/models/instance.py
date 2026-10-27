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
    max_concurrent: int = 1    # 队列自己设置的上限（1 = 串行）
    effective_concurrent: int = 1  # 实际生效 = min(全局 QUEUE_MAX_CONCURRENT, max_concurrent)
    description: str = ""
    tail_skip_enabled: bool = False   # 长尾跳过：任务剩余量 <= 阈值时额外放行一个
    tail_skip_percent: int = 10       # 剩余任务数百分比阈值（1-50）
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
    max_concurrent: int = 1        # 本队列的并发上限（仍受全局 QUEUE_MAX_CONCURRENT 约束）
    description: str = ""
    tail_skip_enabled: bool = False
    tail_skip_percent: int = 10    # 剩余任务数 <= 该百分比时额外放行一个


class QueueUpdate(BaseModel):
    """更新队列请求模型（目前只开放长尾跳过与描述/并发量展示值）。"""
    name: Optional[str] = None
    max_concurrent: Optional[int] = None
    description: Optional[str] = None
    tail_skip_enabled: Optional[bool] = None
    tail_skip_percent: Optional[int] = None


class QueueTaskItem(BaseModel):
    """队列详情中的任务行（对应设计图「队列详情」表格）"""
    id: str
    name: str
    status: str
    queue_position: int = 0
    position_label: str = ""           # 如 "1 (等待中)" / "-"
    created_at: str
    estimated_start_at: Optional[str] = None   # 预计开始时间
    # 队列管理页右侧任务列表（设计稿 queue2.png）用到的展示字段
    created_by: Optional[str] = None            # 负责人
    started_at: Optional[str] = None            # 开始时间
    stopped_at: Optional[str] = None            # 结束时间（终态才有）
    duration_seconds: Optional[float] = None    # 运行时长（秒），未开始/运行中为 None
    completed_tasks: int = 0                    # 成功数
    failed_tasks: int = 0                       # 失败数
    total_tasks: int = 0                        # 任务总数


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
    suffix_api_keys: bool = True      # true: 模型 key 追加 _<沙箱标识>（默认开启；用第三方模型时须手动关闭）

    # user_proxy_model.json 可配置项（兼容旧字段，映射到 user_simulator）
    user_proxy_model_name: str = ""
    user_proxy_api_key: str = ""
    user_proxy_base_url: str = ""

    # 新 agent 配置列表
    agents: List[AgentConfig] = []

    harness_type: str = "openclaw"

    code_repo_id: Optional[int] = None

    invite_code: str = ""

    # 配置方案（harness_configs.id）。新客户端应改用下面的版本 id；
    # 仅传方案时后端取该方案最新的已发布版本（兼容旧客户端）。
    harness_config_id: Optional[int] = None

    # 方案下的具体版本（harness_config_versions.id）。必须是已发布状态。
    harness_config_version_id: Optional[int] = None

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
    suffix_api_keys: bool = False       # 创建时是否开启 key 沙箱后缀（列表标识用）
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
    # 沙箱 OS：'linux' | 'windows'。快照里没有识别依据时按 linux 兜底，
    # inferred=True 表示这是推断值（历史 env_make 形态 / 快照缺失）。
    sandbox_type: str = "linux"
    sandbox_type_inferred: bool = False


class OverviewBatchRequest(BaseModel):
    """首页批量概览请求：一次拿回所有 running/preparing 实例的概览字段。"""
    ids: List[str] = []


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
