from typing import Optional

from pydantic import BaseModel


class SandboxConfigCreate(BaseModel):
    """新建：索引字段 + 要写进派生文件的沙箱字段。"""
    name: str
    sandbox_type: str = "linux"               # linux | windows
    sandbox_key: Optional[str] = None          # 留空时按类型派生（linux→x86_cpu, windows→windows）
    description: str = ""

    # 以下字段写进 settings/sandbox/<type>_<id>.yaml
    # 资源池 = sandbox_namespace（同一 namespace 共享一份配额）
    controlplane_base_url: str = ""
    controlplane_api_key: str = ""
    sandbox_namespace: str = "omni-env-default-worker"
    cpu_request: str = "2"
    cpu_limit: str = "3"
    memory_request: str = "6Gi"
    memory_limit: str = "8Gi"
    env_json: str = "[]"
    ttl_seconds: int = 8000
    lease_ttl_seconds: int = 3600
    wait_timeout_seconds: int = 3600
    image: str = ""
    s3_uploader_script: str = ""
    s3_download_script: str = ""


class SandboxConfigUpdate(BaseModel):
    """编辑：只允许改索引里的 name/description，其余走文件。"""
    name: Optional[str] = None
    description: Optional[str] = None
    # 表单字段（写进派生文件）—— sandbox_type / sandbox_key 不可改
    controlplane_base_url: Optional[str] = None
    controlplane_api_key: Optional[str] = None
    sandbox_namespace: Optional[str] = None
    cpu_request: Optional[str] = None
    cpu_limit: Optional[str] = None
    memory_request: Optional[str] = None
    memory_limit: Optional[str] = None
    env_json: Optional[str] = None
    ttl_seconds: Optional[int] = None
    lease_ttl_seconds: Optional[int] = None
    wait_timeout_seconds: Optional[int] = None
    image: Optional[str] = None
    s3_uploader_script: Optional[str] = None
    s3_download_script: Optional[str] = None


class SandboxConfigInfo(BaseModel):
    """索引字段 + 从派生文件读出的字段（响应合成）。"""
    id: int
    name: str
    sandbox_type: str
    sandbox_key: str
    file: str = ""                            # 派生文件名，如 windows_3.yaml
    file_exists: bool = True
    is_default: int = 0
    enabled: int = 1
    description: str = ""
    created_by: str = ""
    created_at: str = ""
    updated_at: str = ""

    # 由文件内容派生（只读，供列表与表单回填）
    controlplane_base_url: str = ""
    controlplane_api_key: str = ""
    sandbox_namespace: str = "omni-env-default-worker"
    cpu_request: str = "2"
    cpu_limit: str = "3"
    memory_request: str = "6Gi"
    memory_limit: str = "8Gi"
    env_json: str = "[]"
    ttl_seconds: int = 8000
    lease_ttl_seconds: int = 3600
    wait_timeout_seconds: int = 3600
    image: str = ""
    s3_uploader_script: str = ""
    s3_download_script: str = ""


class SandboxFileContent(BaseModel):
    filename: str
    path: str = ""
    content: str = ""
    template: str = ""                        # 对应的模板文件名
