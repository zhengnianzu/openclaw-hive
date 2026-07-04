from pydantic import BaseModel
from typing import Optional


class UserCreate(BaseModel):
    username: str
    password: str


class UserLogin(BaseModel):
    username: str
    password: str


class UserRegister(BaseModel):
    username: str
    password: str


class UserRoleUpdate(BaseModel):
    role: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str = ""
    role: str = ""


class UserInfo(BaseModel):
    id: int
    username: str
    is_active: bool
    role: str = "viewer"
    created_at: str
