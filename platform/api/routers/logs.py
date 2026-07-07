import asyncio
import json
import os
import re
import time
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


_LOGGER_RE = re.compile(r'\|(?:INFO|WARNING|ERROR|DEBUG)\|[^|]*\|[^|]*\|(.+)', re.DOTALL)
_NODE_ID_RE = re.compile(r'^\[node_id:\d+\][^|]*\|')
_SSE_STDOUT_STDERR_RE = re.compile(r',\s*stdout:\s*\[(.*)\],\s*stderr:\s*\[(.*)\]$', re.DOTALL)
_SSE_NO_RESP_RE = re.compile(r'sse data:.*no response')
_SSE_CMD_RE = re.compile(r'sse data:\s*\[#(\d+)\]\[([^\]]*:\[.+?\])\]')


def _join_multiline(lines: list[str]) -> list[str]:
    """SSE stdout/stderr 跨多行，把非 [node_id:] 开头的行合并到上一行。"""
    joined = []
    for line in lines:
        if line.startswith('[node_id:') or not joined:
            joined.append(line)
        else:
            joined[-1] += '\n' + line
    return joined


def _extract_sse_payload(line: str) -> Optional[tuple[str, str, str]]:
    """从一条已 join 的日志行中提取 (cmd_key, stdout, stderr)，非 SSE 行返回 None。"""
    cleaned = line
    m = _LOGGER_RE.search(cleaned)
    if m:
        cleaned = m.group(1)
    cleaned = _NODE_ID_RE.sub('', cleaned).strip()

    if _SSE_NO_RESP_RE.search(cleaned):
        return None
    if 'sse data:' not in cleaned:
        return None

    m_cmd = _SSE_CMD_RE.search(cleaned)
    cmd_key = m_cmd.group(2) if m_cmd else ''

    m_data = _SSE_STDOUT_STDERR_RE.search(cleaned)
    if not m_data:
        return None
    stdout = m_data.group(1) or ''
    stderr = m_data.group(2) or ''
    return (cmd_key, stdout, stderr)


def _concat_sse_groups(lines: list[str]) -> list[str]:
    """把同一命令的连续 SSE stdout/stderr 拼接成完整文本，然后按行输出。"""
    result = []
    current_cmd = None
    stdout_buf = []
    stderr_buf = []

    def _flush():
        text = ''.join(stdout_buf)
        seen_out = set()
        for out_line in text.splitlines():
            stripped = out_line.strip()
            if stripped and stripped not in seen_out:
                seen_out.add(stripped)
                result.append(f'[STDOUT] {stripped}')
        err_text = ''.join(stderr_buf)
        seen_err = set()
        for err_line in err_text.splitlines():
            stripped = err_line.strip()
            if stripped and stripped != 'None' and stripped not in seen_err:
                seen_err.add(stripped)
                result.append(f'[STDERR] {stripped}')

    for line in lines:
        payload = _extract_sse_payload(line)
        if payload is not None:
            cmd_key, stdout, stderr = payload
            if cmd_key != current_cmd and current_cmd is not None:
                _flush()
                stdout_buf.clear()
                stderr_buf.clear()
            current_cmd = cmd_key
            if stdout and stdout != 'None':
                stdout_buf.append(stdout)
            if stderr and stderr != 'None':
                stderr_buf.append(stderr)
        else:
            if current_cmd is not None:
                _flush()
                stdout_buf.clear()
                stderr_buf.clear()
                current_cmd = None
            cleaned = line
            m = _LOGGER_RE.search(cleaned)
            if m:
                cleaned = m.group(1)
            cleaned = _NODE_ID_RE.sub('', cleaned).strip()
            if _SSE_NO_RESP_RE.search(cleaned):
                continue
            cleaned = cleaned.replace('\n', ' ').strip()
            if cleaned:
                result.append(cleaned)

    if current_cmd is not None:
        _flush()

    return result


