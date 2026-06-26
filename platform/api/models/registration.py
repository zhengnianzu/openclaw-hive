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


class RegistrationInfo(BaseModel):
    id: int
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
