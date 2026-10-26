from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
import json
import os
import shutil
import sys

import httpx
from fastapi import Depends, HTTPException, Query
from typing import Optional

from api.core.config import settings
from api.core.database import init_db, init_db_lock, init_db_unlocked, get_connection
from api.core.security import get_current_user, get_password_hash
from api.routers import auth, instances, obs, logs, registrations, images, code_repos, config_templates, harness_configs, harness_meta, queues, sandbox_configs
from api.routers import batch_output, resource_pools, stats
from api.routers import wiki, notifications, settings_overview
from api.routers import public_api
from api.routers import artifacts
from api.routers import worker_monitor
# 检查 settings 目录：如果实际配置文件不存在，从 .example 复制
# 子目录项（worker/config.yaml）也在这里，os.path.join 能正常处理；
# 复制前需要先确保父目录存在，否则 copy2 会 FileNotFoundError。
SETTINGS_FILES = ["config.yaml", "openclaw.json", "user_proxy_model.json", "hermes_config.yaml",
                  "cc_settings.json", os.path.join("worker", "config.yaml")]
for name in SETTINGS_FILES:
    actual = os.path.join(settings.SETTINGS_DIR, name)
    example = f"{actual}.example"
    if not os.path.exists(actual):
        if os.path.exists(example):
            os.makedirs(os.path.dirname(actual) or ".", exist_ok=True)
            shutil.copy2(example, actual)
            print(f"已从 {os.path.basename(example)} 创建 {os.path.basename(actual)}，请编辑填入实际密钥")
        else:
            print(f"警告: {actual} 和 {example} 均不存在，请先创建配置文件", file=sys.stderr)

app = FastAPI(title=settings.PROJECT_NAME)

# 前端打包产物单文件 1.2MB、列表接口单次 650KB，gzip 后约降到 1/3。
# 未挂这个中间件时，首屏和轮询都在传未压缩的 JSON/JS。
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(instances.router)
app.include_router(instances.key_router)
app.include_router(queues.router)
app.include_router(obs.router)
app.include_router(logs.router)
app.include_router(registrations.router)
app.include_router(images.router)
app.include_router(code_repos.router)
app.include_router(config_templates.router)
app.include_router(harness_configs.router)
app.include_router(harness_meta.router)
app.include_router(sandbox_configs.router)
# 资源池总量（首页看板的分母），只读 settings/sandbox/resource_pools.yaml
app.include_router(resource_pools.router)
# 资源看板（/analytics）：用户排行、harness/OS 比例、池占用、趋势重建
app.include_router(stats.router)
# 批跑作业「输出」页（task_records + task_traj_records；worker 独立进程，不起 co-routine）
app.include_router(batch_output.router)
# 文档中心（Wiki）：管理员手写更新说明，可关联配置实体、发布为全局通知
app.include_router(wiki.router)
# 全局站内通知：广播 + 已读表（纯请求驱动，无后台循环）
app.include_router(notifications.router)
# 系统设置总览：一次返回所有配置实体入口 + 计数
app.include_router(settings_overview.router)
# 对外 API：固定 AK 鉴权（X-API-Key），不涉用户身份，见 routers/public_api.py
app.include_router(public_api.router)
# 沙箱产物浏览（实例详情「工作区」tab）：只读本地磁盘，产物由 offline worker 全量预下载
app.include_router(artifacts.router)
# Worker 调度监控（资源看板）：心跳 / 进程指标 / 队列积压 / ETA
app.include_router(worker_monitor.router)

# 启动期的**所有**写库动作必须共享同一把跨进程锁。
# uvicorn --workers 4 会起 4 个进程，每个进程 import main 时都会把这段跑一遍；
# 之前只把 init_db() 包进锁，后面三步仍在锁外，4 个进程并发抢同一个 SQLite 写锁，
# 实测启动时 harness_configs.ensure_defaults() 抛 database is locked，
# 该 worker 直接起不来（日志里 INFO: Child process [xxx] died）。
#
# 注意调用的是 init_db_unlocked()，不是 init_db()：后者内部自己会拿同一把锁，
# 这里已经持有，再进一次就是 flock 自死锁（同一进程两个 fd 也互斥），
# 实测卡在 locks_lock_inode_wait 永不返回。
with init_db_lock():
    init_db_unlocked()
    harness_configs.ensure_defaults()
    sandbox_configs.ensure_defaults()

    with get_connection() as conn:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (settings.ADMIN_USERNAME,)).fetchone()
        if not existing:
            hashed = get_password_hash(settings.ADMIN_PASSWORD)
            try:
                conn.execute("INSERT INTO users (username, hashed_password, role) VALUES (?, ?, 'admin')", (settings.ADMIN_USERNAME, hashed))
                print(f"已创建管理员账号: {settings.ADMIN_USERNAME}")
            except Exception:
                pass


@app.get("/api/health")
def health():
    return {"status": "ok", "project": settings.PROJECT_NAME}


@app.on_event("startup")
async def _start_background_tasks():
    import asyncio
    # 回收「准备中被重启中断」的孤儿实例，退回 created 供重新启动（幂等，多 worker 安全）
    try:
        instances.recover_orphan_preparing()
    except Exception as e:
        print(f"[startup] recover_orphan_preparing error: {e}")
    asyncio.create_task(instances.background_cache_refresher())
    # 调度是事件驱动的，重启后若整条队列都停在 queued，没有任何事件会再触发它，
    # 任务会被永久搁死。启动时对所有含排队任务的队列兜底调度一次。
    try:
        from api.services.queue_scheduler import resume_all_queues
        n = await resume_all_queues()
        if n:
            print(f"[startup] 恢复 {n} 个队列的排队任务")
    except Exception as e:
        print(f"[startup] resume_all_queues error: {e}")


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


# 静态文件 fallback（无 Nginx 时使用）
static_dir = os.path.join(os.path.dirname(__file__), "frontend", "dist")
if os.path.isdir(static_dir):
    # StaticFiles 默认不给 HTML 发 Cache-Control，浏览器可能长期沿用旧的 index.html，
    # 进而继续加载旧的 hash 化 JS —— 发版后用户端一直跑旧前端（曾出现新版批量接口
    # 只被调用 39 次、旧式逐个 /overview 却被调用 4478 次）。
    # 这里显式给 index.html 加 no-cache（每次回源校验），带 hash 的 assets 保持不变。
    class NoCacheHTMLStaticFiles(StaticFiles):
        async def get_response(self, path, scope):
            resp = await super().get_response(path, scope)
            if path in ("", "index.html") or (path or "").endswith("/"):
                resp.headers["Cache-Control"] = "no-cache, must-revalidate"
            elif (path or "").startswith("assets/"):
                # Vite 产出的文件名带内容 hash，内容变了名字就变，可以放心长期缓存。
                # 没有这行时每次进入资源看板都要回源协商（日志里成片的 304），
                # 536KB 的 Analytics chunk 拿不到磁盘缓存。
                resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return resp

    app.mount("/", NoCacheHTMLStaticFiles(directory=static_dir, html=True), name="frontend")