@router.get("/{instance_id}/main")
async def get_main_log(
    instance_id: str,
    tail: int = Query(default=200, description="返回最后N行"),
    task_filter: str = Query(default="", description="按 env_id 或 config_name 过滤"),
    mode: str = Query(default="verbose", description="verbose 或 concise"),
    source: str = Query(default="main", description="main 优先读 logs/main.log，nohup 读 nohup.log"),
    user: dict = Depends(get_current_user),
):
    inst = _get_instance(instance_id)
    output_dir = _get_output_dir(inst["config_path"])

    structured_log = os.path.join(output_dir, "logs", "main.log")
    legacy_log = os.path.join(output_dir, "nohup.log")

    if source == "nohup" or task_filter:
        log_file = legacy_log
    elif os.path.exists(structured_log):
        log_file = structured_log
    else:
        log_file = legacy_log

    if not os.path.exists(log_file):
        return {"lines": [], "total_lines": 0}

    if task_filter:
        lines = await _extract_task_lines(log_file, task_filter)
    else:
        async with aiofiles.open(log_file, "r", errors="replace") as f:
            all_lines = await f.readlines()
        lines = [l.rstrip("\n") for l in all_lines]

    if mode == "concise":
        lines = _join_multiline(lines)
        lines = _concat_sse_groups(lines)

    total = len(lines)
    lines = lines[-tail:] if tail < total else lines
    return {"lines": lines, "total_lines": total}


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


@router.get("/{instance_id}/task-log-list")
async def list_task_logs(
    instance_id: str,
    user: dict = Depends(get_current_user),
):
    inst = _get_instance(instance_id)
    logs_dir = os.path.join(_get_output_dir(inst["config_path"]), "logs")
    if not os.path.isdir(logs_dir):
        return {"files": []}
    files = sorted(
        [f for f in os.listdir(logs_dir) if f.startswith("task-") and f.endswith(".log")],
        key=lambda f: int(m.group()) if (m := re.search(r'\d+', f)) else 0,
    )
    return {"files": files}


@router.get("/{instance_id}/task-log/{filename}")
async def get_task_log(
    instance_id: str,
    filename: str,
    tail: int = Query(default=200),
    mode: str = Query(default="verbose"),
    user: dict = Depends(get_current_user),
):
    if not re.match(r'^(task-\d+|main)\.log$', filename):
        raise HTTPException(status_code=400, detail="无效的日志文件名")
    inst = _get_instance(instance_id)
    log_file = os.path.join(_get_output_dir(inst["config_path"]), "logs", filename)
    if not os.path.exists(log_file):
        return {"lines": [], "total_lines": 0}

    async with aiofiles.open(log_file, "r", errors="replace") as f:
        all_lines = await f.readlines()
    lines = [l.rstrip("\n") for l in all_lines]

    if mode == "concise":
        lines = _join_multiline(lines)
        lines = _concat_sse_groups(lines)

    total = len(lines)
    lines = lines[-tail:] if tail < total else lines
    return {"lines": lines, "total_lines": total}


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


# 缓存: instance_id -> (timestamp, dirs)
_obs_tree_cache: dict[str, tuple[float, list[str]]] = {}
_OBS_TREE_TTL = 10  # 秒


@router.get("/{instance_id}/obs-tree")
async def list_obs_tree(
    instance_id: str,
    refresh: bool = Query(default=False),
    user: dict = Depends(get_current_user),
):
    """用 obsutil ls -d 列出一级目录，返回目录名列表。结果缓存10秒。"""
    now = time.time()
    if not refresh and instance_id in _obs_tree_cache:
        cached_time, cached_dirs = _obs_tree_cache[instance_id]
        if now - cached_time < _OBS_TREE_TTL:
            inst = _get_instance(instance_id)
            obs_path = _get_obs_base_path(inst)
            return {"obs_path": obs_path, "dirs": cached_dirs}

    inst = _get_instance(instance_id)
    obs_path = _get_obs_base_path(inst)

    dirs = await _list_obs_dirs(obs_path)

    _obs_tree_cache[instance_id] = (now, dirs)
    return {"obs_path": obs_path, "dirs": dirs}


