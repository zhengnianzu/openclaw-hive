from pydantic import BaseModel
from typing import Optional


class HarnessConfigCreate(BaseModel):
    name: str
    harness_type: str = "openclaw"
    version: str = "v1"
    description: str = ""
    obs_harness_path: str = ""
    obs_task_path: str = ""
    obs_proxy_path: str = ""


class HarnessConfigUpdate(BaseModel):
    name: Optional[str] = None
    version: Optional[str] = None
    description: Optional[str] = None
    config_files_json: Optional[str] = None
    obs_harness_path: Optional[str] = None
    obs_task_path: Optional[str] = None
    obs_proxy_path: Optional[str] = None


class HarnessConfigInfo(BaseModel):
    id: int
    name: str
    harness_type: str
    version: str = "v1"
    description: str = ""
    config_files_json: str = "[]"
    is_default: int = 0
    obs_source_path: str = ""
    obs_harness_path: str = ""
    obs_task_path: str = ""
    obs_proxy_path: str = ""
    created_by: str = ""
    created_at: str = ""
    updated_at: str = ""
