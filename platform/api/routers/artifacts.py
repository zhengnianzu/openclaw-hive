# -*- coding: utf-8 -*-
"""沙箱产物浏览端点（实例详情「工作区」tab）。

**读路径全部走本地磁盘，零 obsutil 调用。** 产物由 offline/output_worker.py 的
`_consume_artifacts` 全量预下载到 `OUTPUT_CACHE/<batch>/<leaf>/`，这里只做遍历、
读取与预览。唯一会触发 OBS 的是 `POST /request`（running 实例按需下载），
它只写一行 pending，真正的下载在 worker 进程里做。

安全：
  - 所有路径过 `clean_rel` + `safe_join`（realpath 包含性校验），拒绝 `..`/反斜杠/NUL。
  - task 必须在该实例已知集合内，防止枚举别的实例的目录。
  - `/raw` 强制 `Content-Disposition: attachment`：沙箱 agent 产出的 html 若在平台
    同源下内联打开，里面的 `<script>` 能读 localStorage 的 token。
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response

from ..core.config import settings
from ..core.database import async_execute, async_query
from ..core.security import get_current_user, require_operator
from ..models.artifact import ArtifactTaskItem, ArtifactTasksResponse, ArtifactTreeResponse
from ..services import artifact_store as store

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])

# 内联响应的 CSP：iframe srcdoc/blob 预览时，这条头是唯一真正的沙箱边界
_INLINE_CSP = ("sandbox; default-src 'none'; img-src data: blob:; "
               "style-src 'unsafe-inline'; script-src 'unsafe-inline'")
_PDF_CSP = "sandbox; default-src 'none'; object-src 'none'"


def _cache_subdir(obs_base: str, task: str) -> str:
    """OBS 前缀 → 本地缓存子目录，规则与 deep 详情完全一致。"""
    from src import offline_analysis as oa
    task_obs = obs_base.rstrip("/") + "/" + task.strip("/") + "/"
    return oa._cache_subdir_for(task_obs)


async def _resolve(instance_id: str, task: str) -> tuple[dict, str, str]:
    """取实例、校验 task 归属，返回 (inst, obs_base, task_dir)。

    task 必须在 `task_traj_records` 里（worker 按实例 config 列表建行）——只做前缀包含不够，
    否则能枚举同 batch 下别的实例的任务目录。
    """
    rows = await async_query("SELECT * FROM task_instances WHERE id=?", (instance_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="实例不存在")
    inst = rows[0]

    from src import offline_analysis as oa
    obs_base = oa.obs_base_from_snapshot(inst.get("config_snapshot"))
    if not obs_base:
        config_path = inst.get("config_path")
        if config_path and os.path.exists(config_path):
            try:
                from omegaconf import OmegaConf
                cfg = OmegaConf.load(config_path)
                obs = cfg.run_config.obs
                obs_base = (f"{str(obs.traj_save_bucket).rstrip('/')}/"
                            f"{str(obs.traj_save_path).strip('/')}/")
            except Exception:
                obs_base = None
    if not obs_base:
        raise HTTPException(status_code=400,
                            detail="该实例缺少 OBS 轨迹配置（config_snapshot 与 config.yaml 都取不到）")

    task = (task or "").strip().strip("/")
    if not task:
        raise HTTPException(status_code=400, detail="缺少 task 参数")
    known = await async_query(
        "SELECT traj_name FROM task_traj_records WHERE instance_id=?", (instance_id,))
    if task not in {r["traj_name"] for r in known}:
        raise HTTPException(status_code=404, detail=f"task 不属于该实例: {task}")

    try:
        subdir = _cache_subdir(obs_base, task)
        task_dir = store.task_local_dir(subdir)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"非法路径: {e}")
    return inst, obs_base, task_dir


def _file_path(task_dir: str, key: str) -> str:
    try:
        rel = store.clean_rel(key)
        path = store.safe_join(task_dir, rel)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"非法路径: {e}")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"文件不存在或尚未下载: {key}")
    return path


# ============ task 列表 ============

@router.get("/{instance_id}/tasks", response_model=ArtifactTasksResponse)
async def list_artifact_tasks(instance_id: str, user: dict = Depends(get_current_user)):
    """该实例的 task 列表 + 每个 task 的下载状态。零 obsutil。"""
    insts = await async_query("SELECT * FROM task_instances WHERE id=?", (instance_id,))
    if not insts:
        raise HTTPException(status_code=404, detail="实例不存在")

    from src import offline_analysis as oa
    inst = insts[0]
    obs_base = oa.obs_base_from_snapshot(inst.get("config_snapshot"))

    rows = await async_query(
        "SELECT traj_name, artifacts_status AS status, error, "
        "artifact_files AS file_count, artifact_bytes AS total_bytes, "
        "artifact_downloaded_at AS downloaded_at, artifact_complete "
        "FROM task_traj_records WHERE instance_id=? ORDER BY traj_name", (instance_id,))

    items: list[ArtifactTaskItem] = []
    for r in rows:
        # 未标记 complete 的（任务失败/未跑）不参与产物浏览，但列表里仍可见
        if not r["artifact_complete"]:
            continue
        task_dir = ""
        if obs_base:
            try:
                task_dir = store.task_local_dir(_cache_subdir(obs_base, r["traj_name"]))
            except ValueError:
                task_dir = ""
        local = bool(task_dir) and os.path.isdir(task_dir)
        items.append(ArtifactTaskItem(
            traj_name=r["traj_name"],
            status=r["status"] or "queued",
            error=r["error"],
            file_count=r["file_count"],
            total_bytes=r["total_bytes"],
            downloaded_at=r["downloaded_at"],
            downloaded=local,
        ))
    return ArtifactTasksResponse(instance_id=instance_id, obs_base=obs_base, tasks=items)


# ============ 目录树（一次返回全部，本地 IO） ============

@router.get("/{instance_id}/tree", response_model=ArtifactTreeResponse)
async def get_artifact_tree(
    instance_id: str,
    task: str = Query(description="task 目录名"),
    user: dict = Depends(get_current_user),
):
    """整个 task 的文件列表。本地遍历，毫秒级，无需懒加载。"""
    inst, obs_base, task_dir = await _resolve(instance_id, task)
    if not os.path.isdir(task_dir):
        raise HTTPException(status_code=409,
                            detail="该 task 尚未下载到本地，请先点「下载此任务」")
    entries = store.walk_tree(task_dir)
    return ArtifactTreeResponse(task=task, root=task_dir, entries=entries)


# ============ 内容 / 下载 / 预览 ============

@router.get("/{instance_id}/content")
async def get_artifact_content(
    instance_id: str,
    task: str = Query(...),
    key: str = Query(..., description="task 内相对路径"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200000, ge=1, le=2000000),
    user: dict = Depends(get_current_user),
):
    """文本分页读取（按字符，同 logs.py obs-view 的语义）。"""
    inst, obs_base, task_dir = await _resolve(instance_id, task)
    path = _file_path(task_dir, key)
    out = store.read_text_slice(path, offset, limit)
    out.update({"task": task, "key": store.clean_rel(key), "size": os.path.getsize(path)})
    return out


@router.get("/{instance_id}/raw")
async def download_artifact(
    instance_id: str,
    task: str = Query(...),
    key: str = Query(...),
    user: dict = Depends(get_current_user),
):
    """下载原文件。强制 attachment（防同源 XSS）。"""
    inst, obs_base, task_dir = await _resolve(instance_id, task)
    path = _file_path(task_dir, key)
    name = os.path.basename(store.clean_rel(key))
    return FileResponse(
        path,
        filename=name,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{instance_id}/inline")
async def inline_artifact(
    instance_id: str,
    task: str = Query(...),
    key: str = Query(...),
    user: dict = Depends(get_current_user),
):
    """内联返回文件字节，供前端 blob/iframe 预览。"""
    inst, obs_base, task_dir = await _resolve(instance_id, task)
    path = _file_path(task_dir, key)
    media, inline_ok = store.media_type(store.clean_rel(key))
    with open(path, "rb") as f:
        body = f.read()
    headers = {"X-Content-Type-Options": "nosniff"}
    if inline_ok:
        headers["Content-Security-Policy"] = _INLINE_CSP
    return Response(content=body, media_type=media, headers=headers)


@router.get("/{instance_id}/pdf")
async def artifact_pdf(
    instance_id: str,
    task: str = Query(...),
    key: str = Query(...),
    user: dict = Depends(get_current_user),
):
    """office 文件转 PDF 预览。未安装 LibreOffice 时 503。

    转换是 CPU/进程密集（soffice 单实例），放到线程池里跑，不阻塞事件循环。
    """
    inst, obs_base, task_dir = await _resolve(instance_id, task)
    if not store.soffice_available():
        raise HTTPException(
            status_code=503,
            detail="服务器未安装 LibreOffice，无法在线预览该格式，请下载后查看")

    rel = store.clean_rel(key)
    src = _file_path(task_dir, rel)
    try:
        pdf = await async_execute(store.ensure_pdf, task_dir, rel)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"文件不存在: {key}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"转换失败: {str(e)[:300]}")

    with open(pdf, "rb") as f:
        body = f.read()
    return Response(content=body, media_type="application/pdf",
                    headers={"Content-Security-Policy": _PDF_CSP,
                             "X-Content-Type-Options": "nosniff"})


# ============ 按需下载 ============

@router.post("/{instance_id}/request")
async def request_artifact_download(
    instance_id: str,
    task: str = Query(...),
    user: dict = Depends(require_operator),
):
    """把某 task 排进产物下载队列（worker 下一轮消费）。

    running 实例不自动下载，用户点开工作区时才走这里。
    """
    insts = await async_query("SELECT id FROM task_instances WHERE id=?", (instance_id,))
    if not insts:
        raise HTTPException(status_code=404, detail="实例不存在")

    rows = await async_query(
        "SELECT traj_name, artifacts_status FROM task_traj_records "
        "WHERE instance_id=? AND traj_name=?",
        (instance_id, task.strip("/")))
    if not rows:
        raise HTTPException(
            status_code=404,
            detail="该 task 尚未被 worker 发现；等 worker 扫过该批次后重试")

    cur = rows[0]
    if cur["status"] == "downloading":
        return {"task": task, "status": "downloading"}
    if cur["status"] == "done":
        return {"task": task, "status": "done"}

    await async_execute(
        lambda: _requeue_artifact(instance_id, task.strip("/")))
    return {"task": task, "status": "pending"}


def _requeue_artifact(instance_id: str, task: str) -> None:
    from ..core.database import get_connection
    with get_connection() as conn:
        conn.execute(
            "UPDATE task_traj_records SET artifacts_status='queued', error=NULL, "
            "updated_at=CURRENT_TIMESTAMP "
            "WHERE instance_id=? AND traj_name=? AND artifacts_status<>'downloading'",
            (instance_id, task))
        conn.commit()