# 缓存: (instance_id, subdir) -> (timestamp, items)
_obs_subtree_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_OBS_SUBTREE_TTL = 30  # 秒


@router.get("/{instance_id}/obs-subtree")
async def list_obs_subtree(
    instance_id: str,
    subdir: str = Query(description="一级子目录名"),
    refresh: bool = Query(default=False),
    user: dict = Depends(get_current_user),
):
    """用 obsutil ls 列出某个子目录下所有文件，返回扁平路径列表。结果缓存30秒。"""
    now = time.time()
    cache_key = (instance_id, subdir)
    if not refresh and cache_key in _obs_subtree_cache:
        cached_time, cached_items = _obs_subtree_cache[cache_key]
        if now - cached_time < _OBS_SUBTREE_TTL:
            return {"items": cached_items}

    inst = _get_instance(instance_id)
    obs_path = _get_obs_base_path(inst)
    subdir_path = f"{obs_path}{subdir}/"

    proc = await asyncio.create_subprocess_exec(
        settings.OBSUTIL_PATH, "ls", subdir_path, "-limit=5000",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)

    items = []
    for line in stdout.decode().splitlines():
        line = line.strip()
        if not line.startswith("obs://"):
            continue
        parts = line.split()
        full_path = parts[0]
        name = full_path.replace(subdir_path, "").strip("/")
        if not name:
            continue
        is_dir = full_path.endswith("/")
        if is_dir:
            name = name.rstrip("/")
        size = None
        size_match = re.search(r'(\d+(?:\.\d+)?(?:B|KB|MB|GB|TB))', line)
        if size_match:
            size = size_match.group(1)
        items.append({"name": name, "path": full_path, "is_dir": is_dir, "size": size})

    _obs_subtree_cache[cache_key] = (now, items)
    return {"items": items}


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


def _get_obs_base_path(inst: dict) -> str:
    config_path = inst["config_path"]
    if not os.path.exists(config_path):
        raise HTTPException(status_code=404, detail="配置文件不存在")
    from omegaconf import OmegaConf
    cfg = OmegaConf.load(config_path)
    traj_bucket = cfg.run_config.obs.traj_save_bucket
    traj_path = cfg.run_config.obs.traj_save_path
    return f"{traj_bucket}/{traj_path}/"


# 内存缓存
_eval_stats_cache: dict[str, dict[str, float]] = {}  # instance_id -> {task: score}
_task_completed_cache: dict[str, dict[str, bool]] = {}  # instance_id -> {task: True/False}


