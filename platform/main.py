from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import json
import os
import shutil
import sys

import httpx
from fastapi import Depends, HTTPException, Query
from typing import Optional

from api.core.config import settings
from api.core.database import init_db, get_connection
from api.core.security import get_current_user, get_password_hash
from api.routers import auth, instances, obs, logs

# 检查 settings 目录：如果实际配置文件不存在，从 .example 复制
SETTINGS_FILES = ["config.yaml", "openclaw.json", "user_proxy_model.json"]
for name in SETTINGS_FILES:
    actual = os.path.join(settings.SETTINGS_DIR, name)
    example = actual.rsplit(".", 1)
    example = f"{actual}.example" if not actual.endswith(".yaml") else actual.replace(".yaml", ".yaml.example")
    # config.yaml → config.yaml.example, openclaw.json → openclaw.json.example
    example = f"{actual}.example"
    if not os.path.exists(actual):
        if os.path.exists(example):
            shutil.copy2(example, actual)
            print(f"已从 {os.path.basename(example)} 创建 {os.path.basename(actual)}，请编辑填入实际密钥")
        else:
            print(f"警告: {actual} 和 {example} 均不存在，请先创建配置文件", file=sys.stderr)

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
        try:
            conn.execute("INSERT INTO users (username, hashed_password) VALUES (?, ?)", (settings.ADMIN_USERNAME, hashed))
            print(f"已创建管理员账号: {settings.ADMIN_USERNAME}")
        except Exception:
            pass


@app.get("/api/health")
def health():
    return {"status": "ok", "project": settings.PROJECT_NAME}


@app.post("/api/generate-api-key")
async def generate_api_key(
    base_url: Optional[str] = None,
    invite_code: str = "pangu",
    name: str = "test-user",
    user: dict = Depends(get_current_user),
):
    if not base_url:
        openclaw_template = os.path.join(settings.SETTINGS_DIR, "openclaw.json")
        with open(openclaw_template, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        base_url = cfg["models"]["providers"]["local"]["baseUrl"]
    api_url = base_url.rstrip("/") + f"/api/invite?invite_code={invite_code}&name={name}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(api_url)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"API Key 服务返回错误: {e.response.text[:200]}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"无法连接 API Key 服务: {str(e)[:200]}")


static_dir = os.path.join(os.path.dirname(__file__), "frontend", "dist")
if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")
