from fastapi import APIRouter, HTTPException, status

from ..core.database import get_connection
from ..core.security import verify_password, create_access_token
from ..models.auth import UserLogin, Token

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=Token)
def login(req: UserLogin):
    with get_connection() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = ?", (req.username,)).fetchone()
    if not user or not verify_password(req.password, user["hashed_password"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    token = create_access_token(data={"sub": user["username"]})
    return Token(access_token=token)
