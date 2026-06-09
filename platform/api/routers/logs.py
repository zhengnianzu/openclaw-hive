import asyncio
import os
import re
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from ..core.config import settings
from ..core.database import get_connection
from ..core.security import get_current_user

router = APIRouter(prefix="/api/logs", tags=["logs"])


def _get_output_dir(config_path: str) -> str:
    config_basename = Path(config_path).stem
    instance_dir = str(Path(config_path).parent)
    return os.path.join(instance_dir, "outputs", config_basename)


def _get_instance(instance_id: str) -> dict:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")
    return dict(row)


@router.get("/{instance_id}/main")
async def get_main_log(
    instance_id: str,
    tail: int = Query(default=200, description="返回最后N行"),
    task_filter: str = Query(default="", description="按 env_id 或 config_name 过滤"),
    user: dict = Depends(get_current_user),
):
    inst = _get_instance(instance_id)
    output_dir = _get_output_dir(inst["config_path"])
    log_file = os.path.join(output_dir, "nohup.log")
    if not os.path.exists(log_file):
        return {"lines": [], "total_lines": 0}

    if task_filter:
        lines = await _extract_task_lines(log_file, task_filter)
        total = len(lines)
        lines = lines[-tail:] if tail < total else lines
        return {"lines": lines, "total_lines": total}

    async with aiofiles.open(log_file, "r", errors="replace") as f:
        all_lines = await f.readlines()

    total = len(all_lines)
    lines = all_lines[-tail:] if tail < total else all_lines
    return {"lines": [l.rstrip("\n") for l in lines], "total_lines": total}


async def _extract_task_lines(log_file: str, task_filter: str) -> list[str]:
    """
    按 Worker 区间提取某个任务的完整日志。
    Worker 从 "Worker X starting task Y: config_name" 开始，
    到 "Worker X finished task Y" 或 "Worker X starting task Z" 结束。
    """
    start_re = re.compile(r"Worker (\d+) starting task (\d+): (.+?) =")
    env_re = re.compile(r"Task (\d+): env=(\w+)")
    finish_re = re.compile(r"Worker (\d+) (?:finished|error on) task")

    # 第一遍：找到目标任务对应的 worker_id 和 task_idx
    target_workers = set()
    async with aiofiles.open(log_file, "r", errors="replace") as f:
        async for line in f:
            m = start_re.search(line)
            if m and task_filter in line:
                target_workers.add((m.group(1), m.group(2)))
            m = env_re.search(line)
            if m and task_filter in line:
                target_workers.add((None, m.group(1)))

    if not target_workers:
        return []

    target_task_idxs = {t[1] for t in target_workers}

    # 第二遍：按 Worker 区间提取日志
    result = []
    active_workers = {}
    async with aiofiles.open(log_file, "r", errors="replace") as f:
        async for line in f:
            stripped = line.rstrip("\n")

            m = start_re.search(stripped)
            if m:
                worker_id, task_idx = m.group(1), m.group(2)
                if task_idx in target_task_idxs:
                    active_workers[worker_id] = True
                    result.append(stripped)
                    continue
                else:
                    active_workers.pop(worker_id, None)

            m = finish_re.search(stripped)
            if m:
                worker_id = m.group(1)
                if worker_id in active_workers:
                    result.append(stripped)
                    active_workers.pop(worker_id, None)
                    continue

            if active_workers:
                for wid in list(active_workers):
                    if f"Worker {wid}" in stripped or f"Task " in stripped:
                        result.append(stripped)
                        break
                else:
                    if any(active_workers.values()):
                        result.append(stripped)

    return result


@router.get("/{instance_id}/clean")
async def get_clean_log(
    instance_id: str,
    tail: int = Query(default=100),
    user: dict = Depends(get_current_user),
):
    inst = _get_instance(instance_id)
    output_dir = _get_output_dir(inst["config_path"])
    log_file = os.path.join(output_dir, "nohup_clean.log")
    if not os.path.exists(log_file):
        return {"lines": [], "total_lines": 0}

    async with aiofiles.open(log_file, "r", errors="replace") as f:
        all_lines = await f.readlines()

    total = len(all_lines)
    lines = all_lines[-tail:] if tail < total else all_lines
    return {"lines": [l.rstrip("\n") for l in lines], "total_lines": total}


@router.get("/{instance_id}/tasks")
async def list_log_tasks(
    instance_id: str,
    user: dict = Depends(get_current_user),
):
    """解析 nohup.log 提取所有任务的 task_idx / env_id / config_name。"""
    inst = _get_instance(instance_id)
    output_dir = _get_output_dir(inst["config_path"])
    log_file = os.path.join(output_dir, "nohup.log")
    if not os.path.exists(log_file):
        return {"tasks": []}

    # 匹配: "Worker {w} starting task {idx}: {config_name}"
    start_pattern = re.compile(r"Worker (\d+) starting task (\d+): (.+?) =")
    # 匹配: "Task {idx}: env={env_id}"
    env_pattern = re.compile(r"Task (\d+): env=(\w+)")

    tasks = {}
    async with aiofiles.open(log_file, "r", errors="replace") as f:
        async for line in f:
            m = start_pattern.search(line)
            if m:
                idx = m.group(2)
                tasks.setdefault(idx, {"task_idx": idx, "config_name": m.group(3), "env_id": ""})
            m = env_pattern.search(line)
            if m:
                idx = m.group(1)
                tasks.setdefault(idx, {"task_idx": idx, "config_name": "", "env_id": ""})
                tasks[idx]["env_id"] = m.group(2)

    sorted_tasks = sorted(tasks.values(), key=lambda t: int(t["task_idx"]))
    return {"tasks": sorted_tasks}


