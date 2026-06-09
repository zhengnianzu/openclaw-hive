from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import os

from api.core.config import settings
from api.core.database import init_db, get_connection
from api.core.security import get_password_hash
from api.routers import auth, instances, obs, logs

app = FastAPI(title=settings.PROJECT_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(instances.router)
app.include_router(obs.router)
app.include_router(logs.router)

init_db()

with get_connection() as conn:
    existing = conn.execute("SELECT id FROM users WHERE username = ?", (settings.ADMIN_USERNAME,)).fetchone()
    if not existing:
        hashed = get_password_hash(settings.ADMIN_PASSWORD)
        conn.execute("INSERT INTO users (username, hashed_password) VALUES (?, ?)", (settings.ADMIN_USERNAME, hashed))
        print(f"已创建管理员账号: {settings.ADMIN_USERNAME}")

static_dir = os.path.join(os.path.dirname(__file__), "frontend", "dist")
if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")


@app.get("/api/health")
def health():
    return {"status": "ok", "project": settings.PROJECT_NAME}
