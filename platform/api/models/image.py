from pydantic import BaseModel
from typing import Optional


class ImageCreate(BaseModel):
    name: str
    address: str
    harness_type: str = "openclaw"
    os: str = "linux"
    remark: str = ""


class ImageUpdate(BaseModel):
    # 字段一律可选：只改备注时不该连带覆盖 os，反之亦然。
    # 没有 address —— 实例/模板引用的是地址，改了会对不上历史任务。
    name: Optional[str] = None
    os: Optional[str] = None
    remark: Optional[str] = None


class ImageInfo(BaseModel):
    id: int
    name: str
    address: str
    harness_type: str
    os: str = "linux"
    remark: str = ""
    created_at: str
    created_by: str = ""
