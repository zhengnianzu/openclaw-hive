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
from fastapi import APIRouter, Depends, HTTPException, Query
from omegaconf import OmegaConf

from ..core.config import settings
from ..core.database import get_connection, async_execute, async_query, async_query_one
from ..core.security import get_current_user, require_operator
from ..models.instance import InstanceCreate, InstanceInfo, InstanceOverview

router = APIRouter(prefix="/api/instances", tags=["instances"])

# 独立的 key 申请路由（路径 /api/generate-api-key，不带 instances 前缀）
key_router = APIRouter(prefix="/api", tags=["api-key"])

ALLOWED_CONFIG_FILES = {"config.yaml", "openclaw.json", "user_proxy_model.json", "hermes_config.yaml", "cc_settings.json", "openjiuwen.json", "opencode.json", "config.toml", "models.json", "grok_config.toml"}

_status_cache = {}
_STATUS_CACHE_TTL = 5

_analyze_cache = {}
_ANALYZE_CACHE_TTL = 10

# 与 _analyze_cache 同一时刻写入：config_path -> error_tree(list)
# 携带异常的一级分类(C/S/T/X)汇总 + 具体错误码明细，供前端折叠展示
_analyze_tree_cache = {}

# 一级分类中文名
_PREFIX_LABEL = {"C": "客户端", "S": "服务端", "T": "任务侧", "X": "未分类"}
# 错误码 -> 描述（镜像 hive.py 的 ERROR_CATALOG；此处复制以免为一处展示拉起 hive 依赖）
_ERROR_CODE_DESC = {
    "C001": "extract code failed", "C002": "upload file failed",
    "C003": "environment creation failed", "C004": "agent config write failed",
    "S001": "gateway start failed", "S002": "gateway startup timeout",
    "S003": "gateway unexpected output", "S004": "sandbox port-update failed",
    "S005": "skill download failed", "S006": "user profile download failed",
    "S007": "agents download failed", "S008": "script execution failed",
    "S009": "upload traj to OBS failed",
    "T001": "Task_Failed", "T002": "达到最大轮次", "T003": "连续3次未收到回复",
    "T004": "AgentExecutionError", "T005": "AssertionError",
    "T006": "API call failed", "T007": "TimeoutError", "T008": "HarnessError",
    "T010": "Uncategorized Traceback", "X999": "unclassified exception",
}

# 增量扫描状态: config_path -> {fname: (mtime, size, status, code)}
# 只重读 mtime/size 变化过的 task 日志，避免每次全量遍历上万文件
_analyze_file_state = {}

# 本轮 _compute_analyze 中「实际重读过」（签名变化）的 fname 集合: config_path -> set(fname)
# 供 _sync_task_records 只 UPSERT 变化过的任务行，避免每 8s 全量重写整表。
# None（键不存在）表示该 config 还没被扫过 → 写库需走全量兜底。
_analyze_changed = {}

# 后台预热开关：为 True 时请求线程只读缓存（哪怕已过期），扫描交给后台任务，
# 避免同步端点在请求线程里做重扫把 anyio 线程池打满
_bg_refresh_active = False


def _load_key_gen_config() -> dict:
    """加载 key 申请配置 settings/key_gen.yaml，文件不存在或解析失败时返回空配置。"""
    cfg_path = os.path.join(settings.SETTINGS_DIR, "key_gen.yaml")
    if not os.path.exists(cfg_path):
        return {}
    try:
        return OmegaConf.to_container(OmegaConf.load(cfg_path), resolve=True) or {}
    except Exception:
        return {}


def _resolve_key_gen_rule(base_url: str, cfg: dict) -> dict:
    """
    根据传入 base_url 解析出实际使用的申请规则。
    返回 {base_url, method, endpoint, params, response_key_field}。

    规则匹配：
    - 传入 base_url 命中 endpoints 里某条 match 前缀 -> 用该条的 method/endpoint/params，base_url 用传入的
    - 传入 base_url 但未命中任何 match          -> 用传入 base_url + default 的 method/endpoint/params
    - 未传 base_url                             -> 用 default.base_url（若为 "*" 则视为未指定，由上层报错）
    """
    default = cfg.get("default") or {}
    endpoints = cfg.get("endpoints") or []
    incoming = (base_url or "").strip()

    def _method(rule):
        return str(rule.get("method") or default.get("method") or "GET").upper()

    if incoming:
        for rule in endpoints:
            match = str(rule.get("match", "")).strip()
            if match and match in incoming:
                return {
                    "base_url": incoming,
                    "method": _method(rule),
                    "endpoint": rule.get("endpoint") or default.get("endpoint") or "/api/invite",
                    "params": rule.get("params") or {},
                    "response_key_field": rule.get("response_key_field")
                        or default.get("response_key_field") or "api_key",
                }
        # 未命中：用传入 base_url + default 的其余设置（default.base_url 为 "*" 即沿用传入）
        return {
            "base_url": incoming,
            "method": str(default.get("method") or "GET").upper(),
            "endpoint": default.get("endpoint") or "/api/invite",
            "params": default.get("params") or {},
            "response_key_field": default.get("response_key_field") or "api_key",
        }

    # 未传 base_url：走 default；若 default.base_url 是通配 "*" 则无从确定，返回空由上层报错
    default_base = str(default.get("base_url", "")).strip()
    if default_base == "*":
        default_base = ""
    return {
        "base_url": default_base,
        "method": str(default.get("method") or "GET").upper(),
        "endpoint": default.get("endpoint") or "/api/invite",
        "params": default.get("params") or {},
        "response_key_field": default.get("response_key_field") or "api_key",
    }


