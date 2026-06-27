import json
import os
import re
import shutil
import signal
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from omegaconf import OmegaConf

from ..core.config import settings
from ..core.database import get_connection
from ..core.security import get_current_user, require_operator
from ..models.instance import InstanceCreate, InstanceInfo, InstanceOverview

router = APIRouter(prefix="/api/instances", tags=["instances"])

ALLOWED_CONFIG_FILES = {"config.yaml", "openclaw.json", "user_proxy_model.json", "hermes_config.yaml"}


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
def create_instance(req: InstanceCreate, user: dict = Depends(require_operator)):
    timestamp = datetime.now().strftime("%y%m%d%H%M")
    short_id = uuid.uuid4().hex[:4]
    instance_id = f"{timestamp}-{req.task_name}-{short_id}"
    instance_dir = _get_instance_dir(instance_id)
    os.makedirs(instance_dir, exist_ok=True)

    # --- 1. 生成 config.yaml ---
    template_path = settings.CONFIG_TEMPLATE
    if not os.path.exists(template_path):
        raise HTTPException(status_code=500, detail=f"模板配置文件不存在: {template_path}")

    base = OmegaConf.load(template_path)
    base.remote_server.user_id = req.task_name
    base.remote_server.project_id = req.harness_type
    base.run_config.concurrent_num = req.concurrent_num
    base.run_config.start_index = req.start_index
    base.run_config.total_num = req.total_num

    if req.skill_dir:
        base.run_config.obs.skill_download_path = req.skill_dir
    if req.default_skills:
        base.run_config.obs.default_skills = [s.strip() for s in req.default_skills.split(",") if s.strip()]
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
        traj_prefix = "hermes_trajs" if req.harness_type == "hermes" else "openclaw_trajs"
        base.run_config.obs.traj_save_path = f"{traj_prefix}/traj_{req.task_name}"
    if req.image_name:
        base.env_make.image_name = req.image_name

    openclaw_path = os.path.join(instance_dir, "openclaw.json")
    hermes_config_path = os.path.join(instance_dir, "hermes_config.yaml")
    user_proxy_path = os.path.join(instance_dir, "user_proxy_model.json")

    if req.harness_type == "hermes":
        base.run_config.sandbox.openclaw_local_config_file = hermes_config_path
        base.run_config.sandbox.harness_local_config_file = hermes_config_path
    else:
        base.run_config.sandbox.openclaw_local_config_file = openclaw_path
        base.run_config.sandbox.harness_local_config_file = openclaw_path
    base.run_config.sandbox.user_proxy_model_local_file = user_proxy_path

    # 输出和下载目录都放在实例目录下
    base.run_config.task.task_output_path = os.path.join(instance_dir, "outputs")
    base.run_config.task.task_download_path = os.path.join(instance_dir, "downloads")

    config_path = os.path.join(instance_dir, "config.yaml")
    OmegaConf.save(base, config_path)

    # --- 2. 生成 harness 配置文件 ---
    if req.harness_type == "hermes":
        hermes_cfg = {
            "model": {
                "default": req.model_id or "claude-opus-4-6",
                "provider": "custom",
                "base_url": req.model_base_url or "http://127.0.0.1:4000/",
                "api_key": req.model_api_key or "sky-1234",
            },
            "custom_providers": [{
                "name": "local",
                "base_url": req.model_base_url or "http://127.0.0.1:4001",
                "api_key": req.model_api_key or "",
                "api_mode": req.model_api_type.replace("-", "_") if req.model_api_type else "anthropic_messages",
            }],
        }
        hermes_omega = OmegaConf.create(hermes_cfg)
        OmegaConf.save(hermes_omega, hermes_config_path)
    else:
        openclaw_template = os.path.join(settings.SETTINGS_DIR, "openclaw.json")
        with open(openclaw_template, "r", encoding="utf-8") as f:
            openclaw_cfg = json.load(f)

        if req.model_api_key:
            openclaw_cfg["models"]["providers"]["local"]["apiKey"] = req.model_api_key
        if req.model_base_url:
            openclaw_cfg["models"]["providers"]["local"]["baseUrl"] = req.model_base_url
        if req.model_api_type:
            openclaw_cfg["models"]["providers"]["local"]["api"] = req.model_api_type
            models_list = openclaw_cfg["models"]["providers"]["local"]["models"]
            if models_list:
                models_list[0]["api"] = req.model_api_type
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
        file_count = len([f for f in os.listdir(task_input) if os.path.isfile(os.path.join(task_input, f))])
        actual_start = min(req.start_index, file_count)
        available = file_count - actual_start
        total_tasks = min(req.total_num, available) if req.total_num > 0 else available

    with get_connection() as conn:
        conn.execute(
            """INSERT INTO task_instances
               (id, name, config_path, status, created_by, total_tasks, concurrent_num, config_snapshot, create_params, created_at)
               VALUES (?, ?, ?, 'created', ?, ?, ?, ?, ?, ?)""",
            (instance_id, req.name, config_path, user["username"], total_tasks,
             req.concurrent_num, OmegaConf.to_yaml(base), req.model_dump_json(),
             datetime.now().isoformat()),
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


@router.get("/{instance_id}/create-params")
def get_create_params(instance_id: str, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = conn.execute("SELECT create_params FROM task_instances WHERE id=?", (instance_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")
    if not row["create_params"]:
        raise HTTPException(status_code=404, detail="该实例无创建参数记录")
    return json.loads(row["create_params"])


# ============================================================================
# 启动 / 停止 / 重跑
# ============================================================================

@router.post("/{instance_id}/start")
def start_instance(instance_id: str, user: dict = Depends(require_operator)):
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
def stop_instance(instance_id: str, user: dict = Depends(require_operator)):
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
def retry_failed(instance_id: str, user: dict = Depends(require_operator)):
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

    failed_file = os.path.join(output_dir, "failed.jsonl")
    retry_file = os.path.join(output_dir, "retry.jsonl")
    if os.path.exists(failed_file):
        if os.path.exists(retry_file):
            os.remove(retry_file)
        os.rename(failed_file, retry_file)
    elif not os.path.exists(retry_file):
        raise HTTPException(status_code=400, detail="没有失败任务可重跑")

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

    error_breakdown = _analyze_task_status(inst["config_path"], total)
    time_est = _estimate_remaining_time(total, finished, inst.get("started_at"), inst["status"])

    elapsed_seconds = None
    if inst.get("started_at"):
        end = inst.get("stopped_at") or datetime.now().isoformat()
        try:
            elapsed_seconds = round((datetime.fromisoformat(end) - datetime.fromisoformat(inst["started_at"])).total_seconds(), 0)
        except (ValueError, TypeError):
            pass

    return InstanceOverview(
        total=total, completed=completed, failed=failed,
        running=running_pods, pending=pending,
        success_rate=round(rate, 1), error_breakdown=error_breakdown,
        elapsed_seconds=elapsed_seconds,
        **time_est,
    )




def _estimate_remaining_time(
    total: int, finished: int, started_at: str, status: str,
) -> dict:
    result = {"avg_task_seconds": None, "estimated_remaining_seconds": None, "estimated_finish_time": None}
    if status != "running" or total == 0 or not started_at:
        return result

    try:
        elapsed = (datetime.now() - datetime.fromisoformat(started_at)).total_seconds()
    except (ValueError, TypeError):
        return result

    if elapsed <= 0:
        return result

    done = max(finished, 1)
    avg = elapsed / done
    total_est = total / done * elapsed
    remaining = max(0, total_est - elapsed)
    finish_time = (datetime.now() + timedelta(seconds=remaining)).isoformat(timespec="seconds")

    return {
        "avg_task_seconds": round(avg, 1),
        "estimated_remaining_seconds": round(remaining, 0),
        "estimated_finish_time": finish_time,
    }


def _analyze_task_status(config_path: str, total_tasks: int) -> dict:
    output_dir = _get_output_dir(config_path)
    logs_dir = os.path.join(output_dir, "logs")

    succeeded = 0
    failed = 0
    abnormal = 0

    if os.path.isdir(logs_dir):
        task_files = [f for f in os.listdir(logs_dir) if f.startswith("task-") and f.endswith(".log")]
        for fname in task_files:
            fpath = os.path.join(logs_dir, fname)
            has_success = False
            has_failed = False
            try:
                with open(fpath, "r", errors="replace") as f:
                    for line in f:
                        if "[INFO] 任务完成" in line:
                            has_success = True
                        if "[INFO] 任务失败" in line:
                            has_failed = True
            except OSError:
                continue

            if has_failed:
                failed += 1
            elif has_success:
                succeeded += 1
            else:
                abnormal += 1

    executed = succeeded + failed + abnormal
    not_executed = max(0, total_tasks - executed)

    result = {}
    if succeeded:
        result["任务成功"] = succeeded
    if failed:
        result["任务失败"] = failed
    if abnormal:
        result["异常退出"] = abnormal
    if not_executed:
        result["未执行"] = not_executed
    return result


@router.delete("/{instance_id}")
def delete_instance(instance_id: str, user: dict = Depends(require_operator)):
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
