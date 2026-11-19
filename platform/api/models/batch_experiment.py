from pydantic import BaseModel
from typing import Optional, List

from .instance import AgentConfig


# ============================================================================
# 批量实验：同一测试集 × N 个 Harness × M 个用户模拟配置（× 重复次数）
#
# 矩阵是**两轴**的笛卡尔积：
#   - 轴一「臂」(arm)      = 一个 Harness（可精确到配置方案的某个已发布版本）
#   - 轴二「模拟臂」(sim_arm) = 一组 user_proxy agents（user_simulator / evaluator ...）
#
# 一个 arm × 一个 sim_arm × 一个重复轮次 = 一个 task_instances，跑完整个测试集。
# 所以子任务数 = 臂数 × max(模拟臂数, 1) × 重复次数，
#     总任务数 = 子任务数 × 样本数。
#
# sim_arms 为空时整个轴退化掉（等价于长度 1），子任务吃 harness 模板自带的
# user_proxy_model.json —— 这是旧客户端的行为，必须保持。
# ============================================================================


class BatchArm(BaseModel):
    """实验的一个对比臂：一个 Harness（可精确到配置方案的某个已发布版本）。

    模型参数（model_*）**按臂配置**：model_api_type 决定 provider 包/transport/
    client_provider 的写法，各 harness 并不通用（opencode 用 npm 包名、pi 写
    models.json、hermes 写 config.yaml 的 transport、grok 写 config.toml），
    所以一个全局值在多 harness 混跑时不可能同时正确。
    留空的字段回落到 BatchCommonParams 的同名字段。
    """
    harness_type: str
    harness_config_id: Optional[int] = None
    harness_config_version_id: Optional[int] = None
    # 展示名，如 "OpenClaw·科研 v1.1"。留空时后端按 harness_type + 版本号拼。
    label: str = ""

    # 块内模型参数：留空 = 继承 common 的同名字段
    model_base_url: str = ""
    model_api_key: str = ""
    model_id: str = ""
    model_api_type: str = ""

    # 块内沙箱参数：镜像与沙箱配置是跟 Harness 走的（不同 harness 常需不同镜像/
    # 资源池），所以按臂配置。留空/None = 落回 common 的同名字段。
    image_name: Optional[str] = None
    sandbox_config_id: Optional[int] = None


class BatchModelParams(BaseModel):
    """Harness 块内的一个模型 —— 矩阵的第二维（同一 Harness 下可配多个模型）。"""
    model_id: str = ""
    model_base_url: str = ""
    model_api_key: str = ""
    model_api_type: str = ""


class BatchBlock(BaseModel):
    """一个 Harness 配置块 = 一个 Harness（类型/方案/版本/镜像/沙箱）+ 若干模型。

    这是矩阵的**原始二维结构**：Harness 是一维，块内 models 是另一维。
    真正的展开（Harness × 模型 → 一个子任务配置）发生在 create_experiment
    建子任务时，而不是在前端提交前就压平 —— 否则 params_json 里只剩笛卡尔积，
    详情页无法还原出「两个 Harness、两个模型」这样的原始配置。
    """
    harness_type: str
    harness_config_id: Optional[int] = None
    harness_config_version_id: Optional[int] = None
    label: str = ""
    image_name: Optional[str] = None
    sandbox_config_id: Optional[int] = None
    models: List[BatchModelParams] = []


class BatchSimArm(BaseModel):
    """实验的一个用户模拟臂：一组 user_proxy agents。

    agents 非空时整组替换子任务的 user_proxy_model.json（见
    instances.create_instance 的「生成 user_proxy_model.json」段）。
    """
    # 展示名，如 "gemini 模拟"。留空时后端按 agents 的名字拼。
    label: str = ""
    agents: List[AgentConfig] = []


class BatchCommonParams(BaseModel):
    """所有子任务共享的创建参数（InstanceCreate 的子集）。

    **不在这里**的三类字段：
    - 测试集与 OBS 目录（user_config_dir / total_num / skill_dir / agent_dir /
      user_profile_dir / default_skills）—— 全部由实验选定的登记
      （task_registrations）决定，后端填入，避免前端传了和测试集冲突的值。
    - harness_* 与 model_* —— 那是每个臂（BatchArm）自己的。
      model_* 在这里只作为臂留空时的兜底。
    - agents —— 那是每个模拟臂（BatchSimArm）自己的。
      这里的 agents 只作为 sim_arms 为空时的兜底（兼容旧客户端）。
    """
    concurrent_num: int = 100
    image_name: Optional[str] = None
    max_ttl_second: int = 36000
    start_index: int = 0

    # 留空即由后端按 {harness}_trajs/traj_{task_name} 生成，而 task_name 已按
    # (臂, 模拟臂, 轮次) 唯一，所以不配也不会串路径。
    traj_save_path: str = ""

    # 以下 model_* 为臂留空时的兜底值
    model_api_key: str = ""
    model_base_url: str = ""
    model_id: str = ""
    model_api_type: str = ""
    suffix_api_keys: bool = True
    proxy_enable: bool = False

    # sim_arms 为空时的兜底 agents
    agents: List[AgentConfig] = []

    sandbox_config_id: Optional[int] = None
    code_repo_id: Optional[int] = None
    invite_code: str = ""


class BatchQueueConfig(BaseModel):
    """实验自带的那条队列。批次内全部子任务都排进它。

    实际吞吐由两级并发相乘决定：
      队列并发（本类的 max_concurrent，落库时仍受全局 QUEUE_MAX_CONCURRENT 封顶）
      × 独立并发（common.concurrent_num，每个子任务内部同时跑几个沙箱）
    所以「同时最多几个沙箱」= min(全局上限, 队列并发) × 独立并发。

    长尾跳过沿用 queue_scheduler 的口径：队列里某任务剩余量已喂不满一轮并发时，
    额外放行一个 —— 避免最后几个任务把队列尾部拖得很长。
    """
    name: str = ""                       # 留空 → 用实验名
    max_concurrent: int = 10             # 队列并发
    tail_skip_enabled: bool = False
    tail_skip_percent: int = 10          # 1-20
    description: str = ""


