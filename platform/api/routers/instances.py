import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from omegaconf import OmegaConf

from ..core.config import settings
from ..core.database import get_connection, async_execute
from ..core.security import get_current_user, require_operator
from ..models.instance import InstanceCreate, InstanceInfo, InstanceOverview

router = APIRouter(prefix="/api/instances", tags=["instances"])

ALLOWED_CONFIG_FILES = {"config.yaml", "openclaw.json", "user_proxy_model.json", "hermes_config.yaml", "cc_settings.json"}

_status_cache = {}
_STATUS_CACHE_TTL = 5

_analyze_cache = {}
_ANALYZE_CACHE_TTL = 10


async def _request_api_key(base_url: str, invite_code: str, name: str) -> str:
    api_url = base_url.rstrip("/") + f"/api/invite?invite_code={invite_code}&name={name}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(api_url)
        resp.raise_for_status()
        data = resp.json()
        return data.get("api_key", "")


def _get_instance_dir(instance_id: str) -> str:
    return os.path.join(settings.HIVE_ROOT, "platform", "instances", instance_id)


def _get_output_dir(config_path: str) -> str:
    config_basename = Path(config_path).stem
    instance_dir = str(Path(config_path).parent)
    return os.path.join(instance_dir, "outputs", config_basename)


def _count_lines(file_path: str) -> int:
    if not os.path.exists(file_path):
        return 0
    count = 0
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            count += chunk.count(b"\n")
    return count


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

    if inst["status"] not in ("running", "preparing"):
        return inst

    if inst["status"] == "preparing":
        return inst

    output_dir = _get_output_dir(inst["config_path"])

    now = time.time()
    cache_key = inst["id"]
    cached = _status_cache.get(cache_key)
    if cached and (now - cached[1]) < _STATUS_CACHE_TTL:
        inst["completed_tasks"] = cached[0]["completed_tasks"]
        inst["failed_tasks"] = cached[0]["failed_tasks"]
        inst["status"] = cached[0]["status"]
        return inst

    completed = _count_lines(os.path.join(output_dir, "complete.jsonl"))
    failed = _count_lines(os.path.join(output_dir, "failed.jsonl"))
    inst["completed_tasks"] = completed
    inst["failed_tasks"] = failed

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

    _status_cache[cache_key] = ({"completed_tasks": completed, "failed_tasks": failed, "status": inst["status"]}, now)
    return inst


# ============================================================================
# CRUD
# ============================================================================

def _with_harness_type(d: dict) -> dict:
    if d.get("create_params"):
        try:
            d["harness_type"] = json.loads(d["create_params"]).get("harness_type", "openclaw")
        except Exception:
            pass
    return d


