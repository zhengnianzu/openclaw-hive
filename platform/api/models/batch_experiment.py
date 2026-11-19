from pydantic import BaseModel
from typing import Optional, List

from .instance import AgentConfig


# ============================================================================
# 批量实验：同一测试集 × N 个 Harness（× 重复次数）
#
# 一个「臂」(arm) = 矩阵的一列 = 一个 Harness（可精确到配置版本）。
# 一个臂 × 一个重复轮次 = 一个 task_instances，跑完整个测试集。
# 所以总任务数 = 臂数 × 重复次数 × 样本数。
# ============================================================================


class BatchArm(BaseModel):
    """实验的一个对比臂：一个 Harness（可精确到配置方案的某个已发布版本）。"""
    harness_type: str
    harness_config_id: Optional[int] = None
    harness_config_version_id: Optional[int] = None
    # 展示名，如 "OpenClaw·科研 v1.1"。留空时后端按 harness_type + 版本号拼。
    label: str = ""


class BatchCommonParams(BaseModel):
    """所有子任务共享的创建参数（InstanceCreate 的子集）。

    测试集相关字段（user_config_dir / total_num）**不在这里** —— 它们由
    实验选定的登记（task_registrations）决定，由后端填入，避免前端传了
    和测试集冲突的值。harness_* 也不在这里，那是每个臂自己的。
    """
    concurrent_num: int = 100
    image_name: Optional[str] = None
    max_ttl_second: int = 36000
    start_index: int = 0

    skill_dir: str = ""
    default_skills: str = ""
    agent_dir: str = ""
    user_profile_dir: str = ""
    traj_save_path: str = ""

    model_api_key: str = ""
    model_base_url: str = ""
    model_id: str = ""
    model_api_type: str = ""
    suffix_api_keys: bool = True
    proxy_enable: bool = False

    agents: List[AgentConfig] = []

    sandbox_config_id: Optional[int] = None
    code_repo_id: Optional[int] = None
    invite_code: str = ""


class BatchExperimentCreate(BaseModel):
    name: str
    description: str = ""
    registration_id: int                      # 测试集
    arms: List[BatchArm] = []
    repeat_times: int = 1
    schedule_mode: str = "shared"             # shared | isolated | serial
    max_concurrent: int = 20
    common: BatchCommonParams = BatchCommonParams()


class BatchExperimentInfo(BaseModel):
    id: str
    name: str
    description: str = ""
    registration_id: Optional[int] = None
    test_set_path: str = ""
    test_set_name: str = ""
    sample_total: int = 0
    repeat_times: int = 1
    schedule_mode: str = "shared"
    queue_id: Optional[str] = None
    max_concurrent: int = 20
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
    """结果对比页「总体评测结果」的一行（一个臂一行）。"""
    item_id: int
    instance_id: Optional[str] = None
    arm_label: str = ""
    harness_type: str = ""
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