class BatchExperimentCreate(BaseModel):
    name: str
    description: str = ""
    registration_id: int                      # 测试集
    # 矩阵原始二维结构：Harness 块 × 块内模型。优先于 arms 使用，
    # 由后端展开成 arms（一个模型一个臂）再建子任务。
    blocks: List[BatchBlock] = []
    # 摊平后的臂。为兼容旧客户端保留：blocks 为空时才直接使用它。
    arms: List[BatchArm] = []
    # 第二矩阵轴。空列表 = 该轴退化，子任务吃 harness 模板默认的
    # user_proxy_model.json（旧客户端行为）。
    sim_arms: List[BatchSimArm] = []
    repeat_times: int = 1
    # 队列配置。整批共用一条队列，不再有 shared/isolated/serial 三种调度策略。
    queue: BatchQueueConfig = BatchQueueConfig()
    common: BatchCommonParams = BatchCommonParams()


class BatchExperimentInfo(BaseModel):
    id: str
    # 展示序号：按创建时间升序分配（最早=1），创建后不变；删除会留空洞。
    # 容忍 NULL（迁移刚加完列、回填还没跑完的窗口期），用 0 表示「还没编号」。
    seq: Optional[int] = 0
    name: str
    description: str = ""
    registration_id: Optional[int] = None
    test_set_path: str = ""
    test_set_name: str = ""
    sample_total: int = 0
    repeat_times: int = 1
    schedule_mode: str = "shared"     # 兼容旧数据：新实验恒为 shared（单队列）
    queue_id: Optional[str] = None
    max_concurrent: int = 20
    queue_name: str = ""
    tail_skip_enabled: bool = False
    tail_skip_percent: int = 10
    status: str = "created"
    params_json: str = "{}"
    created_by: Optional[str] = None
    created_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    # --- 派生字段：由一条 GROUP BY 聚合 SQL 算出，不做 N+1 ---
    arm_count: int = 0          # 子任务（instance）个数 = 臂数 × 重复次数
    task_total: int = 0         # 所有子任务的 total_tasks 之和
    completed: int = 0
    done: int = 0               # DONE 口径（agent 自己打的 Task_Done 标记）
    failed: int = 0
    running: int = 0            # 处于 running 的子任务数
    queued: int = 0             # 处于 queued 的子任务数
    finished_arms: int = 0      # 已到终态的子任务数
    startable_arms: int = 0     # 待启动/已停止、可被「启动队列」拉起的子任务数
    failed_arms: int = 0        # 子任务失败数（status='failed'），用于推导实验终态
    active_arms: int = 0        # 正在跑的子任务数（running/preparing）
    success_rate: float = 0.0
    progress_percent: float = 0.0


class BatchItemInfo(BaseModel):
    """实验详情「任务列表 / 各臂进度」里的一行。"""
    id: int
    batch_id: str
    instance_id: Optional[str] = None
    harness_type: str
    harness_config_id: Optional[int] = None
    harness_config_version_id: Optional[int] = None
    arm_label: str = ""
    # 用户模拟臂（第二轴）。sim_arms 为空的实验里恒为 ''/0。
    sim_label: str = ""
    sim_idx: int = 0
    repeat_idx: int = 1
    create_error: Optional[str] = None

    # 来自关联的 task_instances（创建失败时全为默认值）
    instance_name: str = ""
    status: str = ""
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    done_tasks: int = 0
    queue_id: Optional[str] = None
    started_at: Optional[str] = None
    stopped_at: Optional[str] = None
    progress_percent: float = 0.0


class BatchCompareRow(BaseModel):
    """结果对比页「总体评测结果」的一行（一个臂一行）。

    arm_label 已经拼了双轴（"harness · 模拟"），sim_label 单独给出是为了让
    前端能加一列「用户模拟」，不必回去拆字符串。
    """
    item_id: int
    instance_id: Optional[str] = None
    arm_label: str = ""
    harness_type: str = ""
    sim_label: str = ""
    sim_idx: int = 0
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    done_tasks: int = 0
    # 两个口径并列，前端可切换。completed 只代表流水线没抛异常，
    # done 是 agent 自己打的【Task_Done】标记，更接近「真的做完了」。
    completed_rate: float = 0.0
    done_rate: float = 0.0
    # 平均耗时（秒/任务）= (stopped_at - started_at) / total_tasks
    avg_task_seconds: Optional[float] = None
    elapsed_seconds: Optional[float] = None
    # 评分：只对已评估的行求均值，未评估的单独计数，**绝不当 0 分拉低均值**
    avg_score: Optional[float] = None
    scored_count: int = 0
    unscored_count: int = 0
    # Token：数据源 task_traj_records.total_tokens 已就绪，本期不计算，
    # 恒为 None，前端渲染成 '—'。
    total_tokens: Optional[int] = None


class BatchMatrixCell(BaseModel):
    instance_id: str
    eval_score: Optional[float] = None
    traj_level: Optional[str] = None
    task_done: Optional[int] = None
    status: Optional[str] = None


class BatchMatrixRow(BaseModel):
    config_name: str
    cells: dict = {}            # instance_id -> BatchMatrixCell(dict)


class BatchMatrixResponse(BaseModel):
    total: int                  # 去重后的样本（config_name）总数
    page: int
    page_size: int
    columns: List[BatchCompareRow] = []   # 列定义（复用臂的展示信息）
    rows: List[BatchMatrixRow] = []
