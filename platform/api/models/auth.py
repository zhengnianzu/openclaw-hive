from pydantic import BaseModel
from typing import Optional


class UserCreate(BaseModel):
    username: str
    password: str
    role: Optional[str] = "viewer"


class UserLogin(BaseModel):
    username: str
    password: str


class UserRegister(BaseModel):
    username: str
    password: str


class UserRoleUpdate(BaseModel):
    role: str


class PasswordChange(BaseModel):
    """本人改密码：要验旧密码。"""
    old_password: str
    new_password: str


class PasswordReset(BaseModel):
    """管理员重置他人密码：不需要旧密码（忘密码场景）。"""
    new_password: str


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
