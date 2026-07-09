from pydantic import BaseModel
from typing import Optional


class ConfigTemplateCreate(BaseModel):
    name: str = "默认配置"
    harness_type: str = "openclaw"
    model_base_url: str = ""
    invite_code: str = "pangu"
    model_api_type: str = ""
    model_id: str = ""
    agents_json: str = "[]"
    image_name: str = ""
    code_repo_id: Optional[int] = None


class ConfigTemplateUpdate(BaseModel):
    name: Optional[str] = None
    harness_type: Optional[str] = None
    model_base_url: Optional[str] = None
    invite_code: Optional[str] = None
    model_api_type: Optional[str] = None
    model_id: Optional[str] = None
    agents_json: Optional[str] = None
    image_name: Optional[str] = None
    code_repo_id: Optional[int] = None


class ConfigTemplateInfo(BaseModel):
    id: int
    name: str = "默认配置"
    owner: str
    is_default: int = 0
    harness_type: str = "openclaw"
    model_base_url: str = ""
    invite_code: str = "pangu"
    model_api_type: str = ""
    model_id: str = ""
    agents_json: str = "[]"
    image_name: str = ""
    code_repo_id: Optional[int] = None
    created_at: str = ""
    updated_at: str = ""
