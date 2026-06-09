import json
import os
import re
import shutil
import signal
import subprocess
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from omegaconf import OmegaConf

from ..core.config import settings
from ..core.database import get_connection
from ..core.security import get_current_user
from ..models.instance import InstanceCreate, InstanceInfo, InstanceOverview

router = APIRouter(prefix="/api/instances", tags=["instances"])

ALLOWED_CONFIG_FILES = {"config.yaml", "openclaw.json", "user_proxy_model.json"}


def _get_instance_dir(instance_id: str) -> str:
    return os.path.join(settings.HIVE_ROOT, "platform", "instances", instance_id)


def _get_output_dir(config_path: str) -> str:
    """hive.py 会把 output_path 拼上 config basename，推导实际输出目录。"""
    config_basename = Path(config_path).stem
    instance_dir = str(Path(config_path).parent)
    return os.path.join(instance_dir, "outputs", config_basename)


def _count_lines(file_path: str) -> int:
    if not os.path.exists(file_path):
        return 0
    with open(file_path, "r") as f:
        return sum(1 for line in f if line.strip())


def _is_pid_running(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _sync_instance_status(instance: dict) -> dict:
    inst = dict(instance)
    output_dir = _get_output_dir(inst["config_path"])

    completed = _count_lines(os.path.join(output_dir, "complete.jsonl"))
    failed = _count_lines(os.path.join(output_dir, "failed.jsonl"))
    inst["completed_tasks"] = completed
    inst["failed_tasks"] = failed

    if inst["status"] == "running":
        pid_alive = _is_pid_running(inst.get("pid"))
        all_done = inst["total_tasks"] > 0 and (completed + failed) >= inst["total_tasks"]
        if not pid_alive or all_done:
            inst["status"] = "completed" if failed == 0 else "finished"
            with get_connection() as conn:
                conn.execute(
                    "UPDATE task_instances SET status=?, completed_tasks=?, failed_tasks=?, stopped_at=? WHERE id=?",
                    (inst["status"], completed, failed, datetime.now().isoformat(), inst["id"]),
                )
        else:
            with get_connection() as conn:
                conn.execute(
                    "UPDATE task_instances SET completed_tasks=?, failed_tasks=? WHERE id=?",
                    (completed, failed, inst["id"]),
                )
            )

    return inst


# ============================================================================
# CRUD
# ============================================================================

