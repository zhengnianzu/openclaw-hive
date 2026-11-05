from fastapi import APIRouter, Depends, HTTPException, status

from ..core.config import settings
from ..core.database import get_connection
from ..core.security import (
    verify_password, get_password_hash, create_access_token,
    get_current_user, require_admin, invalidate_user_cache,
)
from ..models.auth import (
    UserLogin, UserRegister, UserCreate, UserRoleUpdate,
    PasswordChange, PasswordReset, Token, UserInfo,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _create_user(username: str, password: str, role: str) -> None:
    """共用的建号逻辑：自助注册与管理员创建走同一套校验，口径不会跑偏。"""
    if len(username) < 2:
        raise HTTPException(status_code=400, detail="用户名至少2个字符")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="密码至少4个字符")
    with get_connection() as conn:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="用户名已存在")
        conn.execute(
            "INSERT INTO users (username, hashed_password, role) VALUES (?, ?, ?)",
            (username, get_password_hash(password), role),
        )


@router.get("/registration-open")
def registration_open():
    """登录页据此决定是否显示「去注册」入口。未登录可访问。"""
    return {"open": settings.ALLOW_REGISTRATION}


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
    if not settings.ALLOW_REGISTRATION:
        raise HTTPException(status_code=403, detail="注册已关闭，请联系管理员开通账号")
    _create_user(req.username, req.password, "viewer")
    token = create_access_token(data={"sub": req.username})
    return {"access_token": token, "token_type": "bearer",
            "username": req.username, "role": "viewer"}


@router.post("/users", response_model=UserInfo)
def create_user(req: UserCreate, user: dict = Depends(require_admin)):
    """注册关闭后，这里是新增账号的唯一入口。"""
    role = req.role or "viewer"
    if role not in ("viewer", "operator", "admin"):
        raise HTTPException(status_code=400, detail="角色必须是 viewer/operator/admin")
    _create_user(req.username, req.password, role)
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (req.username,)).fetchone()
    return UserInfo(**dict(row))


@router.get("/me", response_model=UserInfo)
def get_me(user: dict = Depends(get_current_user)):
    return UserInfo(**user)


@router.put("/me/password")
def change_my_password(req: PasswordChange, user: dict = Depends(get_current_user)):
    """改自己的密码，需验旧密码。

    返回新 token：JWT 只装 sub + exp，改密码不会让已签发的 token 失效
    （最长到过期为止）。前端换用新 token 没有安全收益，但能避免
    「改完密码后当前会话还剩几小时就莫名登出」的体验落差。
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT hashed_password FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
    if not row or not verify_password(req.old_password, row["hashed_password"]):
        raise HTTPException(status_code=400, detail="原密码错误")
    if len(req.new_password) < 4:
        raise HTTPException(status_code=400, detail="密码至少4个字符")
    if req.new_password == req.old_password:
        raise HTTPException(status_code=400, detail="新密码不能与原密码相同")
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET hashed_password = ? WHERE id = ?",
            (get_password_hash(req.new_password), user["id"]),
        )
    invalidate_user_cache(user["username"])
    token = create_access_token(data={"sub": user["username"]})
    return {"message": "密码已修改", "access_token": token, "token_type": "bearer",
            "username": user["username"], "role": user["role"]}


@router.put("/users/{user_id}/password")
def reset_user_password(user_id: int, req: PasswordReset, user: dict = Depends(require_admin)):
    """管理员重置他人密码（忘密码场景），不需要旧密码。

    不限制重置自己：admin 想借这里改自己的密码也合理，
    只是走 /me/password 更对路（那条要验旧密码）。
    """
    if len(req.new_password) < 4:
        raise HTTPException(status_code=400, detail="密码至少4个字符")
    with get_connection() as conn:
        row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        conn.execute(
            "UPDATE users SET hashed_password = ? WHERE id = ?",
            (get_password_hash(req.new_password), user_id),
        )
    invalidate_user_cache(row["username"])
    return {"message": f"已重置 {row['username']} 的密码"}


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
