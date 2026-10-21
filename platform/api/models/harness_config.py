from pydantic import BaseModel
from typing import Optional


# ============================================================================
# 方案（harness_configs）：业务用途层面的配置，如「研究版本」「编码版本」
# ============================================================================

class HarnessConfigCreate(BaseModel):
    name: str
    harness_type: str = "openclaw"
    description: str = ""
    obs_harness_path: str = ""
    obs_task_path: str = ""
    obs_proxy_path: str = ""


class HarnessConfigUpdate(BaseModel):
    # name 可以改：目录名是 harness-<id>，与名称无关，改名只更新 DB。
    # 不含 version —— 版本号是目录名，改它会让磁盘与 DB 脱节；产生新版本请用「创建版本」。
    name: Optional[str] = None
    description: Optional[str] = None
    config_files_json: Optional[str] = None
    obs_harness_path: Optional[str] = None
    obs_task_path: Optional[str] = None
    obs_proxy_path: Optional[str] = None


class HarnessConfigInfo(BaseModel):
    id: int
    name: str
    harness_type: str
    slug: str = ""
    # 方案目录相对 HARNESS_DIR 的路径；为空时按 harness/<type>/harness-<id> 推导
    harness_dir: str = ""
    # 兼容旧前端：老模型里 version 是主键，新模型下它代表"迁移前版本"，可留空
    version: str = ""
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
    # 聚合字段：方案下的版本概况
    version_count: int = 0
    latest_version: str = ""
    latest_status: str = ""
    published_count: int = 0


# ============================================================================
# 版本（harness_config_versions）：方案下的不可变快照
# ============================================================================

class HarnessConfigVersionCreate(BaseModel):
    version: str
    description: str = ""


class HarnessConfigVersionStatus(BaseModel):
    status: str  # draft | testing | published


class HarnessConfigVersionRollback(BaseModel):
    version: str
    description: str = ""


class HarnessConfigVersionInfo(BaseModel):
    id: int
    config_id: int
    version: str
    status: str = "draft"
    description: str = ""
    files_json: str = "[]"
    obs_harness_path: str = ""
    obs_task_path: str = ""
    obs_proxy_path: str = ""
    created_by: str = ""
    created_at: str = ""
    published_at: Optional[str] = None
    published_by: str = ""