@router.get("", response_model=list[InstanceInfo])
def list_instances(user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM task_instances ORDER BY created_at DESC").fetchall()
    return [InstanceInfo(**_with_harness_type(_sync_instance_status(dict(r)))) for r in rows]


@router.get("/{instance_id}", response_model=InstanceInfo)
def get_instance(instance_id: str, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")
    return InstanceInfo(**_with_harness_type(_sync_instance_status(dict(row))))


async def _async_subprocess_run(cmd, *, cwd=None, timeout=600):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise HTTPException(status_code=500, detail=f"命令超时({timeout}s): {' '.join(cmd[:3])}")
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


@router.post("", response_model=InstanceInfo)
async def create_instance(req: InstanceCreate, user: dict = Depends(require_operator)):
    """创建实例：只生成配置文件并入库，不做 OBS 下载，秒级返回。"""
    timestamp = datetime.now().strftime("%y%m%d%H%M")
    short_id = uuid.uuid4().hex[:4]
    instance_id = f"{timestamp}-{req.task_name}-{short_id}"
    instance_dir = _get_instance_dir(instance_id)
    os.makedirs(instance_dir, exist_ok=True)

    # --- 1. 生成 config.yaml ---
    if req.harness_config_id:
        with get_connection() as conn:
            hc_row = conn.execute("SELECT harness_type, version FROM harness_configs WHERE id = ?", (req.harness_config_id,)).fetchone()
        if hc_row and hc_row["version"] != "默认":
            harness_settings_dir = os.path.join(settings.SETTINGS_DIR, hc_row["harness_type"], hc_row["version"])
            if not os.path.isdir(harness_settings_dir):
                harness_settings_dir = settings.SETTINGS_DIR
        else:
            harness_settings_dir = settings.SETTINGS_DIR
    else:
        harness_settings_dir = settings.SETTINGS_DIR

    config_template = os.path.join(harness_settings_dir, "config.yaml")
    if not os.path.exists(config_template):
        config_template = settings.CONFIG_TEMPLATE
    template_path = config_template
    if not os.path.exists(template_path):
        raise HTTPException(status_code=500, detail=f"模板配置文件不存在: {template_path}")

    base = OmegaConf.load(template_path)
    base.sandbox_id_prefix = req.task_name
    base.run_config.harness_type = req.harness_type
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
        base.run_config.obs.user_config_download_path = req.user_config_dir
    if req.user_profile_dir:
        base.run_config.obs.user_profile_download_path = req.user_profile_dir
    if req.traj_save_path:
        base.run_config.obs.traj_save_path = req.traj_save_path
    else:
        traj_prefixes = {"hermes": "hermes_trajs", "claude-code": "cc_trajs"}
        traj_prefix = traj_prefixes.get(req.harness_type, "openclaw_trajs")
        base.run_config.obs.traj_save_path = f"{traj_prefix}/traj_{req.task_name}"
    if req.image_name:
        base.sandbox.x86_cpu.sandbox.image = req.image_name.strip()

    openclaw_path = os.path.join(instance_dir, "openclaw.json")
    hermes_config_path = os.path.join(instance_dir, "hermes_config.yaml")
    cc_settings_path = os.path.join(instance_dir, "cc_settings.json")
    user_proxy_path = os.path.join(instance_dir, "user_proxy_model.json")

    if req.harness_type == "hermes":
        base.run_config.sandbox.harness_local_config_file = hermes_config_path
    elif req.harness_type == "claude-code":
        base.run_config.sandbox.harness_local_config_file = cc_settings_path
    else:
        base.run_config.sandbox.harness_local_config_file = openclaw_path
    base.run_config.sandbox.user_proxy_model_local_file = user_proxy_path

    base.run_config.task.task_output_path = os.path.join(instance_dir, "outputs")
    base.run_config.task.task_download_path = os.path.join(instance_dir, "downloads")

    # 代码仓：只记录到配置，不做下载
    if req.code_repo_id:
        with get_connection() as conn:
            repo_row = conn.execute("SELECT * FROM code_repos WHERE id = ?", (req.code_repo_id,)).fetchone()
        if repo_row:
            repo = dict(repo_row)
            code_tar_dir = os.path.join(settings.HIVE_ROOT, "platform", "code", "tar", repo["name"], repo["version"])
            tar_path = os.path.join(code_tar_dir, "openclaw-task.tar")
            base.run_config.task.main_code_tar = tar_path
            base.run_config.task.main_code_dir = ""
            if repo.get("main_python_file"):
                base.run_config.task.main_python_file = repo["main_python_file"]

    config_path = os.path.join(instance_dir, "config.yaml")
    OmegaConf.save(base, config_path)

    # --- 2. 生成 harness 配置文件 ---
    if req.harness_type == "hermes":
        hermes_template = os.path.join(harness_settings_dir, "hermes_config.yaml")
        if not os.path.exists(hermes_template):
            hermes_template = os.path.join(settings.SETTINGS_DIR, "hermes_config.yaml")
        hermes_omega = OmegaConf.load(hermes_template)

        if req.model_id:
            hermes_omega.model.default = req.model_id
            hermes_omega.model.model = req.model_id
        if req.model_base_url:
            hermes_omega.model.base_url = req.model_base_url
        if req.model_api_key:
            hermes_omega.model.api_key = req.model_api_key

        OmegaConf.save(hermes_omega, hermes_config_path)
    elif req.harness_type == "claude-code":
        cc_template = os.path.join(harness_settings_dir, "cc_settings.json")
        if not os.path.exists(cc_template):
            cc_template = os.path.join(settings.SETTINGS_DIR, "cc_settings.json")
        with open(cc_template, "r", encoding="utf-8") as f:
            cc_cfg = json.load(f)

        if req.model_base_url:
            cc_cfg["env"]["ANTHROPIC_BASE_URL"] = req.model_base_url
        if req.model_api_key:
            cc_cfg["env"]["ANTHROPIC_AUTH_TOKEN"] = req.model_api_key
        if req.model_id:
            cc_cfg["env"]["ANTHROPIC_MODEL"] = req.model_id

        with open(cc_settings_path, "w", encoding="utf-8") as f:
            json.dump(cc_cfg, f, indent=2, ensure_ascii=False)
    else:
        openclaw_template = os.path.join(harness_settings_dir, "openclaw.json")
        if not os.path.exists(openclaw_template):
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

    # --- 3. 自动申请 API Key (invite_code) ---
    if req.invite_code and not req.model_api_key and req.model_base_url:
        try:
            key_name = f"{user['username']}_{datetime.now().strftime('%y%m%d%H%M')}_{req.name}"
            req.model_api_key = await _request_api_key(req.model_base_url, req.invite_code, key_name)
            if req.harness_type == "hermes" and os.path.exists(hermes_config_path):
                hermes_omega = OmegaConf.load(hermes_config_path)
                hermes_omega.model.api_key = req.model_api_key
                OmegaConf.save(hermes_omega, hermes_config_path)
            elif req.harness_type == "claude-code" and os.path.exists(cc_settings_path):
                with open(cc_settings_path, "r", encoding="utf-8") as f:
                    cc_cfg = json.load(f)
                cc_cfg["env"]["ANTHROPIC_AUTH_TOKEN"] = req.model_api_key
                with open(cc_settings_path, "w", encoding="utf-8") as f:
                    json.dump(cc_cfg, f, indent=2, ensure_ascii=False)
            elif os.path.exists(openclaw_path):
                with open(openclaw_path, "r", encoding="utf-8") as f:
                    openclaw_cfg = json.load(f)
                openclaw_cfg["models"]["providers"]["local"]["apiKey"] = req.model_api_key
                with open(openclaw_path, "w", encoding="utf-8") as f:
                    json.dump(openclaw_cfg, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    # --- 4. 生成 user_proxy_model.json ---
    user_proxy_template = os.path.join(harness_settings_dir, "user_proxy_model.json")
    if not os.path.exists(user_proxy_template):
        user_proxy_template = os.path.join(settings.SETTINGS_DIR, "user_proxy_model.json")
    with open(user_proxy_template, "r", encoding="utf-8") as f:
        user_proxy_cfg = json.load(f)

    if req.agents:
        user_proxy_cfg = {}
        for ag in req.agents:
            entry = {}
            if ag.model:
                entry["model"] = ag.model
            if ag.provider:
                entry["provider"] = ag.provider
            if ag.base_url:
                entry["base_url"] = ag.base_url
            if ag.api_key:
                entry["api_key"] = ag.api_key
            if ag.api:
                entry["api"] = ag.api
            user_proxy_cfg[ag.name] = entry
    elif req.user_proxy_model_name or req.user_proxy_api_key or req.user_proxy_base_url:
        sim = user_proxy_cfg.get("user_simulator", {})
        if req.user_proxy_model_name:
            sim["model"] = req.user_proxy_model_name
        if req.user_proxy_api_key:
            sim["api_key"] = req.user_proxy_api_key
        if req.user_proxy_base_url:
            sim["base_url"] = req.user_proxy_base_url
        user_proxy_cfg["user_simulator"] = sim

    with open(user_proxy_path, "w", encoding="utf-8") as f:
        json.dump(user_proxy_cfg, f, indent=2, ensure_ascii=False)

    # --- 5. 入库（不统计任务数，留到启动时统计） ---
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO task_instances
               (id, name, config_path, status, created_by, total_tasks, concurrent_num, config_snapshot, create_params, created_at)
               VALUES (?, ?, ?, 'created', ?, ?, ?, ?, ?, ?)""",
            (instance_id, req.name, config_path, user["username"], 0,
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
# 启动准备：OBS 下载、打包、任务数统计
# ============================================================================

async def _prepare_instance(instance_id: str, inst: dict):
    """启动前的准备工作：下载用户配置、下载代码仓、打包、统计任务数。"""
    config_path = inst["config_path"]
    instance_dir = str(Path(config_path).parent)
    base = OmegaConf.load(config_path)

    create_params = json.loads(inst["create_params"]) if inst.get("create_params") else {}
    harness_type = create_params.get("harness_type", "openclaw")

    # --- 下载用户配置（全局缓存，相同路径不重复下载） ---
    user_config_dir = create_params.get("user_config_dir", "")
    if user_config_dir:
        # 使用全局共享目录缓存 OBS 下载，路径作为 key
        cache_key_path = user_config_dir.strip("/").replace("/", "__")
        global_configs_cache = os.path.join(settings.HIVE_ROOT, "platform", "downloads", "configs", cache_key_path)
        configs_dir = os.path.join(instance_dir, "configs")

        if os.path.isdir(global_configs_cache) and os.listdir(global_configs_cache):
            # 已有缓存，直接软链接到实例目录
            if os.path.exists(configs_dir):
                if os.path.islink(configs_dir):
                    os.unlink(configs_dir)
                else:
                    shutil.rmtree(configs_dir)
            os.symlink(global_configs_cache, configs_dir)
        else:
            # 首次下载到全局缓存目录
            os.makedirs(global_configs_cache, exist_ok=True)
            obs_src = f"{base.s3.bucket_name}/{user_config_dir}"
            if not obs_src.endswith("/"):
                obs_src += "/"
            returncode, _, stderr = await _async_subprocess_run(
                [settings.OBSUTIL_PATH, "cp", obs_src, global_configs_cache, "-r", "-f"],
                timeout=600,
            )
            if returncode != 0:
                shutil.rmtree(global_configs_cache, ignore_errors=True)
                raise HTTPException(status_code=500, detail=f"OBS下载用户配置失败: {stderr[:500]}")

            # 软链接到实例目录
            if os.path.exists(configs_dir):
                if os.path.islink(configs_dir):
                    os.unlink(configs_dir)
                else:
                    shutil.rmtree(configs_dir)
            os.symlink(global_configs_cache, configs_dir)

        # 找到实际目录（穿透单层嵌套）
        actual_dir = configs_dir
        while True:
            entries = os.listdir(actual_dir)
            if len(entries) == 1 and os.path.isdir(os.path.join(actual_dir, entries[0])):
                actual_dir = os.path.join(actual_dir, entries[0])
            else:
                break

        # 注入 harness_type 到每个 JSON（幂等操作）
        for _root, _dirs, _files in os.walk(actual_dir):
            for _fname in _files:
                if not _fname.endswith(".json"):
                    continue
                _fpath = os.path.join(_root, _fname)
                try:
                    with open(_fpath, "r", encoding="utf-8") as _f:
                        _cfg = json.load(_f)
                    if _cfg.get("harness_type") != harness_type:
                        _cfg["harness_type"] = harness_type
                        with open(_fpath, "w", encoding="utf-8") as _f:
                            json.dump(_cfg, _f, indent=2, ensure_ascii=False)
                except (json.JSONDecodeError, OSError):
                    pass

        # 更新配置中的 task_input_path
        base.run_config.task.task_input_path = actual_dir
        base.run_config.obs.user_config_download_path = ""
        OmegaConf.save(base, config_path)

    # --- 下载代码仓并打包 ---
    code_repo_id = create_params.get("code_repo_id")
    if code_repo_id:
        with get_connection() as conn:
            repo_row = conn.execute("SELECT * FROM code_repos WHERE id = ?", (code_repo_id,)).fetchone()
        if repo_row:
            repo = dict(repo_row)
            code_src_dir = os.path.join(settings.HIVE_ROOT, "platform", "code", "src", repo["name"], repo["version"])
            code_tar_dir = os.path.join(settings.HIVE_ROOT, "platform", "code", "tar", repo["name"], repo["version"])
            tar_path = os.path.join(code_tar_dir, "openclaw-task.tar")

            if not os.path.isfile(tar_path):
                if not (os.path.isdir(code_src_dir) and os.listdir(code_src_dir)):
                    os.makedirs(code_src_dir, exist_ok=True)
                    obs_src = repo["obs_path"]
                    if not obs_src.endswith("/"):
                        obs_src += "/"
                    returncode, _, stderr = await _async_subprocess_run(
                        [settings.OBSUTIL_PATH, "cp", obs_src, code_src_dir, "-r", "-f"],
                        timeout=600,
                    )
                    if returncode != 0:
                        raise HTTPException(status_code=500, detail=f"代码仓下载失败: {stderr[:500]}")

                obs_dir_name = repo["obs_path"].rstrip("/").split("/")[-1]
                os.makedirs(code_tar_dir, exist_ok=True)
                returncode, _, stderr = await _async_subprocess_run(
                    ["tar", "cf", tar_path, obs_dir_name],
                    cwd=code_src_dir,
                    timeout=120,
                )
                if returncode != 0:
                    raise HTTPException(status_code=500, detail=f"打包失败: {stderr[:500]}")

    # --- 统计任务数 ---
    base = OmegaConf.load(config_path)
    task_input = str(base.run_config.task.task_input_path)
    total_tasks = 0
    if os.path.isdir(task_input):
        file_count = len([f for f in os.listdir(task_input) if os.path.isfile(os.path.join(task_input, f))])
        start_index = int(base.run_config.start_index) if hasattr(base.run_config, 'start_index') else 0
        total_num = int(base.run_config.total_num) if hasattr(base.run_config, 'total_num') else 0
        actual_start = min(start_index, file_count)
        available = file_count - actual_start
        total_tasks = min(total_num, available) if total_num > 0 else available

    with get_connection() as conn:
        conn.execute(
            "UPDATE task_instances SET total_tasks=? WHERE id=?",
            (total_tasks, instance_id),
        )

    return total_tasks


# ============================================================================
# 启动 / 停止 / 重跑
# ============================================================================

@router.post("/{instance_id}/start")
async def start_instance(instance_id: str, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")

    inst = dict(row)
    if inst["status"] == "running" and _is_pid_running(inst.get("pid")):
        raise HTTPException(status_code=400, detail="实例正在运行中")
    if inst["status"] == "preparing":
        raise HTTPException(status_code=400, detail="实例正在准备中，请稍候")

    # 标记为 preparing 状态
    with get_connection() as conn:
        conn.execute("UPDATE task_instances SET status='preparing' WHERE id=?", (instance_id,))
    _status_cache.pop(instance_id, None)

    # 执行准备工作（OBS下载、打包、统计任务数）
    try:
        await _prepare_instance(instance_id, inst)
    except Exception as e:
        with get_connection() as conn:
            conn.execute(
                "UPDATE task_instances SET status='created', error_summary=? WHERE id=?",
                (str(e)[:500], instance_id),
            )
        raise

    # 启动 hive
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

    _status_cache.pop(instance_id, None)

    with get_connection() as conn:
        conn.execute(
            "UPDATE task_instances SET status='stopped', stopped_at=? WHERE id=?",
            (datetime.now().isoformat(), instance_id),
        )

    return {"message": "实例已停止"}


@router.post("/{instance_id}/retry-failed")
async def retry_failed(instance_id: str, user: dict = Depends(require_operator)):
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

    _status_cache.pop(instance_id, None)

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

    error_breakdown = _analyze_task_status(inst["config_path"], total, inst["status"])
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


def _analyze_task_status(config_path: str, total_tasks: int, instance_status: str = "running") -> dict:
    now = time.time()
    cache_key = config_path
    cached = _analyze_cache.get(cache_key)

    if instance_status not in ("running",):
        if cached:
            return cached[0]

    if cached and (now - cached[1]) < _ANALYZE_CACHE_TTL:
        return cached[0]

    output_dir = _get_output_dir(config_path)
    logs_dir = os.path.join(output_dir, "logs")
    status_re = re.compile(
        r"任务执行状态=(?P<status>任务成功|任务失败|任务异常)\s+error_code=(?P<code>\S+)"
    )

    succeeded = 0
    failed = 0
    abnormal_by_prefix = {"C": 0, "S": 0, "T": 0, "X": 0}
    unfinished = 0

    if os.path.isdir(logs_dir):
        task_files = [f for f in os.listdir(logs_dir) if f.startswith("task-") and f.endswith(".log")]
        for fname in task_files:
            fpath = os.path.join(logs_dir, fname)
            last_status = None
            last_code = None
            try:
                with open(fpath, "r", errors="replace") as f:
                    for line in f:
                        m = status_re.search(line)
                        if m:
                            last_status = m.group("status")
                            last_code = m.group("code")
            except OSError:
                continue

            if last_status == "任务成功":
                succeeded += 1
            elif last_status == "任务失败":
                failed += 1
            elif last_status == "任务异常":
                prefix = (last_code or "")[:1]
                if prefix in abnormal_by_prefix:
                    abnormal_by_prefix[prefix] += 1
                else:
                    abnormal_by_prefix["X"] += 1
            else:
                unfinished += 1

    abnormal = sum(abnormal_by_prefix.values())
    executed = succeeded + failed + abnormal
    not_executed = max(0, total_tasks - executed)

    result = {}
    if succeeded:
        result["任务成功"] = succeeded
    if failed:
        result["任务失败"] = failed
    if abnormal:
        result["任务异常"] = abnormal
        prefix_label = {
            "C": "└ C 客户端",
            "S": "└ S 服务端",
            "T": "└ T 任务侧",
            "X": "└ X 未分类",
        }
        for p in ("C", "S", "T", "X"):
            if abnormal_by_prefix[p]:
                result[prefix_label[p]] = abnormal_by_prefix[p]
    if not_executed:
        result["未执行"] = not_executed

    _analyze_cache[cache_key] = (result, now)
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

    _status_cache.pop(instance_id, None)
    _analyze_cache.pop(inst.get("config_path"), None)

    with get_connection() as conn:
        conn.execute("DELETE FROM task_instances WHERE id=?", (instance_id,))

    return {"message": "实例已删除"}
