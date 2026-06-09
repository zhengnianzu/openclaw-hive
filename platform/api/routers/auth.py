from fastapi import APIRouter, HTTPException, status

from ..core.database import get_connection
from ..core.security import verify_password, get_password_hash, create_access_token
from ..models.auth import UserCreate, UserLogin, Token, UserInfo

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=UserInfo)
def register(req: UserCreate):
    with get_connection() as conn:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (req.username,)).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="用户名已存在")
        hashed = get_password_hash(req.password)
        cursor = conn.execute(
            "INSERT INTO users (username, hashed_password) VALUES (?, ?)",
            (req.username, hashed),
        )
        user = conn.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return UserInfo(**dict(user))


@router.post("/login", response_model=Token)
def login(req: UserLogin):
    with get_connection() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = ?", (req.username,)).fetchone()
    if not user or not verify_password(req.password, user["hashed_password"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    token = create_access_token(data={"sub": user["username"]})
    return Token(access_token=token)
