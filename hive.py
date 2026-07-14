"""
Agent Distillation Task Runner

A configuration-driven task orchestration system for running AI Agent tasks
in remote sandboxed environments (k8s/docker). 
- openclaw
- hermes
- claude-code
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import copy
import json
import logging
import os
import random
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional, List

import aiofiles
from omegaconf import OmegaConf, DictConfig

from execution_client.client.client import ExecutionClient, make
from execution_client.core.error_code import ErrorCode
from execution_client.core.logger import init_logger, get_logger
from execution_client.core.utils import generate_random_port
from execution_client.models.request import EnvMakeRequest, ExtendExecCommand, ExtendUploadFile
from execution_client.models.response import Result

from env_sdk.compat.legacy_adapter import build_make_config   # 安装新版sdk, 新增导入该项

# Module-level logger
logger = get_logger()

# Async lock for file writes
_file_lock = asyncio.Lock()

# ContextVar for per-task log routing
_current_task_idx = contextvars.ContextVar('current_task_idx', default=None)

# Default timeout for OBS download operations (seconds)
OBS_DOWNLOAD_TIMEOUT = 1200
OBS_UPLOAD_TIMEOUT = 900

# ============================================================================
# Agent framework — module-level switch
# ============================================================================

# 每框架的目录布局 — 用这个 dict 派生所有路径默认值
_FRAMEWORK_LAYOUTS = {
    "openclaw": {
        "harness_dir":            "/home/ma-user/.openclaw",
        "harness_local_config":   "uploads/openclaw.json",
        "harness_sandbox_config": "/home/ma-user/.openclaw/openclaw.json",
        "upload_paths": [
            "/home/ma-user/.openclaw/agents",
        ],
        "workspace_base": "/home/ma-user/.openclaw/workspace",
    },
    "hermes": {
        "harness_dir":            "/home/ma-user/.hermes",
        "harness_local_config":   "uploads/config.yaml",
        "harness_sandbox_config": "/home/ma-user/.hermes/config.yaml",
        "upload_paths": [
            "/home/ma-user/.hermes/profiles",
            "/home/ma-user/.hermes/sessions", 
            "/home/ma-user/.hermes/logs", 
            "/home/ma-user/.hermes/state.db"
        ],
        # workspace 隔离在 profiles/<name>/ 里
        "workspace_base": None,
    },
    "claude-code": {
        "harness_dir":            "/home/ma-user/.claude",
        "harness_local_config":   "uploads/settings.json",
        "harness_sandbox_config": "/home/ma-user/.claude/settings.json",
        "upload_paths": [
            "/home/ma-user/.claude/projects",
            "/home/ma-user/.claude/todos",
            "/home/ma-user/.claude/debug",
            "/home/ma-user/.claude/session-env",
        ],
        "workspace_base": "/home/ma-user/.claude/workspace",
    },
}

AGENT_FRAMEWORK: str = "openclaw"  # 占位默认, main() 会覆盖
_FW: dict = _FRAMEWORK_LAYOUTS[AGENT_FRAMEWORK]

def set_agent_framework(name: str) -> None:
    """Set the module-level framework switch. Called from ``main()`` exactly once."""
    global AGENT_FRAMEWORK, _FW
    name = (name or "").strip().lower()
    if name not in _FRAMEWORK_LAYOUTS:
        raise RuntimeError(
            f"harness_type 必须是 {_FRAMEWORK_LAYOUTS.keys()}, got {name!r}"
        )
    AGENT_FRAMEWORK = name
    _FW = _FRAMEWORK_LAYOUTS[name]


def get_obsutil_downloader_command(s3_config, objects_storage_path, bucket_path):
    command = f"{s3_config.s3_download_script} cp {s3_config.bucket_name}/{bucket_path} {objects_storage_path} -r -f"
    return command


def get_obsutil_uploader_command(s3_config, local_folder_absolute_path, bucket_path):
    command = f"{s3_config.s3_download_script} cp {local_folder_absolute_path} {s3_config.bucket_name}/{bucket_path} -r -f"
    return command


class TaskFileHandler(logging.Handler):
    """Routes log records to per-task files based on ContextVar."""

    def __init__(self, log_dir):
        super().__init__()
        self.log_dir = log_dir
        self._files = {}

    def emit(self, record):
        task_idx = _current_task_idx.get()
        if task_idx is None:
            return
        try:
            if task_idx not in self._files:
                path = os.path.join(self.log_dir, f"task-{task_idx}.log")
                self._files[task_idx] = open(path, 'a', encoding='utf-8')
            msg = self.format(record)
            self._files[task_idx].write(msg + '\n')
            self._files[task_idx].flush()
        except Exception:
            self.handleError(record)

    def close(self):
        for f in self._files.values():
            f.close()
        self._files.clear()
        super().close()


# ============================================================================
# Configuration Dataclasses
# ============================================================================

@dataclass
class ObsBucketConfig:
    """OBS bucket configuration for traj and skill storage."""
    download_timeout: int = OBS_DOWNLOAD_TIMEOUT
    upload_timeout: int = OBS_UPLOAD_TIMEOUT
    s3_download_script: str = "obsutil"
    traj_save_bucket: str = "obs://rl-agentdata"
    traj_save_path: str = ""
    skill_download_path: str = "skills/260325/skill_localize/skills_library"
    user_profile_download_path: str = ""
    user_config_download_path: str = ""
    agents_download_path: str = ""
    default_skills: list = field(default_factory=list)


@dataclass
class SandboxConfig:
    """Sandbox environment configuration."""
    home: str = "/home/ma-user"
    workspace: str = f"{home}/workspace"
    result_workdir: str = f"{workspace}/workdir"
    result_log: str = "run.log"
    data_config_path: str = f"{workspace}/config"
    harness_dir: str = field(default_factory=lambda: _FW["harness_dir"])
    default_skill_path: str = f"{harness_dir}/skills"
    harness_sandbox_config_file: str = field(default_factory=lambda: _FW["harness_sandbox_config"])
    harness_local_config_file: str = field(default_factory=lambda: _FW["harness_local_config"])
    # openclaw用于启动gateway
    openclaw_bash: str = "/usr/local/node24/bin/openclaw"
    gateway_log: str = "gateway.log"
    openclaw_start_timeout: int = 10
    user_proxy_model_local_file: str = "uploads/user_proxy_model.json"
    user_proxy_model_remote_file: str = "configs/user_proxy_model.json"

@dataclass
class TaskConfig:
    """Main task configuration."""
    run_config_file: str
    task_input_path: str = "uploads/configs"
    task_output_path: str = "outputs"
    task_complete_record: str = "complete.jsonl"
    task_failed_record: str = "failed.jsonl"
    task_download_path: str = "downloads"
    main_code_tar: str = "uploads/openclaw-task.tar"
    main_code_dir: str = ""
    main_python_file: str = "harness_automation.py"
    main_python_timeout: int = 7200
    openclaw_gateway_timeout: int = 300
    simple_bash_timeout: int = 10
    obs_config: ObsBucketConfig = None  # field(default_factory=ObsBucketConfig)
    sandbox_config: SandboxConfig = field(default_factory=SandboxConfig)
    run_input_config_files: set = field(default_factory=set)
    run_output_complete_record: set = field(default_factory=set)


@dataclass
class DataConfig:
    """Data configuration parsed from JSON config files."""
    system: dict = field(default_factory=dict)
    input_dir: dict = field(default_factory=dict)
    agents: list[dict] = field(default_factory=list)
    queries: list[dict] = field(default_factory=list)
    gateway_ws_url: str = ""
    api_key: Optional[str] = None
    workspace_base: str = ""
    simulator_config: str = ""
    harness_type: str = ""


# ============================================================================
# Utility Functions
# ============================================================================

def run_cmd_stream(cmd: List[str], timeout: Optional[int] = None) -> int:
    """
    流式执行命令，实时打印 stdout 和 stderr。
    返回命令的退出码（0 表示成功）。
    """
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    # 定义读取函数
    def read_stream(stream, prefix):
        for line in iter(stream.readline, ''):
            if line:
                logger.info(f"{prefix}{line}")
        stream.close()

    # 创建两个线程分别读取 stdout 和 stderr
    t1 = threading.Thread(target=read_stream, args=(process.stdout, "[OUT] "))
    t2 = threading.Thread(target=read_stream, args=(process.stderr, "[ERR] "))
    t1.daemon = True
    t2.daemon = True
    t1.start()
    t2.start()

    # 等待进程结束，支持超时
    try:
        retcode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.terminate()          # 尝试终止
        try:
            process.wait(timeout=5)  # 给进程5秒清理
        except subprocess.TimeoutExpired:
            process.kill()           # 强制杀死
        raise                        # 重新抛出超时异常

    return retcode


def parse_data_config(data_config_file: str) -> DataConfig:
    """Parse a data config JSON file into a DataConfig dataclass."""
    with open(data_config_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    known = {f.name for f in fields(DataConfig)}
    return DataConfig(**{k: v for k, v in data.items() if k in known})


def load_yaml_config(config_file: str) -> DictConfig:
    """Load and return an OmegaConf config object."""
    return OmegaConf.load(config_file)

def check_bucket_path(bucket_path: str) -> str:
    if bucket_path and not bucket_path.endswith("/"):
        bucket_path += "/"
    return bucket_path

# ============================================================================
# Task Execution
# ============================================================================

class OpenClawDistillationTask:
    """Executes a single distillation task in a sandboxed environment."""

    def __init__(self, config: TaskConfig):
        self.logger = logger
        self.config = config
        self.complete_record_file: str = ""
        self.failed_record_file: str = ""

        yaml_config = load_yaml_config(self.config.run_config_file)
        self.global_config = yaml_config
        self.client_config = self._init_config(yaml_config)
        self.execution_client: Optional[ExecutionClient] = None

    def _init_config(self, yaml_config: DictConfig) -> DictConfig:
        """Initialize and return the client config with runtime values."""
        s3 = yaml_config.s3
        sandbox_id_prefix = yaml_config.sandbox_id_prefix
        config = copy.deepcopy(yaml_config)
        config = build_make_config("x86_cpu", config=yaml_config)
        config.s3 = s3
        config.env_make.env_id = f"{sandbox_id_prefix}-{uuid.uuid4().hex}"

        # Ensure output directories exist
        self.complete_record_file = os.path.join(
            self.config.task_output_path, self.config.task_complete_record
        )
        record_dir = os.path.dirname(self.complete_record_file)
        if record_dir:
            Path(record_dir).mkdir(parents=True, exist_ok=True)

        self.failed_record_file = os.path.join(
            self.config.task_output_path, self.config.task_failed_record
        )
        return config

    async def _save_record(self, file_path: str, config: str) -> None:
        """Append a config filename to a record file."""
        if not config:
            self.logger.error(f"save_record error: config={config}, file_path={file_path}")
            return

        config_basename = os.path.basename(config)
        async with _file_lock:
            async with aiofiles.open(file_path, "a", encoding="utf-8") as f:
                await f.write(config_basename + "\n")

    async def _upload_file(self, file_info: str, local_path: str, remote_path: str) -> None:
        """Upload a file to the sandbox."""
        upload_request = ExtendUploadFile(upload_path=local_path, remote_path=remote_path)
        result = await self.execution_client.extend(args=upload_request.to_dict())
        if result.code != ErrorCode.SUCCESS[0]:
            msg = f"upload {file_info}: {local_path} -> {remote_path} failed: {result.msg}"
            self.logger.error(msg)
            raise RuntimeError(msg)

    async def _copy_agent_config(self) -> None:
        """Copy main agent configuration to sandbox."""
        remote_path = self.config.sandbox_config.harness_sandbox_config_file
        local_path = self.config.sandbox_config.harness_local_config_file
        local_model_path = self.config.sandbox_config.user_proxy_model_local_file
        # 根据不同的harness更新文件
        if AGENT_FRAMEWORK == "openclaw":
            data = json.loads(Path(local_path).read_text(encoding="utf-8"))
            model_cfg = json.loads(Path(local_model_path).read_text(encoding="utf-8"))

            data.setdefault("models", {}).setdefault("mode", "merge")
            providers = data["models"].setdefault("providers", {})
            agents_section = data.setdefault("agents", {})
            agents_list = agents_section.setdefault("list", [])

            for agent_name, cfg in model_cfg.items():
                if agent_name == "user_simulator":
                    continue
                if not (cfg.get("model") and cfg.get("base_url")
                        and cfg.get("api_key") and cfg.get("provider")):
                    self.logger.warning(
                        f"agent {agent_name} missing model/base_url/api_key/provider, skip"
                    )
                    continue

                api_kind = cfg.get("api") or "anthropic-messages"
                provider_key = cfg["provider"]
                model_ref = f"{provider_key}/{cfg['model']}"

                # 1) providers 注入
                providers[provider_key] = {
                    "baseUrl": cfg["base_url"],
                    "apiKey": cfg["api_key"],
                    "api": api_kind,
                    "models": [
                        {
                            "id": cfg["model"],
                            "name": cfg["model"],
                            "api": api_kind,
                            "reasoning": True,
                            "input": ["text"],
                            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                            "contextWindow": 200000,
                            "maxTokens": 327680,
                            "compat": {"maxTokensField": "max_tokens"},
                        }
                    ],
                }

                # 2) agents.list 里 upsert 对应条目
                entry = {
                    "id": agent_name,
                    "name": agent_name,
                    "workspace": f"/home/ma-user/.openclaw/workspace-{agent_name}",
                    "agentDir": f"/home/ma-user/.openclaw/agents/{agent_name}",
                    "model": {"primary": model_ref},
                    "models": {model_ref: {}},
                }
                idx = next(
                    (i for i, e in enumerate(agents_list)
                     if isinstance(e, dict) and e.get("id") == agent_name),
                    None,
                )
                if idx is None:
                    agents_list.append(entry)
                else:
                    agents_list[idx] = {**agents_list[idx], **entry}

                self.logger.info(
                    f"Injected provider={provider_key} model={model_ref} for agent {agent_name}"
                )

            Path(local_path).write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

        await self._upload_file("agent config", local_path, remote_path)
        self.logger.info(f"Copied agent config: {local_path} -> {remote_path}")

    async def _upload_and_extract_code(self) -> None:
        """Upload and extract the main code tarball in sandbox."""
        tar_basename = os.path.basename(self.config.main_code_tar)
        remote_path = os.path.join(self.config.sandbox_config.workspace, tar_basename)
        await self._upload_file("code tarball", self.config.main_code_tar, remote_path)

        # Extract code in sandbox
        command = f"cd {self.config.sandbox_config.workspace} && tar -xf {tar_basename}"
        exec_request = ExtendExecCommand(
            command=["/bin/bash", "-c", command],
            timeout=self.config.simple_bash_timeout,
        )
        result = await self.execution_client.extend(args=exec_request.to_dict())
        if result.code != ErrorCode.SUCCESS[0] or result.data["exit_code"] != 0:
            msg = f"extract code failed: {result.msg or result.data}"
            self.logger.error(msg)
            raise RuntimeError(msg)
        self.logger.info("Code extracted successfully")

    async def _upload_data_config(self, config_file: str) -> None:
        """Upload task data config file to sandbox."""
        remote_path = os.path.join(
            self.config.sandbox_config.data_config_path, os.path.basename(config_file)
        )
        await self._upload_file("data config", config_file, remote_path)
        self.logger.info(f"Uploaded data config: {config_file} -> {remote_path}")

    async def _upload_user_proxy_model_config(self) -> None:
        local_path = self.config.sandbox_config.user_proxy_model_local_file
        code_stem = Path(self.config.main_code_tar).stem
        remote_path = os.path.join(
            self.config.sandbox_config.workspace,
            code_stem,
            self.config.sandbox_config.user_proxy_model_remote_file
        )
        await self._upload_file("user_proxy_model config", local_path, remote_path)
        self.logger.info(f"Copied user_proxy_model config: {local_path} -> {remote_path}")

    async def _start_openclaw_gateway(self, config_file: str) -> None:
        """Start the OpenClaw gateway in the sandbox."""
        # Read gateway port from local config
        with open(self.config.sandbox_config.harness_local_config_file, "r", encoding="utf-8") as f:
            openclaw_config = json.load(f)
            original_port = openclaw_config.get("gateway", {}).get("port")

        start_time = asyncio.get_running_loop().time()
        max_wait = self.config.openclaw_gateway_timeout

        while True:
            elapsed = asyncio.get_running_loop().time() - start_time
            if elapsed > max_wait:
                self.logger.error(f"Gateway startup timeout ({max_wait}s)")
                raise RuntimeError(f"Gateway startup timeout for {config_file}")

            port = str(generate_random_port())
            remote_config_path = os.path.join(
                self.config.sandbox_config.data_config_path, os.path.basename(config_file)
            )

            # Re-copy original config before sed (needed because config may have been modified in previous retry)
            await self._copy_agent_config()

            # Update port in remote config files
            for remote_file in [self.config.sandbox_config.harness_sandbox_config_file, remote_config_path]:
                sed_cmd = f"sed -i 's/{original_port}/{port}/g' {remote_file}"
                exec_request = ExtendExecCommand(
                    command=["/bin/bash", "-c", sed_cmd],
                    timeout=self.config.simple_bash_timeout,
                )
                result = await self.execution_client.extend(args=exec_request.to_dict())
                if result.code != ErrorCode.SUCCESS[0] or result.data["exit_code"] != 0:
                    raise RuntimeError(f"Failed to update port: {result.msg}")

            # Start gateway
            gateway_log_path = os.path.join(
                self.config.sandbox_config.result_workdir, self.config.sandbox_config.gateway_log
            )
            gateway_cmd = [
                "/bin/bash", "-c",
                f"mkdir -p {self.config.sandbox_config.result_workdir} && "
                f"nohup {self.config.sandbox_config.openclaw_bash} gateway --port {port} "
                f"> {gateway_log_path} 2>&1 & sleep {self.config.sandbox_config.openclaw_start_timeout} && cat {gateway_log_path}"
            ]
            exec_request = ExtendExecCommand(
                command=gateway_cmd,
                timeout=self.config.sandbox_config.openclaw_start_timeout + 5
            )
            result = await self.execution_client.extend(args=exec_request.to_dict())

            if result.code != ErrorCode.SUCCESS[0] or result.data["exit_code"] != 0:
                raise RuntimeError(f"Gateway start failed: {result.msg}")

            stdout = result.data.get("stdout", "")
            if f"Port {port} is already in use" in stdout:
                self.logger.warning(f"Port {port} in use, retrying...")
                await asyncio.sleep(2)
            elif f"listening on ws://127.0.0.1:{port}" in stdout or f"listening on http://127.0.0.1:{port}":
                break
            else:
                raise RuntimeError(f"Gateway start unexpected output: {result.data}")

        self.logger.info(f"Gateway started on port {port}")

    async def _run_main_script(self, config_file: str, task_idx: int) -> None:
        """Execute the main Python script in the sandbox."""
        python_file = os.path.basename(self.config.main_python_file)
        config_path = os.path.join(
            self.config.sandbox_config.data_config_path, os.path.basename(config_file)
        )
        code_stem = Path(self.config.main_code_tar).stem
        log_path = self.config.sandbox_config.result_workdir