@router.websocket("/ws/{instance_id}")
async def websocket_log_stream(websocket: WebSocket, instance_id: str):
    """WebSocket endpoint for real-time log streaming."""
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="缺少token")
        return

    from jose import JWTError, jwt as jose_jwt
    from ..core.config import settings as app_settings
    try:
        jose_jwt.decode(token, app_settings.SECRET_KEY, algorithms=[app_settings.ALGORITHM])
    except JWTError:
        await websocket.close(code=4001, reason="token无效")
        return

    await websocket.accept()

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()
    if not row:
        await websocket.send_json({"error": "实例不存在"})
        await websocket.close()
        return

    inst = dict(row)
    output_dir = _get_output_dir(inst["config_path"])
    log_file = os.path.join(output_dir, "nohup.log")

    try:
        while True:
            if not os.path.exists(log_file):
                await websocket.send_json({"type": "waiting", "message": "等待日志文件..."})
                await asyncio.sleep(2)
                continue

            async with aiofiles.open(log_file, "r", errors="replace") as f:
                await f.seek(0, 2)
                while True:
                    line = await f.readline()
                    if line:
                        await websocket.send_json({"type": "log", "data": line.rstrip("\n")})
                    else:
                        await asyncio.sleep(0.5)
                        try:
                            await asyncio.wait_for(websocket.receive_text(), timeout=0.01)
                        except asyncio.TimeoutError:
                            pass
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass


@router.get("/{instance_id}/obs-logs")
async def list_obs_logs(
    instance_id: str,
    user: dict = Depends(get_current_user),
):
    """List log files on OBS for a completed task instance."""
    inst = _get_instance(instance_id)

    config_path = inst["config_path"]
    if not os.path.exists(config_path):
        raise HTTPException(status_code=404, detail="配置文件不存在")

    from omegaconf import OmegaConf
    cfg = OmegaConf.load(config_path)
    traj_bucket = cfg.run_config.obs.traj_save_bucket
    traj_path = cfg.run_config.obs.traj_save_path
    obs_path = f"{traj_bucket}/{traj_path}/"

    proc = await asyncio.create_subprocess_exec(
        settings.OBSUTIL_PATH, "ls", obs_path, "-limit=500",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)

    items = []
    for line in stdout.decode().splitlines():
        if "obs://" in line:
            parts = line.split()
            for p in parts:
                if p.startswith("obs://"):
                    name = p.replace(obs_path, "").strip("/")
                    if name:
                        items.append({"name": name, "path": p, "is_dir": p.endswith("/")})
    return {"obs_path": obs_path, "items": items}


@router.get("/{instance_id}/obs-download")
async def download_obs_log(
    instance_id: str,
    file_path: str = Query(description="OBS上的文件路径"),
    user: dict = Depends(get_current_user),
):
    """Download a specific log file from OBS."""
    inst = _get_instance(instance_id)

    if not file_path.startswith("obs://"):
        raise HTTPException(status_code=400, detail="无效的OBS路径")

    tmp_dir = os.path.join(settings.HIVE_ROOT, "platform", "tmp", instance_id)
    os.makedirs(tmp_dir, exist_ok=True)
    filename = os.path.basename(file_path.rstrip("/"))
    local_path = os.path.join(tmp_dir, filename)

    proc = await asyncio.create_subprocess_exec(
        settings.OBSUTIL_PATH, "cp", file_path, local_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=120)

    if not os.path.exists(local_path):
        raise HTTPException(status_code=404, detail="文件下载失败")

    return FileResponse(local_path, filename=filename)


@router.get("/{instance_id}/obs-view")
async def view_obs_log(
    instance_id: str,
    file_path: str = Query(description="OBS上的文件路径"),
    tail: int = Query(default=500),
    user: dict = Depends(get_current_user),
):
    """View log content from OBS without downloading."""
    inst = _get_instance(instance_id)

    if not file_path.startswith("obs://"):
        raise HTTPException(status_code=400, detail="无效的OBS路径")

    tmp_dir = os.path.join(settings.HIVE_ROOT, "platform", "tmp", instance_id)
    os.makedirs(tmp_dir, exist_ok=True)
    filename = os.path.basename(file_path.rstrip("/"))
    local_path = os.path.join(tmp_dir, filename)

    proc = await asyncio.create_subprocess_exec(
        settings.OBSUTIL_PATH, "cp", file_path, local_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=120)

    if not os.path.exists(local_path):
        raise HTTPException(status_code=404, detail="文件下载失败")

    async with aiofiles.open(local_path, "r", errors="replace") as f:
        all_lines = await f.readlines()

    total = len(all_lines)
    lines = all_lines[-tail:] if tail < total else all_lines
    return {"lines": [l.rstrip("\n") for l in lines], "total_lines": total, "file": filename}