@router.get("", response_model=list[InstanceInfo])
def list_instances(user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM task_instances ORDER BY created_at DESC").fetchall()
    return [InstanceInfo(**_sync_instance_status(dict(r))) for r in rows]


@router.get("/{instance_id}", response_model=InstanceInfo)
def get_instance(instance_id: str, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")
    return InstanceInfo(**_sync_instance_status(dict(row)))


@router.post("", response_model=InstanceInfo)
def create_instance(req: InstanceCreate, user: dict = Depends(get_current_user)):
    instance_id = uuid.uuid4().hex[:12]
    instance_dir = _get_instance_dir(instance_id)
    os.makedirs(instance_dir, exist_ok=True)

    # --- 1. 生成 config.yaml ---
    template_path = settings.CONFIG_TEMPLATE
    if not os.path.exists(template_path):
        raise HTTPException(status_code=500, detail=f"模板配置文件不存在: {template_path}")

    base = OmegaConf.load(template_path)
    base.remote_server.user_id = req.task_name
    base.run_config.concurrent_num = req.concurrent_num
    base.run_config.start_index = req.start_index
    base.run_config.total_num = req.total_num

    if req.skill_dir:
        base.run_config.obs.skill_download_path = req.skill_dir
    if req.agent_dir:
        base.run_config.obs.agents_download_path = req.agent_dir
    if req.user_config_dir:
        configs_dir = os.path.join(instance_dir, "configs")
        os.makedirs(configs_dir, exist_ok=True)
        obs_src = f"{base.s3.bucket_name}/{req.user_config_dir}"
        if not obs_src.endswith("/"):
            obs_src += "/"
        ret = subprocess.run(
            [settings.OBSUTIL_PATH, "cp", obs_src, configs_dir, "-r", "-f"],
            capture_output=True, text=True, timeout=600,
        )
        if ret.returncode != 0:
            raise HTTPException(status_code=500, detail=f"OBS下载失败: {ret.stderr[:500]}")
        # obsutil 会创建子目录（如 configs/demo_test/），找到实际包含文件的目录
        actual_dir = configs_dir
        while True:
            entries = os.listdir(actual_dir)
            if len(entries) == 1 and os.path.isdir(os.path.join(actual_dir, entries[0])):
                actual_dir = os.path.join(actual_dir, entries[0])
            else:
                break
        base.run_config.task.task_input_path = actual_dir
        base.run_config.obs.user_config_download_path = ""
    if req.user_profile_dir:
        base.run_config.obs.user_profile_download_path = req.user_profile_dir
    if req.traj_save_path:
        base.run_config.obs.traj_save_path = req.traj_save_path
    else:
        base.run_config.obs.traj_save_path = f"openclaw_trajs/traj_{req.task_name}"
    if req.image_name:
        base.env_make.image_name = req.image_name

    openclaw_path = os.path.join(instance_dir, "openclaw.json")
    user_proxy_path = os.path.join(instance_dir, "user_proxy_model.json")
    base.run_config.sandbox.openclaw_local_config_file = openclaw_path
    base.run_config.sandbox.user_proxy_model_local_file = user_proxy_path

    # 输出和下载目录都放在实例目录下
    base.run_config.task.task_output_path = os.path.join(instance_dir, "outputs")
    base.run_config.task.task_download_path = os.path.join(instance_dir, "downloads")

    config_path = os.path.join(instance_dir, "config.yaml")
    OmegaConf.save(base, config_path)

    # --- 2. 生成 openclaw.json ---
    openclaw_template = os.path.join(settings.SETTINGS_DIR, "openclaw.json")
    with open(openclaw_template, "r", encoding="utf-8") as f:
        openclaw_cfg = json.load(f)

    if req.model_api_key:
        openclaw_cfg["models"]["providers"]["local"]["apiKey"] = req.model_api_key
    if req.model_base_url:
        openclaw_cfg["models"]["providers"]["local"]["baseUrl"] = req.model_base_url
    if req.model_api_type:
        openclaw_cfg["models"]["providers"]["local"]["api"] = req.model_api_type
    if req.model_id:
        models_list = openclaw_cfg["models"]["providers"]["local"]["models"]
        if models_list:
            models_list[0]["id"] = req.model_id
            models_list[0]["name"] = req.model_id
        openclaw_cfg["agents"]["defaults"]["model"]["primary"] = f"local/{req.model_id}"
        openclaw_cfg["agents"]["defaults"]["models"] = {f"local/{req.model_id}": {}}

    with open(openclaw_path, "w", encoding="utf-8") as f:
        json.dump(openclaw_cfg, f, indent=2, ensure_ascii=False)

    # --- 3. 生成 user_proxy_model.json ---
    user_proxy_template = os.path.join(settings.SETTINGS_DIR, "user_proxy_model.json")
    with open(user_proxy_template, "r", encoding="utf-8") as f:
        user_proxy_cfg = json.load(f)

    if req.user_proxy_model_name:
        user_proxy_cfg["model"] = req.user_proxy_model_name
    if req.user_proxy_api_key:
        user_proxy_cfg["api_key"] = req.user_proxy_api_key
    if req.user_proxy_base_url:
        user_proxy_cfg["base_url"] = req.user_proxy_base_url

    with open(user_proxy_path, "w", encoding="utf-8") as f:
        json.dump(user_proxy_cfg, f, indent=2, ensure_ascii=False)

    # --- 4. 统计任务数并入库 ---
    total_tasks = 0
    task_input = str(base.run_config.task.task_input_path)
    if os.path.isdir(task_input):
        total_tasks = len([f for f in os.listdir(task_input) if os.path.isfile(os.path.join(task_input, f))])

    with get_connection() as conn:
        conn.execute(
            """INSERT INTO task_instances
               (id, name, config_path, status, created_by, total_tasks, concurrent_num, config_snapshot)
               VALUES (?, ?, ?, 'created', ?, ?, ?, ?)""",
            (instance_id, req.name, config_path, user["username"], total_tasks,
             req.concurrent_num, OmegaConf.to_yaml(base)),
        )
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()

    return InstanceInfo(**dict(row))


# ============================================================================
# 配置查看
# ============================================================================

@router.get("/{instance_id}/configs")
def list_instance_configs(instance_id: str, user: dict = Depends(get_current_user)):
    instance_dir = _get_instance_dir(instance_id)
    if not os.path.isdir(instance_dir):
        raise HTTPException(status_code=404, detail="实例目录不存在")

    files = []
    for name in ALLOWED_CONFIG_FILES:
        fpath = os.path.join(instance_dir, name)
        if os.path.exists(fpath):
            files.append({
                "name": name,
                "size": os.path.getsize(fpath),
                "modified_at": datetime.fromtimestamp(os.path.getmtime(fpath)).isoformat(),
            })
    return {"instance_id": instance_id, "files": files}


@router.get("/{instance_id}/configs/{filename}")
def get_instance_config(instance_id: str, filename: str, user: dict = Depends(get_current_user)):
    if filename not in ALLOWED_CONFIG_FILES:
        raise HTTPException(status_code=400, detail=f"不允许访问的文件: {filename}")

    fpath = os.path.join(_get_instance_dir(instance_id), filename)
    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail="配置文件不存在")

    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()

    file_type = "yaml" if filename.endswith(".yaml") else "json"
    return {"filename": filename, "type": file_type, "content": content}


# ============================================================================
# 启动 / 停止 / 重跑
# ============================================================================

@router.post("/{instance_id}/start")
def start_instance(instance_id: str, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")

    inst = dict(row)
    if inst["status"] == "running" and _is_pid_running(inst.get("pid")):
        raise HTTPException(status_code=400, detail="实例正在运行中")

    config_path = inst["config_path"]
    output_dir = _get_output_dir(config_path)
    os.makedirs(output_dir, exist_ok=True)
    log_file = os.path.join(output_dir, "nohup.log")
    clean_log_file = os.path.join(output_dir, "nohup_clean.log")

    hive_py = os.path.join(settings.HIVE_ROOT, "hive.py")
    env = os.environ.copy()
    env["RLXF_CLEAN_LOG_PATH"] = clean_log_file

    with open(log_file, "a") as lf:
        proc = subprocess.Popen(
            ["python", hive_py, "--config", config_path],
            stdout=lf, stderr=lf,
            cwd=settings.HIVE_ROOT,
            env=env,
            start_new_session=True,
        )

    with get_connection() as conn:
        conn.execute(
            "UPDATE task_instances SET status='running', pid=?, started_at=? WHERE id=?",
            (proc.pid, datetime.now().isoformat(), instance_id),
        )

    return {"message": "实例已启动", "pid": proc.pid}


@router.post("/{instance_id}/stop")
def stop_instance(instance_id: str, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")

    inst = dict(row)
    pid = inst.get("pid")
    if pid and _is_pid_running(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass

    config_path = inst["config_path"]
    subprocess.Popen(
        ["python", os.path.join(settings.HIVE_ROOT, "run_clear.py"), "--config", config_path],
        cwd=settings.HIVE_ROOT,
    )

    with get_connection() as conn:
        conn.execute(
            "UPDATE task_instances SET status='stopped', stopped_at=? WHERE id=?",
            (datetime.now().isoformat(), instance_id),
        )

    return {"message": "实例已停止"}


@router.post("/{instance_id}/retry-failed")
def retry_failed(instance_id: str, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")

    inst = dict(row)
    if inst["status"] == "running" and _is_pid_running(inst.get("pid")):
        raise HTTPException(status_code=400, detail="实例正在运行中，请先停止")

    config_path = inst["config_path"]
    output_dir = _get_output_dir(config_path)
    os.makedirs(output_dir, exist_ok=True)
    log_file = os.path.join(output_dir, "nohup.log")
    clean_log_file = os.path.join(output_dir, "nohup_clean.log")

    hive_py = os.path.join(settings.HIVE_ROOT, "hive.py")
    env = os.environ.copy()
    env["RLXF_CLEAN_LOG_PATH"] = clean_log_file

    with open(log_file, "a") as lf:
        proc = subprocess.Popen(
            ["python", hive_py, "--config", config_path, "--failed"],
            stdout=lf, stderr=lf,
            cwd=settings.HIVE_ROOT,
            env=env,
            start_new_session=True,
        )

    with get_connection() as conn:
        conn.execute(
            "UPDATE task_instances SET status='running', pid=?, started_at=? WHERE id=?",
            (proc.pid, datetime.now().isoformat(), instance_id),
        )

    return {"message": "重跑失败任务已启动", "pid": proc.pid}


@router.get("/{instance_id}/overview", response_model=InstanceOverview)
def get_instance_overview(instance_id: str, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")

    inst = _sync_instance_status(dict(row))
    completed = inst["completed_tasks"]
    failed = inst["failed_tasks"]
    total = inst["total_tasks"]
    finished = completed + failed
    running_pods = 0

    if inst["status"] == "running":
        running_pods = min(inst["concurrent_num"], total - finished) if total > finished else 0

    pending = max(0, total - finished - running_pods)
    rate = (completed / finished * 100) if finished > 0 else 0.0

    error_breakdown = _analyze_errors(inst["config_path"])

    return InstanceOverview(
        total=total, completed=completed, failed=failed,
        running=running_pods, pending=pending,
        success_rate=round(rate, 1), error_breakdown=error_breakdown,
    )


def _analyze_errors(config_path: str) -> dict:
    output_dir = _get_output_dir(config_path)
    log_file = os.path.join(output_dir, "nohup.log")
    if not os.path.exists(log_file):
        return {}

    error_keywords = [
        ("Gateway startup timeout", "Gateway启动超时"),
        ("Gateway start failed", "Gateway启动失败"),
        ("Failed to update port", "端口更新失败"),
        ("Script execution failed", "脚本执行失败"),
        ("Skill download failed", "技能下载失败"),
        ("User profile download failed", "用户配置下载失败"),
        ("Agents download failed", "Agent下载失败"),
        ("Upload failed", "OBS上传失败"),
        ("extract code failed", "代码解压失败"),
    ]

    counts = Counter()
    error_pattern = re.compile(r"Task \d+ failed:.*?RuntimeError: (.+?)(?:\\n|$)")

    with open(log_file, "r", errors="replace") as f:
        content = f.read()

    for match in error_pattern.finditer(content):
        msg = match.group(1)
        classified = False
        for keyword, category in error_keywords:
            if keyword.lower() in msg.lower():
                counts[category] += 1
                classified = True
                break
        if not classified:
            counts["其他错误"] += 1

    return dict(counts)


@router.delete("/{instance_id}")
def delete_instance(instance_id: str, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")

    inst = dict(row)
    if inst["status"] == "running" and _is_pid_running(inst.get("pid")):
        raise HTTPException(status_code=400, detail="实例正在运行中，请先停止")

    instance_dir = _get_instance_dir(instance_id)
    if os.path.isdir(instance_dir):
        shutil.rmtree(instance_dir)

    with get_connection() as conn:
        conn.execute("DELETE FROM task_instances WHERE id=?", (instance_id,))

    return {"message": "实例已删除"}
