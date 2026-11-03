from pydantic import BaseModel
from typing import Optional


class ImageCreate(BaseModel):
    name: str
    address: str
    harness_type: str = "openclaw"
    os: str = "linux"


class ImageUpdate(BaseModel):
    os: str


class ImageInfo(BaseModel):
    id: int
    name: str
    address: str
    harness_type: str
    os: str = "linux"
    created_at: str
    created_by: str = ""
