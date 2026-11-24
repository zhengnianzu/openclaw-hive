from pydantic import BaseModel
from typing import Optional


class RegistrationCreate(BaseModel):
    task_name: str
    requester: str = ""
    task_path_obs: str = ""
    data_total: int = 0
    skill_dir_obs: str = ""
    agent_dir_obs: str = ""
    user_folder_obs: str = ""
    model_name: str = ""
    eval_model_name: str = ""
    user_proxy_model_name: str = ""
    harness_type: str = "openclaw"
    base_url: str = ""
    api_key: str = ""
    eval_config_model: str = ""
    eval_config_base_url: str = ""
    eval_config_api_key: str = ""
    eval_config_api: str = ""
    default_skills: str = ""
    traj_save_path: str = ""
    remark: str = ""
    # config_template_id 已废弃，DB 列保留但不再由 API 写入


class RegistrationUpdate(BaseModel):
    export_path_obs: Optional[str] = None
    traj_path: Optional[str] = None
    task_name: Optional[str] = None
    requester: Optional[str] = None
    task_path_obs: Optional[str] = None
    data_total: Optional[int] = None
    skill_dir_obs: Optional[str] = None
    agent_dir_obs: Optional[str] = None
    user_folder_obs: Optional[str] = None
    model_name: Optional[str] = None
    eval_model_name: Optional[str] = None
    user_proxy_model_name: Optional[str] = None
    harness_type: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    eval_config_model: Optional[str] = None
    eval_config_base_url: Optional[str] = None
    eval_config_api_key: Optional[str] = None
    eval_config_api: Optional[str] = None
    default_skills: Optional[str] = None
    traj_save_path: Optional[str] = None
    remark: Optional[str] = None
    # 只用于给这次保存产生的版本写说明，不写回主表（见 registrations.update_registration）
    change_summary: Optional[str] = None


class RegistrationInfo(BaseModel):
    id: int
    seq: Optional[int] = 0
    created_at: str
    created_by: str
    task_name: str
    requester: str = ""
    task_path_obs: str = ""
    data_total: int = 0
    skill_dir_obs: str = ""
    agent_dir_obs: str = ""
    user_folder_obs: str = ""
    export_path_obs: str = ""
    status: str = "pending"
    linked_instance_id: Optional[str] = None
    traj_path: str = ""
    completed_tasks: int = 0
    failed_tasks: int = 0
    model_name: str = ""
    eval_model_name: str = ""
    user_proxy_model_name: str = ""
    harness_type: str = "openclaw"
    base_url: str = ""
    api_key: str = ""
    eval_config_model: str = ""
    eval_config_base_url: str = ""
    eval_config_api_key: str = ""
    eval_config_api: str = ""
    default_skills: str = ""
    traj_save_path: str = ""
    remark: str = ""
    count_status: str = ""
    count_error: str = ""
    counted_at: Optional[str] = None
    # 版本功能：当前版本号 + 列表页用的两个聚合字段（与 HarnessConfigInfo 同款）
    updated_at: Optional[str] = None
    current_version: int = 0
    version_count: int = 0
    latest_change: str = ""
    # 本地副本：注册后整份下载，编辑只改本地，云端由 sync_worker 异步回传
    download_status: str = ""        # '' | downloading | done | error
    download_error: str = ""
    downloaded_at: Optional[str] = None
    modified_count: int = 0          # 本地改过、尚未同步到云端的文件数
    last_synced_at: Optional[str] = None


class RegistrationVersionInfo(BaseModel):
    """一次编辑的不可变快照。snapshot_json / change_json 是原样透传的 JSON 字符串，
    由前端解析——后端不做字段裁剪，历史记录必须能原样回放。"""
    id: int
    registration_id: int
    version: int
    snapshot_json: str = "{}"
    change_json: str = "[]"
    summary: str = ""
    created_by: str = ""
    created_at: str = ""