async def _request_api_key(base_url: str, invite_code: str, name: str) -> str:
    cfg = _load_key_gen_config()
    rule = _resolve_key_gen_rule(base_url, cfg)

    effective_base = rule["base_url"]
    if not effective_base:
        raise ValueError("未配置 key 申请的 base_url（key_gen.yaml.default.base_url 或传入 model_base_url）")

    # 去掉尾部 /v1
    effective_base = effective_base.rstrip("/")
    if effective_base.endswith("/v1"):
        effective_base = effective_base[:-3]

    api_url = effective_base + "/" + str(rule["endpoint"]).lstrip("/")
    method = rule["method"]

    # invite_code + name + 规则里的额外固定参数（如 token）
    payload = {"invite_code": invite_code, "name": name}
    extra = rule["params"]
    if isinstance(extra, dict):
        for k, v in extra.items():
            if v is not None and str(v) != "":
                payload[str(k)] = v

    key_field = str(rule["response_key_field"]) or "api_key"
    timeout = cfg.get("timeout", 15)

    async with httpx.AsyncClient(timeout=timeout) as client:
        if method == "POST":
            # 部分服务要求带 X-Requested-With 才返回 JSON
            resp = await client.post(
                api_url, json=payload,
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        else:
            resp = await client.get(api_url, params=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get(key_field, "")


@key_router.post("/generate-api-key")
async def generate_api_key(
    invite_code: str,
    name: str,
    base_url: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """
    独立的 API Key 申请接口（供前端"新建KEY"按钮调用）。
    复用 _request_api_key：按 key_gen.yaml 依据 base_url 匹配对应服务，
    命中的服务会自动附带其配置的固定参数（如 8082 的 token）。
    """
    if not invite_code:
        raise HTTPException(status_code=400, detail="invite_code 不能为空")
    try:
        api_key = await _request_api_key(base_url or "", invite_code, name)
    except httpx.HTTPStatusError as e:
        # 把上游服务的真实报错透出来（如 "Token not provided"）
        detail = ""
        try:
            detail = e.response.json().get("message") or e.response.text
        except Exception:
            detail = e.response.text
        raise HTTPException(status_code=502, detail=f"申请 key 失败：{detail}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"申请 key 失败：{e}")

    if not api_key:
        raise HTTPException(status_code=502, detail="上游服务未返回 api_key")
    return {"api_key": api_key}


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


def _sync_instance_status(instance: dict, persist: bool = False) -> dict:
    """同步 running 实例的 completed/failed 计数与状态。

    persist=False（默认，所有 GET 请求路径）：只算内存计数 + 读写内存缓存，**绝不写库**，
    避免读请求在 GET 路径里争用 SQLite 写锁（这是 "database is locked" 的根因）。
    persist=True（仅后台刷新任务 _refresh_running_once）：把最新计数/状态 UPDATE 落库。
    """
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

    # 后台预热启用时，请求线程只读缓存（哪怕已过期），把扫描/写库都留给后台任务，
    # 从根本上避免读请求触发文件扫描与写锁争用
    if not persist and _bg_refresh_active and cached:
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

    # 只有后台刷新任务才写库；请求线程仅更新内存缓存，避免写锁争用
    if persist:
        if not pid_alive or all_done:
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
        traj_prefixes = {"hermes": "hermes_trajs", "claude-code": "cc_trajs", "openjiuwen": "openjiuwen_trajs", "opencode": "opencode_trajs", "codex": "codex_trajs", "pi": "pi_trajs", "grok": "grok_trajs"}
        traj_prefix = traj_prefixes.get(req.harness_type, "openclaw_trajs")
        base.run_config.obs.traj_save_path = f"{traj_prefix}/traj_{req.task_name}"
    if req.image_name:
        base.sandbox.x86_cpu.sandbox.image = req.image_name.strip()

    openclaw_path = os.path.join(instance_dir, "openclaw.json")
    hermes_config_path = os.path.join(instance_dir, "hermes_config.yaml")
    cc_settings_path = os.path.join(instance_dir, "cc_settings.json")
    openjiuwen_path = os.path.join(instance_dir, "openjiuwen.json")
    opencode_path = os.path.join(instance_dir, "opencode.json")
    codex_path = os.path.join(instance_dir, "config.toml")
    pi_path = os.path.join(instance_dir, "models.json")
    grok_path = os.path.join(instance_dir, "grok_config.toml")
    user_proxy_path = os.path.join(instance_dir, "user_proxy_model.json")

    if req.harness_type == "hermes":
        base.run_config.sandbox.harness_local_config_file = hermes_config_path
    elif req.harness_type == "claude-code":
        base.run_config.sandbox.harness_local_config_file = cc_settings_path
    elif req.harness_type == "openjiuwen":
        base.run_config.sandbox.harness_local_config_file = openjiuwen_path
    elif req.harness_type == "opencode":
        base.run_config.sandbox.harness_local_config_file = opencode_path
    elif req.harness_type == "codex":
        base.run_config.sandbox.harness_local_config_file = codex_path
    elif req.harness_type == "pi":
        base.run_config.sandbox.harness_local_config_file = pi_path
    elif req.harness_type == "grok":
        base.run_config.sandbox.harness_local_config_file = grok_path
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
    elif req.harness_type == "openjiuwen":
        openjiuwen_template = os.path.join(harness_settings_dir, "openjiuwen.json")
        if not os.path.exists(openjiuwen_template):
            openjiuwen_template = os.path.join(settings.SETTINGS_DIR, "openjiuwen.json")
        with open(openjiuwen_template, "r", encoding="utf-8") as f:
            openjiuwen_cfg = json.load(f)

        openjiuwen_cfg.setdefault("default", {})
        if req.model_id:
            openjiuwen_cfg["default"]["model"] = req.model_id
        if req.model_base_url:
            openjiuwen_cfg["default"]["base_url"] = req.model_base_url
        if req.model_api_key:
            openjiuwen_cfg["default"]["api_key"] = req.model_api_key
        # provider 复用 model_api_type 传入；tokenfly 网关走 anthropic-messages，默认 Anthropic，不能回退 OpenAI
        openjiuwen_cfg["default"]["provider"] = req.model_api_type or openjiuwen_cfg["default"].get("provider") or "Anthropic"

        with open(openjiuwen_path, "w", encoding="utf-8") as f:
            json.dump(openjiuwen_cfg, f, indent=2, ensure_ascii=False)
    elif req.harness_type == "opencode":
        opencode_template = os.path.join(harness_settings_dir, "opencode.json")
        if not os.path.exists(opencode_template):
            opencode_template = os.path.join(settings.SETTINGS_DIR, "opencode.json")
        with open(opencode_template, "r", encoding="utf-8") as f:
            opencode_cfg = json.load(f)

        # opencode.json 结构: provider.<name>.{npm,options.{baseURL,apiKey},models} + agent.main.model
        # provider key 固定为 "local-provider"（与模板一致），npm 根据 API 类型选择:
        #   anthropic-messages -> @ai-sdk/anthropic
        #   openai-completions -> @ai-sdk/openai-compatible
        providers = opencode_cfg.setdefault("provider", {})
        main_provider_key = "local-provider"
        # 根据 model_api_type 决定 npm 包名；缺省时保留模板中的值
        if req.model_api_type == "anthropic-messages":
            npm_pkg = "@ai-sdk/anthropic"
        elif req.model_api_type == "openai-completions":
            npm_pkg = "@ai-sdk/openai-compatible"
        else:
            npm_pkg = None  # 保留模板原有值

        if main_provider_key not in providers:
            providers[main_provider_key] = {
                "name": main_provider_key,
                "npm": npm_pkg or "@ai-sdk/anthropic",
                "options": {"baseURL": "", "apiKey": ""},
                "models": {},
            }
        elif npm_pkg:
            providers[main_provider_key]["npm"] = npm_pkg

        main_opts = providers[main_provider_key].setdefault("options", {})
        if req.model_base_url:
            # opencode 要求 baseURL 带 /v1 后缀
            base_url = req.model_base_url.rstrip("/")
            if not base_url.endswith("/v1"):
                base_url += "/v1"
            main_opts["baseURL"] = base_url
        if req.model_api_key:
            main_opts["apiKey"] = req.model_api_key

        # model_id 注入: 写入 provider.models 并更新 agent.main.model
        if req.model_id:
            models_map = providers[main_provider_key].setdefault("models", {})
            models_map[req.model_id] = {"name": req.model_id}
            agents_section = opencode_cfg.setdefault("agent", {})
            main_agent = agents_section.setdefault("main", {})
            main_agent["model"] = f"{main_provider_key}/{req.model_id}"

        with open(opencode_path, "w", encoding="utf-8") as f:
            json.dump(opencode_cfg, f, indent=2, ensure_ascii=False)
    elif req.harness_type == "codex":
        # config.toml 结构:
        #   顶层: model / model_provider / model_reasoning_effort / disable_response_storage / sandbox_mode
        #   [model_providers.<name>]: name / base_url / experimental_bearer_token / wire_api
        import tomllib
        import tomli_w
        codex_template = os.path.join(harness_settings_dir, "config.toml")
        if not os.path.exists(codex_template):
            codex_template = os.path.join(settings.SETTINGS_DIR, "config.toml")
        with open(codex_template, "rb") as f:
            codex_cfg = tomllib.load(f)

        # 找到顶层 model_provider 指向的 provider 段(默认 "custom");不存在则自建
        main_provider_key = codex_cfg.get("model_provider") or "custom"
        providers = codex_cfg.setdefault("model_providers", {})
        prov = providers.setdefault(main_provider_key, {
            "name": main_provider_key,
            "base_url": "",
            "experimental_bearer_token": "",
            "wire_api": "responses",
        })

        if req.model_id:
            codex_cfg["model"] = req.model_id
        if req.model_base_url:
            # codex 要求 base_url 带 /v1 后缀
            base_url = req.model_base_url.rstrip("/")
            if not base_url.endswith("/v1"):
                base_url += "/v1"
            prov["base_url"] = base_url
        if req.model_api_key:
            prov["experimental_bearer_token"] = req.model_api_key

        with open(codex_path, "wb") as f:
            tomli_w.dump(codex_cfg, f)
    elif req.harness_type == "pi":
        # models.json 结构: providers.<name>.{baseUrl, api, apiKey, models: [{id, name, ...}]}
        pi_template = os.path.join(harness_settings_dir, "models.json")
        if not os.path.exists(pi_template):
            pi_template = os.path.join(settings.SETTINGS_DIR, "models.json")
        with open(pi_template, "r", encoding="utf-8") as f:
            pi_cfg = json.load(f)

        providers = pi_cfg.setdefault("providers", {})
        main_provider_key = "custom"
        if main_provider_key not in providers:
            providers[main_provider_key] = {
                "baseUrl": "",
                "api": "openai-completions",
                "apiKey": "",
                "models": [],
            }

        prov = providers[main_provider_key]
        if req.model_api_key:
            prov["apiKey"] = req.model_api_key
        if req.model_base_url:
            # pi 要求 baseUrl 带 /v1 后缀
            base_url = req.model_base_url.rstrip("/")
            if not base_url.endswith("/v1"):
                base_url += "/v1"
            prov["baseUrl"] = base_url
        if req.model_api_type:
            prov["api"] = req.model_api_type
        if req.model_id:
            models_list = prov.setdefault("models", [])
            if models_list:
                models_list[0]["id"] = req.model_id
                models_list[0]["name"] = req.model_id
            else:
                models_list.append({"id": req.model_id, "name": req.model_id})

        with open(pi_path, "w", encoding="utf-8") as f:
            json.dump(pi_cfg, f, indent=2, ensure_ascii=False)
    elif req.harness_type == "grok":
        import tomllib
        import tomli_w
        grok_template = os.path.join(harness_settings_dir, "grok_config.toml")
        if not os.path.exists(grok_template):
            grok_template = os.path.join(settings.SETTINGS_DIR, "grok_config.toml")
        with open(grok_template, "rb") as f:
            grok_cfg = tomllib.load(f)

        # 新版 grok_config.toml 将模型配置放在 [model.default-model] 下
        model_section = grok_cfg.setdefault("model", {}).setdefault("default-model", {})
        if req.model_id:
            model_section["model"] = req.model_id
            model_section["name"] = req.model_id
        if req.model_base_url:
            # grok 要求 base_url 带 /v1 后缀
            base_url = req.model_base_url.rstrip("/")
            if not base_url.endswith("/v1"):
                base_url += "/v1"
            model_section["base_url"] = base_url
        if req.model_api_key:
            model_section["api_key"] = req.model_api_key

        with open(grok_path, "wb") as f:
            tomli_w.dump(grok_cfg, f)
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
    # base_url 可来自 req.model_base_url，或回退到 key_gen.yaml 的默认配置
    if req.invite_code and not req.model_api_key:
        try:
            key_name = f"{user['username']}_{datetime.now().strftime('%y%m%d%H%M')}_{req.name}"
            req.model_api_key = await _request_api_key(req.model_base_url or "", req.invite_code, key_name)
            if not req.model_api_key:
                raise ValueError("申请接口未返回 api_key，请检查 key_gen.yaml 配置与响应字段")
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
            elif req.harness_type == "openjiuwen" and os.path.exists(openjiuwen_path):
                with open(openjiuwen_path, "r", encoding="utf-8") as f:
                    openjiuwen_cfg = json.load(f)
                openjiuwen_cfg.setdefault("default", {})["api_key"] = req.model_api_key
                with open(openjiuwen_path, "w", encoding="utf-8") as f:
                    json.dump(openjiuwen_cfg, f, indent=2, ensure_ascii=False)
            elif req.harness_type == "opencode" and os.path.exists(opencode_path):
                with open(opencode_path, "r", encoding="utf-8") as f:
                    opencode_cfg = json.load(f)
                opencode_cfg.setdefault("provider", {}).setdefault("local-provider", {}).setdefault("options", {})["apiKey"] = req.model_api_key
                with open(opencode_path, "w", encoding="utf-8") as f:
                    json.dump(opencode_cfg, f, indent=2, ensure_ascii=False)
            elif req.harness_type == "codex" and os.path.exists(codex_path):
                # codex config.toml: 用 tomllib(读) + tomli_w(写) 正式解析。
                # 与前面 codex 分支保持一致,避免正则替换在多段/嵌套 table 上的边界问题。
                import tomllib
                import tomli_w
                with open(codex_path, "rb") as f:
                    codex_cfg = tomllib.load(f)
                main_provider_key = codex_cfg.get("model_provider") or "custom"
                prov = codex_cfg.setdefault("model_providers", {}).setdefault(
                    main_provider_key,
                    {"name": main_provider_key, "base_url": "",
                     "experimental_bearer_token": "", "wire_api": "responses"},
                )
                prov["experimental_bearer_token"] = req.model_api_key
                with open(codex_path, "wb") as f:
                    tomli_w.dump(codex_cfg, f)
            elif req.harness_type == "pi" and os.path.exists(pi_path):
                with open(pi_path, "r", encoding="utf-8") as f:
                    pi_cfg = json.load(f)
                pi_cfg.setdefault("providers", {}).setdefault("custom", {})["apiKey"] = req.model_api_key
                with open(pi_path, "w", encoding="utf-8") as f:
                    json.dump(pi_cfg, f, indent=2, ensure_ascii=False)
            elif req.harness_type == "grok" and os.path.exists(grok_path):
                import tomllib
                import tomli_w
                with open(grok_path, "rb") as f:
                    grok_cfg = tomllib.load(f)
                grok_cfg.setdefault("model", {}).setdefault("default-model", {})["api_key"] = req.model_api_key
                with open(grok_path, "wb") as f:
                    tomli_w.dump(grok_cfg, f)
            elif os.path.exists(openclaw_path):
                with open(openclaw_path, "r", encoding="utf-8") as f:
                    openclaw_cfg = json.load(f)
                openclaw_cfg["models"]["providers"]["local"]["apiKey"] = req.model_api_key
                with open(openclaw_path, "w", encoding="utf-8") as f:
                    json.dump(openclaw_cfg, f, indent=2, ensure_ascii=False)
        except Exception as e:
            # 申请失败不阻断实例创建，但记录原因，避免像以前那样被静默吞掉
            print(f"[api_key] 自动申请失败 (instance name={req.name}): {e}")

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
        row = conn.execute(
            "SELECT create_params, config_snapshot FROM task_instances WHERE id=?",
            (instance_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")
    if not row["create_params"]:
        raise HTTPException(status_code=404, detail="该实例无创建参数记录")
    params = json.loads(row["create_params"])
    # OBS 桶名不在 create_params 里，从 config_snapshot(s3.bucket_name) 取出，
    # 供前端把各 OBS 目录字段拼成完整 obs:// 路径展示。
    if row["config_snapshot"]:
        try:
            snap = OmegaConf.create(row["config_snapshot"])
            params["obs_bucket"] = OmegaConf.select(snap, "s3.bucket_name")
        except Exception:
            pass
    return params


# ============================================================================
# 启动准备：OBS 下载、打包、任务数统计
# ============================================================================

def _inject_harness_type(actual_dir: str, harness_type: str):
    """遍历目录下所有 JSON，注入 harness_type（幂等）。同步阻塞操作，应在线程中调用。"""
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

        # 注入 harness_type 到每个 JSON（幂等操作）；
        # 大目录（可能上千个文件、数百 MB）的同步遍历放到线程里，避免阻塞事件循环
        await asyncio.to_thread(_inject_harness_type, actual_dir, harness_type)

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

    # 准备（OBS下载、打包、统计任务数）+ 启动，全部放到后台执行，
    # 立即返回，避免大目录下载/处理长时间占用 worker 阻塞其他请求。
    asyncio.create_task(_prepare_and_launch(instance_id, inst))

    return {"message": "实例准备中，正在后台下载配置并启动", "status": "preparing"}


async def _prepare_and_launch(instance_id: str, inst: dict):
    """后台执行准备与启动。失败时回写 created 状态和错误信息。"""
    try:
        await _prepare_instance(instance_id, inst)
    except Exception as e:
        with get_connection() as conn:
            conn.execute(
                "UPDATE task_instances SET status='created', error_summary=? WHERE id=?",
                (str(e)[:500], instance_id),
            )
        _status_cache.pop(instance_id, None)
        print(f"[start] 实例 {instance_id} 准备失败: {e}")
        return

    # 启动 hive
    try:
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
        _status_cache.pop(instance_id, None)
    except Exception as e:
        with get_connection() as conn:
            conn.execute(
                "UPDATE task_instances SET status='created', error_summary=? WHERE id=?",
                (str(e)[:500], instance_id),
            )
        _status_cache.pop(instance_id, None)
        print(f"[start] 实例 {instance_id} 启动失败: {e}")


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
    # error_tree 与 error_breakdown 在 _compute_analyze 中同刻写入，缓存生命周期一致
    error_tree = _analyze_tree_cache.get(inst["config_path"], [])
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
        error_tree=error_tree,
        elapsed_seconds=elapsed_seconds,
        **time_est,
    )


def _count_config_files(root: str) -> int:
    """数已下载的 config 文件数：穿透单层嵌套目录后，统计其中的普通文件。
    下载中文件实时落盘，据此可给出「已下载 N 个」的准备进度。"""
    if not root or not os.path.isdir(root):
        return 0
    actual = root
    # 穿透单层嵌套（与 _prepare_instance 的 actual_dir 逻辑一致）
    for _ in range(5):
        try:
            entries = os.listdir(actual)
        except OSError:
            return 0
        if len(entries) == 1 and os.path.isdir(os.path.join(actual, entries[0])):
            actual = os.path.join(actual, entries[0])
        else:
            break
    try:
        with os.scandir(actual) as it:
            return sum(1 for e in it if e.is_file())
    except OSError:
        return 0


@router.get("/{instance_id}/prepare-progress")
def get_prepare_progress(instance_id: str, user: dict = Depends(get_current_user)):
    """准备中实例的下载进度：返回已下载到实例 configs 目录的 config 文件数。

    obsutil 整目录下载不逐文件回报，但文件会实时落到磁盘，据此数出「已下载 N 个」。
    下载完成前无法预知总数，故只报已下载数（单调递增）；实例非 preparing 时 downloading=false。
    只读文件系统、不写库，多 worker 安全。
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT status, total_tasks FROM task_instances WHERE id=?", (instance_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")

    configs_dir = os.path.join(_get_instance_dir(instance_id), "configs")
    downloaded = _count_config_files(configs_dir)
    return {
        "status": row["status"],
        "downloading": row["status"] == "preparing",
        "downloaded_configs": downloaded,
        "total_tasks": row["total_tasks"] or 0,
    }


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


_STATUS_RE = re.compile(
    r"任务执行状态=(?P<status>任务成功|任务失败|任务异常)\s+error_code=(?P<code>\S+)"
)
# 状态行里的 config=<文件名> 字段，用于把 task_idx 映射到具体配置名（进而查 eval 缓存）
_CONFIG_RE = re.compile(r"config=(?P<config>\S+)")


def _scan_task_file(fpath: str):
    """读取单个 task 日志，返回 (last_status, last_code, last_config)；异常返回 (None, None, None)。"""
    last_status = None
    last_code = None
    last_config = None
    try:
        with open(fpath, "r", errors="replace") as f:
            for line in f:
                m = _STATUS_RE.search(line)
                if m:
                    last_status = m.group("status")
                    last_code = m.group("code")
                    mc = _CONFIG_RE.search(line)
                    if mc:
                        last_config = mc.group("config")
    except OSError:
        return None, None, None
    return last_status, last_code, last_config


def _compute_analyze(config_path: str, total_tasks: int) -> dict:
    """
    增量扫描：只重读 mtime/size 变化过的 task 日志。
    已完成的任务日志不再变化，后续轮询直接复用上次结果，
    使耗时正比于「本轮新增/变化的文件数」而非日志总数。
    """
    output_dir = _get_output_dir(config_path)
    logs_dir = os.path.join(output_dir, "logs")

    prev_state = _analyze_file_state.get(config_path, {})
    new_state = {}
    changed = set()  # 本轮实际重读过（签名变化 / 新增）的 fname，供增量写库

    if os.path.isdir(logs_dir):
        try:
            entries = os.scandir(logs_dir)
        except OSError:
            entries = []
        for entry in entries:
            fname = entry.name
            if not (fname.startswith("task-") and fname.endswith(".log")):
                continue
            try:
                st = entry.stat()
                sig = (st.st_mtime, st.st_size)
            except OSError:
                continue

            cached = prev_state.get(fname)
            if cached is not None and cached[0] == sig[0] and cached[1] == sig[1]:
                # 文件未变化，复用上次解析结果，跳过读取
                new_state[fname] = cached
            else:
                status, code, cfg = _scan_task_file(entry.path)
                new_state[fname] = (sig[0], sig[1], status, code, cfg)
                changed.add(fname)

    _analyze_file_state[config_path] = new_state
    _analyze_changed[config_path] = changed

    succeeded = 0
    failed = 0
    abnormal_by_prefix = {"C": 0, "S": 0, "T": 0, "X": 0}
    # 具体错误码计数：{prefix: {code: count}}
    abnormal_by_code = {"C": {}, "S": {}, "T": {}, "X": {}}

    for _mtime, _size, last_status, last_code, _cfg in new_state.values():
        if last_status == "任务成功":
            succeeded += 1
        elif last_status == "任务失败":
            failed += 1
        elif last_status == "任务异常":
            code = last_code or ""
            prefix = code[:1]
            if prefix not in abnormal_by_prefix:
                prefix = "X"
                code = code or "X999"
            abnormal_by_prefix[prefix] += 1
            abnormal_by_code[prefix][code] = abnormal_by_code[prefix].get(code, 0) + 1

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

    # 构建折叠树：一级分类汇总 + 具体错误码明细（按码计数降序）
    error_tree = []
    for p in ("C", "S", "T", "X"):
        if not abnormal_by_prefix[p]:
            continue
        codes = [
            {"code": code, "desc": _ERROR_CODE_DESC.get(code, ""), "count": cnt}
            for code, cnt in sorted(
                abnormal_by_code[p].items(), key=lambda kv: (-kv[1], kv[0])
            )
        ]
        error_tree.append({
            "prefix": p,
            "label": _PREFIX_LABEL[p],
            "count": abnormal_by_prefix[p],
            "codes": codes,
        })

    _analyze_cache[config_path] = (result, time.time())
    _analyze_tree_cache[config_path] = error_tree
    return result


# ---- per-task 明细：把增量扫描结果 + evaluator 评分聚合进 task_records 表 ----

def _compute_task_rows(config_path: str, only_fnames=None) -> list[dict]:
    """基于 _analyze_file_state 的增量扫描结果，产出每任务行（不额外读盘）。
    依赖调用前 _compute_analyze 已刷新过该 config_path 的 file_state。
    only_fnames 非 None 时只产出这些 fname 对应的行（增量写库用）；None 则全量。
    config= 尚未解析出（config_name 为 None）的任务直接跳过——其 status 也为 None、
    不贡献任何状态，若用文件名兜底会与后续真实 config_name 形成两行（phantom）。"""
    state = _analyze_file_state.get(config_path, {})
    if only_fnames is not None:
        items = ((f, state[f]) for f in only_fnames if f in state)
    else:
        items = state.items()
    rows = []
    for fname, tup in items:
        # tup = (mtime, size, status, code, config_name)
        _mtime, _size, status, code, config_name = tup
        if not config_name:
            # config= 未解析出：跳过，避免用文件名兜底制造 phantom 行
            continue
        m = re.search(r"task-(\d+)\.log", fname)
        task_idx = int(m.group(1)) if m else None
        category = None
        if status == "任务异常":
            prefix = (code or "")[:1]
            category = prefix if prefix in ("C", "S", "T") else "X"
        rows.append({
            "task_idx": task_idx,
            "config_name": config_name,
            "status": status,
            "error_code": code,
            "error_category": category,
        })
    return rows


def _load_eval_cache(instance_id: str) -> dict:
    """读取 instances/<id>/_eval_score.json（键为 config stem）；不存在返回 {}。不触发 OBS 下载。"""
    cache_file = os.path.join(_get_instance_dir(instance_id), "_eval_score.json")
    if not os.path.exists(cache_file):
        return {}
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_traj_cache(instance_id: str) -> dict:
    """读取 instances/<id>/_traj_score.json（键为 config stem / OBS task_dir）；不存在返回 {}。不触发 OBS 下载。"""
    cache_file = os.path.join(_get_instance_dir(instance_id), "_traj_score.json")
    if not os.path.exists(cache_file):
        return {}
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _sync_task_records(instance_id: str, config_path: str, full: bool = False) -> None:
    """把 per-task 扫描结果 + eval / traj 缓存 UPSERT 进 task_records（跑在执行器线程里，off-event-loop）。
    eval/traj 各列用 COALESCE：本地缓存尚未刷新时保留上次已写入的值。

    增量：默认只 UPSERT 本轮 _compute_analyze 中签名变化过的任务行（_analyze_changed），
    避免每 8s 对整个实例的数万行整表重写。full=True 或该 config 尚未被扫过时走全量。
    """
    changed = _analyze_changed.get(config_path)
    if full or changed is None:
        only_fnames = None  # 全量：首次填充 / eval 分数刷新 / 兜底
    elif not changed:
        return  # 本轮无文件变化，零写入
    else:
        only_fnames = changed

    rows = _compute_task_rows(config_path, only_fnames=only_fnames)
    if not rows:
        return
    eval_map = _load_eval_cache(instance_id)
    traj_map = _load_traj_cache(instance_id)
    with get_connection() as conn:
        for r in rows:
            stem = Path(r["config_name"]).stem
            ev = eval_map.get(stem) or {}
            tj = traj_map.get(stem) or {}
            conn.execute(
                """
                INSERT INTO task_records
                    (instance_id, task_idx, config_name, status, error_code, error_category,
                     eval_score, eval_completion, gate, traj_level, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(instance_id, config_name) DO UPDATE SET
                    task_idx        = excluded.task_idx,
                    status          = excluded.status,
                    error_code      = excluded.error_code,
                    error_category  = excluded.error_category,
                    eval_score      = COALESCE(excluded.eval_score, task_records.eval_score),
                    eval_completion = COALESCE(excluded.eval_completion, task_records.eval_completion),
                    gate            = COALESCE(excluded.gate, task_records.gate),
                    traj_level      = COALESCE(excluded.traj_level, task_records.traj_level),
                    updated_at      = CURRENT_TIMESTAMP
                """,
                (
                    instance_id, r["task_idx"], r["config_name"], r["status"],
                    r["error_code"], r["error_category"],
                    ev.get("score"), ev.get("completion"), ev.get("gate"),
                    tj.get("level"),
                ),
            )


def recover_orphan_preparing() -> int:
    """启动时回收「孤儿 preparing」实例。

    _prepare_and_launch 是挂在事件循环上的后台协程；若后端在「准备中」重启，
    该协程随旧进程一起消失，实例会永远卡在 preparing（pid/started_at 均为空）。
    这里在 startup 时把这些孤儿一律退回 created，让用户重新点启动（配置已就绪，
    重启动会重新数 task 数并拉起进程）。

    仅回退 pid 与 started_at 均为空的 preparing 实例——正在正常准备中、
    或已经拿到 pid 的实例不动，避免误伤。返回回退的实例数。
    幂等：多 worker 各调用一次也安全（第二次已无匹配行）。
    """
    try:
        with get_connection() as conn:
            cur = conn.execute(
                """
                UPDATE task_instances
                   SET status='created',
                       error_summary='准备中后端重启被中断，已自动退回 created，可重新启动'
                 WHERE status='preparing' AND pid IS NULL AND started_at IS NULL
                """
            )
            n = cur.rowcount or 0
        if n:
            print(f"[startup] 回收孤儿 preparing 实例 {n} 个 -> created")
        return n
    except Exception as e:
        print(f"[startup] 回收孤儿 preparing 失败: {e}")
        return 0


def _refresh_running_once():
    """后台线程：预热 running/preparing 实例的状态与分析缓存。

    含终态(completed/finished)最后一次同步：在线同步只在 running 期间跑，实例完成
    那一刻 `if inst["status"] == "running"` 的分支会错过最后一次写入；且运行窗口内若
    后台没跑到该实例，task_records 会永久缺行。用 task_done_sync 标记兜底：
    终态且 task_done_sync=0 的实例，每轮重扫直至写入成功后置位，缺行实例据此补齐。
    """
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM task_instances "
                "WHERE status IN ('running','preparing') "
                "OR (status IN ('completed','finished') AND task_done_sync=0)"
            ).fetchall()
    except Exception:
        return

    for row in rows:
        inst = dict(row)
        try:
            inst = _sync_instance_status(inst, persist=True)
            if inst["status"] == "running":
                _compute_analyze(inst["config_path"], inst["total_tasks"])
                # 复用刚刷新的 file_state，把 per-task 明细写进 task_records
                try:
                    _sync_task_records(inst["id"], inst["config_path"])
                except Exception:
                    pass
            elif inst["status"] in ("completed", "finished"):
                # 终态最后同步：在线同步只在 running 期间跑，完成那一刻/运行窗口内漏跑的
                # 实例 task_records 会永久缺行。task_done_sync=0 的终态实例在此补网——
                # 仅当 task_records 整体为空(0 行)时才需要 full 重扫；行已完整的实例
                # 直接置位，避免对海量历史终态实例全量重扫。写库成功才置位，否则下轮
                # refresher 重试（任务日志已定稿，增量扫描 changed 为空集，故 full=True
                # 全量写一次，ON CONFLICT 幂等）。
                gap = 0
                with get_connection() as conn:
                    gap = conn.execute(
                        "SELECT COUNT(*) FROM task_records WHERE instance_id=?",
                        (inst["id"],),
                    ).fetchone()[0]
                if gap == 0:
                    _compute_analyze(inst["config_path"], inst["total_tasks"])
                    _sync_task_records(inst["id"], inst["config_path"], True)
                with get_connection() as conn:
                    conn.execute(
                        "UPDATE task_instances SET task_done_sync=1 WHERE id=?",
                        (inst["id"],),
                    )
        except Exception:
            continue


async def background_cache_refresher(interval: float = 8.0):
    """
    周期性在线程池里刷新缓存，使 overview/list 请求线程只读缓存、永不触发扫描，
    从根本上避免多客户端并发把 anyio 同步线程池打满。
    """
    global _bg_refresh_active
    # 先同步预热一轮，再开启「请求只读缓存」模式，避免首轮请求撞冷扫描
    try:
        await async_execute(_refresh_running_once)
    except Exception:
        pass
    _bg_refresh_active = True
    while True:
        await asyncio.sleep(interval)
        try:
            await async_execute(_refresh_running_once)
        except Exception:
            pass


def _analyze_task_status(config_path: str, total_tasks: int, instance_status: str = "running") -> dict:
    now = time.time()
    cache_key = config_path
    cached = _analyze_cache.get(cache_key)

    # 非 running：状态已定，命中缓存直接返回，永不重扫
    if instance_status not in ("running",):
        if cached:
            return cached[0]

    if cached and (now - cached[1]) < _ANALYZE_CACHE_TTL:
        return cached[0]

    # 后台预热启用时，请求线程只读缓存（哪怕已过期），把扫描留给后台任务，
    # 避免多客户端并发把 anyio 同步线程池打满
    if _bg_refresh_active and cached:
        return cached[0]

    return _compute_analyze(config_path, total_tasks)


# task_records 允许的排序列白名单（防 SQL 注入）
_TASK_RECORDS_SORT = {
    "task_idx", "config_name", "status", "error_code",
    "error_category", "eval_score", "eval_completion", "gate", "updated_at",
}


@router.get("/{instance_id}/task-records")
async def list_task_records(
    instance_id: str,
    category: str | None = Query(None, description="C/S/T/X 或 success"),
    status: str | None = Query(None, description="任务成功/任务失败/任务异常"),
    min_score: float | None = Query(None, description="eval_score 下限"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    sort_by: str = Query("task_idx"),
    sort_dir: str = Query("asc"),
    user: dict = Depends(get_current_user),
):
    """按实例查询 per-task 明细，支持按错误分类/状态/分数筛选、分页、排序。"""
    row = await async_query_one(
        "SELECT * FROM task_instances WHERE id=?", (instance_id,)
    )
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")

    config_path = row["config_path"]

    # 确保 task_records 已填充（running 刷新 / 老实例 0 行兜底）
    await _ensure_task_records(instance_id, config_path, row["status"], row["total_tasks"])

    # 组装筛选条件
    where = ["instance_id = ?"]
    params: list = [instance_id]
    if category:
        if category == "success":
            where.append("status = ?")
            params.append("任务成功")
        else:
            where.append("error_category = ?")
            params.append(category)
    if status:
        where.append("status = ?")
        params.append(status)
    if min_score is not None:
        where.append("eval_score >= ?")
        params.append(min_score)
    where_sql = " AND ".join(where)

    # 排序：列白名单 + 方向白名单
    col = sort_by if sort_by in _TASK_RECORDS_SORT else "task_idx"
    direction = "DESC" if str(sort_dir).lower() == "desc" else "ASC"

    total_row = await async_query_one(
        f"SELECT COUNT(*) AS n FROM task_records WHERE {where_sql}", tuple(params)
    )
    total = total_row["n"] if total_row else 0

    offset = (page - 1) * page_size
    tasks = await async_query(
        f"SELECT task_idx, config_name, status, error_code, error_category, "
        f"eval_score, eval_completion, gate, traj_level "
        f"FROM task_records WHERE {where_sql} "
        f"ORDER BY {col} {direction} LIMIT ? OFFSET ?",
        tuple(params) + (page_size, offset),
    )

    return {"total": total, "page": page, "page_size": page_size, "tasks": tasks}


async def _ensure_task_records(instance_id: str, config_path: str, status: str, total_tasks: int):
    """确保 task_records 已填充：running 每次刷新；非 running 且 0 行时兜底同步一次。"""
    if status == "running":
        try:
            await async_execute(_sync_task_records, instance_id, config_path)
        except Exception:
            pass
        return
    existing = await async_query_one(
        "SELECT COUNT(*) AS n FROM task_records WHERE instance_id=?", (instance_id,)
    )
    if not existing or existing["n"] == 0:
        try:
            await async_execute(_compute_analyze, config_path, total_tasks)
            # 兜底填充：非 running 且 0 行，需全量写入（此时 changed 可能为空集）
            await async_execute(_sync_task_records, instance_id, config_path, True)
        except Exception:
            pass


@router.get("/{instance_id}/task-status-map")
async def task_status_map(instance_id: str, user: dict = Depends(get_current_user)):
    """返回全量 {task_idx: {status, category, code}}，供日志页下拉框着色（精简、无分页）。"""
    row = await async_query_one(
        "SELECT * FROM task_instances WHERE id=?", (instance_id,)
    )
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")

    await _ensure_task_records(instance_id, row["config_path"], row["status"], row["total_tasks"])

    rows = await async_query(
        "SELECT task_idx, status, error_code, error_category "
        "FROM task_records WHERE instance_id=? AND task_idx IS NOT NULL",
        (instance_id,),
    )
    mapping = {
        r["task_idx"]: {
            "status": r["status"],
            "category": r["error_category"],
            "code": r["error_code"],
        }
        for r in rows
    }
    return {"map": mapping}


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
    _analyze_tree_cache.pop(inst.get("config_path"), None)
    _analyze_file_state.pop(inst.get("config_path"), None)
    _analyze_changed.pop(inst.get("config_path"), None)

    with get_connection() as conn:
        conn.execute("DELETE FROM task_records WHERE instance_id=?", (instance_id,))
        conn.execute("DELETE FROM task_instances WHERE id=?", (instance_id,))

    return {"message": "实例已删除"}
