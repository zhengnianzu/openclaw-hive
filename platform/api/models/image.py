from pydantic import BaseModel
from typing import List, Optional


class ImageCreate(BaseModel):
    name: str
    address: str
    # 一个镜像可映射多个 harness(含 common 兜底类), 形成多对多
    harness_types: List[str] = ["openclaw"]
    os: str = "linux"
    remark: str = ""
    # 设为默认镜像: 对其映射的每个 harness 唯一(旧的自动取消)
    is_default: bool = False


class ImageUpdate(BaseModel):
    # 没有 address —— 实例/模板引用的是地址，改了会对不上历史任务。
    name: Optional[str] = None
    os: Optional[str] = None
    remark: Optional[str] = None
    # 整体替换 harness 映射; 默认标记只保留仍在映射内的 harness
    harness_types: Optional[List[str]] = None
    is_default: Optional[bool] = None
    # 创建页可见性开关: 停用后高级配置下拉不再列出(存量实例引用不受影响)
    enabled: Optional[bool] = None


class ImageInfo(BaseModel):
    id: int
    name: str
    address: str
    harness_types: List[str] = []
    # 该镜像作为默认镜像的 harness 列表(通常 0 或多个)
    defaults: List[str] = []
    os: str = "linux"
    remark: str = ""
    enabled: bool = True
    created_at: str
    created_by: str = ""
