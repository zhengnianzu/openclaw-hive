from fastapi import APIRouter, Depends, HTTPException, status

from ..core.database import get_connection
from ..core.security import (
    verify_password, get_password_hash, create_access_token,
    get_current_user, require_admin,
)
from ..models.auth import UserLogin, UserRegister, UserRoleUpdate, Token, UserInfo

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
def login(req: UserLogin):
    with get_connection() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = ?", (req.username,)).fetchone()
    if not user or not verify_password(req.password, user["hashed_password"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    token = create_access_token(data={"sub": user["username"]})
    return {"access_token": token, "token_type": "bearer",
            "username": user["username"], "role": user["role"]}


@router.post("/register")
def register(req: UserRegister):
    if len(req.username) < 2:
        raise HTTPException(status_code=400, detail="用户名至少2个字符")
    if len(req.password) < 4:
        raise HTTPException(status_code=400, detail="密码至少4个字符")
    with get_connection() as conn:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (req.username,)).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="用户名已存在")
        hashed = get_password_hash(req.password)
        conn.execute(
            "INSERT INTO users (username, hashed_password, role) VALUES (?, ?, 'viewer')",
            (req.username, hashed),
        )
    token = create_access_token(data={"sub": req.username})
    return {"access_token": token, "token_type": "bearer",
            "username": req.username, "role": "viewer"}


@router.get("/me", response_model=UserInfo)
def get_me(user: dict = Depends(get_current_user)):
    return UserInfo(**user)


@router.get("/users", response_model=list[UserInfo])
def list_users(user: dict = Depends(require_admin)):
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    return [UserInfo(**dict(r)) for r in rows]


@router.put("/users/{user_id}/role")
def update_user_role(user_id: int, req: UserRoleUpdate, user: dict = Depends(require_admin)):
    if req.role not in ("viewer", "operator", "admin"):
        raise HTTPException(status_code=400, detail="角色必须是 viewer/operator/admin")
    if user["id"] == user_id:
        raise HTTPException(status_code=400, detail="不能修改自己的角色")
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (req.role, user_id))
    return {"message": "角色已更新"}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, user: dict = Depends(require_admin)):
    if user["id"] == user_id:
        raise HTTPException(status_code=400, detail="不能删除自己")
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return {"message": "用户已删除"}