async def _list_obs_dirs(obs_base: str) -> list[str]:
    """用 obsutil ls -d 列出 OBS 目录下所有子目录名。"""
    proc = await asyncio.create_subprocess_exec(
        settings.OBSUTIL_PATH, "ls", obs_base, "-d", "-limit=2000",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    dirs = []
    for line in stdout.decode().splitlines():
        line = line.strip()
        if not line.startswith("obs://"):
            continue
        full_path = line.split()[0]
        if not full_path.endswith("/"):
            continue
        name = full_path.replace(obs_base, "").strip("/")
        if name and not name.startswith("."):
            dirs.append(name)
    return dirs


async def _download_eval_score(obs_base: str, task_dir: str, tmp_dir: str,
                               semaphore: asyncio.Semaphore) -> tuple[str, float | None]:
    """下载单个任务的 evaluator_use.log，取最后一行的 completion 分数。"""
    async with semaphore:
        eval_path = f"{obs_base}{task_dir}/logs/evaluator_use.log"
        local_file = os.path.join(tmp_dir, f"eval_{task_dir}.log")
        try:
            proc = await asyncio.create_subprocess_exec(
                settings.OBSUTIL_PATH, "cp", eval_path, local_file,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=60)

            if not os.path.exists(local_file):
                return (task_dir, None)

            last_score = None
            async with aiofiles.open(local_file, "r", errors="replace") as f:
                async for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    evaluation = record.get("evaluation", {})
                    completion = evaluation.get("completion")
                    if completion is not None:
                        try:
                            last_score = float(completion)
                        except (ValueError, TypeError):
                            pass
            return (task_dir, last_score)
        except Exception:
            return (task_dir, None)
        finally:
            try:
                os.remove(local_file)
            except OSError:
                pass


async def _check_run_completed(obs_base: str, task_dir: str, tmp_dir: str,
                               semaphore: asyncio.Semaphore) -> tuple[str, bool]:
    """下载 {task_dir}/workdir/run.log，检查是否包含 '所有任务执行完成!'。"""
    async with semaphore:
        run_path = f"{obs_base}{task_dir}/workdir/run.log"
        local_file = os.path.join(tmp_dir, f"run_{task_dir}.log")
        try:
            proc = await asyncio.create_subprocess_exec(
                settings.OBSUTIL_PATH, "cp", run_path, local_file,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=60)

            if not os.path.exists(local_file):
                return (task_dir, False)

            async with aiofiles.open(local_file, "r", errors="replace") as f:
                async for line in f:
                    if "所有任务执行完成!" in line:
                        return (task_dir, True)
            return (task_dir, False)
        except Exception:
            return (task_dir, False)
        finally:
            try:
                os.remove(local_file)
            except OSError:
                pass


@router.get("/{instance_id}/eval-stats")
async def get_eval_stats(
    instance_id: str,
    refresh: bool = Query(default=False, description="强制刷新全部缓存"),
    user: dict = Depends(get_current_user),
):
    """扫描每个任务目录的 evaluator_use.log 和 run.log，返回 per-task 分数和完成状态。"""
    inst = _get_instance(instance_id)
    obs_base = _get_obs_base_path(inst)

    if refresh:
        _eval_stats_cache.pop(instance_id, None)
        _task_completed_cache.pop(instance_id, None)
    cached_scores = _eval_stats_cache.get(instance_id, {})
    cached_completed = _task_completed_cache.get(instance_id, {})

    all_dirs = await _list_obs_dirs(obs_base)
    task_dirs = [d for d in all_dirs if d != "logs"]

    new_dirs_scores = [d for d in task_dirs if d not in cached_scores]
    new_dirs_completed = [d for d in task_dirs if d not in cached_completed]

    tmp_dir = os.path.join(settings.HIVE_ROOT, "platform", "tmp", instance_id)
    os.makedirs(tmp_dir, exist_ok=True)
    semaphore = asyncio.Semaphore(10)

    # 并发下载 evaluator_use.log 和 run.log
    all_tasks = []
    for d in new_dirs_scores:
        all_tasks.append(_download_eval_score(obs_base, d, tmp_dir, semaphore))
    for d in new_dirs_completed:
        all_tasks.append(_check_run_completed(obs_base, d, tmp_dir, semaphore))

    new_scores = {}
    new_completed = {}
    if all_tasks:
        results = await asyncio.gather(*all_tasks)
        score_count = len(new_dirs_scores)
        for task_dir, val in results[:score_count]:
            if val is not None:
                new_scores[task_dir] = val
        for task_dir, val in results[score_count:]:
            new_completed[task_dir] = val

    all_scores = {**cached_scores, **new_scores}
    _eval_stats_cache[instance_id] = all_scores

    all_completed = {**cached_completed, **new_completed}
    _task_completed_cache[instance_id] = all_completed

    return {
        "available": bool(all_scores) or bool(all_completed),
        "total_samples": inst.get("total_tasks", 0),
        "uploaded_trajs": len(task_dirs),
        "task_scores": all_scores,
        "task_completed": all_completed,
    }
