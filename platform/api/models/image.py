from pydantic import BaseModel
from typing import Optional


class ImageCreate(BaseModel):
    name: str
    address: str
    harness_type: str = "openclaw"


class ImageInfo(BaseModel):
    id: int
    name: str
    address: str
    harness_type: str
    created_at: str
    created_by: str = ""
