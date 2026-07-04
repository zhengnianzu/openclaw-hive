"""
Agent Distillation Task Runner

A configuration-driven task orchestration system for running AI Agent tasks
in remote sandboxed environments (k8s/docker). 
- OpenClaw
- Hermes
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
from execution_client.core.logging import ManageLogger
from execution_client.core.utils import generate_random_port, get_obsutil_downloader_command, get_obsutil_uploader_command
from execution_client.models.request import EnvMakeRequest, ExtendExecCommand, ExtendUploadFile
from execution_client.models.response import Result
from execution_client.core.rmq_client import get_rmq_client


# Module-level logger
logger = ManageLogger(__name__).get_logger()

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
        "default_skill_path":     "/home/ma-user/.openclaw/skills",
        "harness_local_config":   "uploads/openclaw.json",
        "harness_sandbox_config": "/home/ma-user/.openclaw/openclaw.json",
        "main_python_file":    "openclaw_automation.py",
        "upload_paths": [
            "/home/ma-user/.openclaw/agents",
            "/home/ma-user/.openclaw/workspace"
        ]
    },
    "hermes": {
        "harness_dir":            "/home/ma-user/.hermes",
        "default_skill_path":     "/home/ma-user/.hermes/skills",
        "harness_local_config":   "uploads/config.yaml",
        "harness_sandbox_config": "/home/ma-user/.hermes/config.yaml",
        "main_python_file":    "hermes_automation.py",
        "upload_paths": [
            "/home/ma-user/.hermes/profiles",
            "/home/ma-user/.hermes/sessions", 
            "/home/ma-user/.hermes/logs", 
            "/home/ma-user/.hermes/state.db", 
            "/home/ma-user/.hermes/api_use.log"
        ]
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
            f"remote_server.project_id 必须是 'openclaw' 或 'hermes', got {name!r}"
        )
    AGENT_FRAMEWORK = name
    _FW = _FRAMEWORK_LAYOUTS[name]


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
    default_skill_path: str = field(default_factory=lambda: _FW["default_skill_path"])
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
    main_python_file: str = field(default_factory=lambda: _FW["main_python_file"])
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
        self.rmq_client = get_rmq_client(yaml_config)
        self.client_config = self._init_config(yaml_config)
        self.execution_client: Optional[ExecutionClient] = None

    def _init_config(self, yaml_config: DictConfig) -> DictConfig:
        """Initialize and return the client config with runtime values."""
        config = copy.deepcopy(yaml_config)
        port = str(generate_random_port())

        # Update ADAPTOR_PORT in environment variables
        for env_dict in config.env_make.args.env:
            if env_dict.get("name") == "ADAPTOR_PORT":
                env_dict["value"] = port
                break

        config.env_make.env_id = uuid.uuid4().hex
        # Remove rabbitmq config as it's handled separately
        if "rabbitmq" in config.env_make:
            del config.env_make.rabbitmq

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
        if result.code != ErrorCode.SUCCESS[0]:
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
                if result.code != ErrorCode.SUCCESS[0]:
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

            if result.code != ErrorCode.SUCCESS[0]:
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

        command = (
            f"source /home/ma-user/.bashrc && cd {self.config.sandbox_config.workspace} && "
            f"mkdir -p {log_path} && cd {code_stem} && "
            f"pip install -r requirements.txt && "
            f"python {python_file} --config {config_path} 2>&1 | "
            f"tee {log_path}/{self.config.sandbox_config.result_log}"
        )

        exec_request = ExtendExecCommand(
            command=["/bin/bash", "-c", command],
            timeout=self.config.main_python_timeout,
            mode="stream",
        )
        result = await self.execution_client.extend(args=exec_request.to_dict())
        if result.code != ErrorCode.SUCCESS[0]:
            raise RuntimeError(f"Script execution failed: {result.msg or result.data}")
        self.logger.info(f"[{task_idx}] Script completed: {python_file}")

    async def _download_s3_skills(self, data_config_file: str) -> None:
        """Download skills from S3 to sandbox."""
        data_cfg = parse_data_config(data_config_file)

        task_skills = []
        if data_cfg.agents and data_cfg.agents[0].get("skills"):
            task_skills = data_cfg.agents[0]["skills"]

        default_skills = self.config.obs_config.default_skills or []

        if not task_skills and not default_skills:
            self.logger.warning("No skills found, skipping download")
            return

        # task_skills 下载到 workspace/<project>/{skill_dir}
        code_stem = Path(self.config.main_code_tar).stem
        skill_dir = data_cfg.input_dir.get("skill_dir", "skills")
        task_target_path = os.path.join(
            self.config.sandbox_config.workspace, code_stem, skill_dir
        )
        # default_skills 下载到 default_skill_path:
        default_target_path = f"{self.config.sandbox_config.default_skill_path}"

        async def download_skill(skill_path: str, target_path: str) -> None:
            bucket_path = os.path.join(
                self.config.obs_config.skill_download_path, skill_path
            ) + "/"
            command = get_obsutil_downloader_command(
                self.client_config.s3,
                objects_storage_path=target_path,
                bucket_path=bucket_path,
            )
            exec_request = ExtendExecCommand(
                command=["/bin/bash", "-c", f"mkdir -p {target_path} && {command}"],
                timeout=self.config.obs_config.download_timeout,
                mode="stream",
            )
            result = await self.execution_client.extend(args=exec_request.to_dict())
            if result.code != ErrorCode.SUCCESS[0]:
                raise RuntimeError(f"Skill download failed: {result.msg}")

        start_time = time.time()
        tasks = []
        for s in set(task_skills):
            tasks.append(download_skill(s, task_target_path))
        for s in set(default_skills):
            tasks.append(download_skill(s, default_target_path))
        await asyncio.gather(*tasks)
        self.logger.info(f"Downloaded skills (task={len(task_skills)}, default={len(default_skills)}) in {time.time() - start_time:.1f}s")

    async def _download_s3_user_profile(self, data_config_file: str) -> None:
        """Download user profile from S3 to sandbox."""
        if not self.config.obs_config.user_profile_download_path:
            logger.warning("no user profile path found")
            return

        data_cfg = parse_data_config(data_config_file)
        user_profile_path = data_cfg.input_dir.get("user_dir", {}).get("path", "")

        if not user_profile_path:
            self.logger.warning("No user profile path found")
            raise RuntimeError("No user profile path found")

        code_stem = Path(self.config.main_code_tar).stem
        objects_storage_path = os.path.join(
            self.config.sandbox_config.workspace,
            code_stem
        )
        user_dir = os.path.dirname(user_profile_path)
        if user_dir:
            objects_storage_path = os.path.join(
                objects_storage_path,
                user_dir
            )

        bucket_path = check_bucket_path(os.path.join(
            self.config.obs_config.user_profile_download_path,
            user_profile_path
        ))

        command = get_obsutil_downloader_command(
            self.client_config.s3,
            objects_storage_path=objects_storage_path,
            bucket_path=bucket_path
        )
        exec_request = ExtendExecCommand(
            command=["/bin/bash", "-c", f"mkdir -p {objects_storage_path} && {command}"],
            timeout=self.config.obs_config.download_timeout,
            mode="stream",
        )
        start_time = time.time()
        result = await self.execution_client.extend(args=exec_request.to_dict())
        if result.code != ErrorCode.SUCCESS[0]:
            raise RuntimeError(f"User profile download failed: {result.msg}")
        self.logger.info(f"Downloaded user profile in {time.time() - start_time:.1f}s")

    async def _download_s3_agents(self) -> None:
        """Download agent configs from S3 to sandbox."""
        bucket_path = self.config.obs_config.agents_download_path
        if not bucket_path:
            logger.warning("No agent bucket path found")
            return

        bucket_path = check_bucket_path(bucket_path)

        code_stem = Path(self.config.main_code_tar).stem
        objects_storage_path = os.path.join(self.config.sandbox_config.workspace, code_stem)

        command = get_obsutil_downloader_command(
            self.client_config.s3,
            objects_storage_path=objects_storage_path,
            bucket_path=bucket_path,
        )
        exec_request = ExtendExecCommand(
            command=["/bin/bash", "-c", f"mkdir -p {objects_storage_path} && {command}"],
            timeout=self.config.obs_config.download_timeout,
            mode="stream",
        )
        start_time = time.time()
        result = await self.execution_client.extend(args=exec_request.to_dict())
        if result.code != ErrorCode.SUCCESS[0]:
            raise RuntimeError(f"Agents download failed: {result.msg}")
        self.logger.info(f"Downloaded agents in {time.time() - start_time:.1f}s")

    async def _upload_traj_to_obs(self, config_file: str) -> bool:
        """Upload execution traj (logs) to OBS."""
        sandbox_cfg = self.config.sandbox_config
        bucket_path = os.path.join(
            self.config.obs_config.traj_save_path, Path(config_file).stem,
        )
        upload_clauses = []
        for src in [sandbox_cfg.result_workdir] + list(_FW["upload_paths"]):
            up_cmd = get_obsutil_uploader_command(
                self.client_config.s3, 
                local_folder_absolute_path=src,
                bucket_path=bucket_path
            )
            upload_clauses.append(f"([ -e {src} ] && {up_cmd}) || true")
        exec_cmd = " && ".join(upload_clauses) if upload_clauses else "true"

        for retry in range(3, 0, -1):
            exec_request = ExtendExecCommand(
                command=["/bin/bash", "-c", exec_cmd],
                timeout=self.config.obs_config.upload_timeout,
            )
            result = await self.execution_client.extend(args=exec_request.to_dict())
            if result.code == ErrorCode.SUCCESS[0]:
                self.logger.info(f"Uploaded traj to OBS: {config_file}")
                return True

            self.logger.warning(f"Upload failed, {retry} retries left: {result.msg}")
            await asyncio.sleep(random.uniform(3, 10))

        return False

    async def _execute_task(self, config_file: str, task_idx: int) -> None:
        """Execute the full task pipeline.

        两种模式只差一步:
          openclaw: 通过 _start_openclaw_gateway 启动 node gateway 进程
                    (内部会先 _copy_agent_config 把 openclaw.json 上传)
          hermes:   只 _copy_agent_config 把 ~/.hermes/config.yaml 上传
                    (没有 gateway 进程, AIAgent 进程内调用)
        其他步骤变量复用, 路径靠 config.yaml 覆写。
        """
        await self._upload_and_extract_code()
        await self._download_s3_skills(config_file)
        await self._download_s3_user_profile(config_file)
        await self._download_s3_agents()
        await self._upload_data_config(config_file)
        await self._upload_user_proxy_model_config()
        if AGENT_FRAMEWORK.strip().lower() == "hermes":
            await self._copy_agent_config()
        else:
            await self._start_openclaw_gateway(config_file)
        await self._run_main_script(config_file, task_idx)

        uploaded = await self._upload_traj_to_obs(config_file)
        if uploaded:
            await self._save_record(self.complete_record_file, config_file)
        else:
            await self._save_record(self.failed_record_file, config_file)

    async def run(self, config_file: str, task_idx: int = 0) -> None:
        """Run a single distillation task."""
        await asyncio.sleep(random.uniform(1, 10))
        start_time = time.perf_counter()

        try:
            request = EnvMakeRequest(**OmegaConf.to_container(self.client_config.env_make))
            result = await make(
                request, config=self.client_config, rmq_client=self.rmq_client
            )
            if isinstance(result, Result):
                raise RuntimeError(f"Environment creation failed: {result.msg}")
            self.execution_client = result
            self.logger.info(
                f">>>>>> Task {task_idx}: env={self.execution_client.get_env_id()} "
                f"elapsed={time.perf_counter() - start_time:.1f}s <<<<<<"
            )
            await self._execute_task(config_file, task_idx)
        except Exception as e:
            self.logger.error(f"Task {task_idx} failed: {traceback.format_exc()}")
            await self._save_record(self.failed_record_file, config_file)
        finally:
            if self.execution_client is not None and isinstance(self.execution_client, ExecutionClient):
                await self.execution_client.close()
            self.logger.info(f"Task {task_idx} finished, elapsed={time.perf_counter() - start_time:.1f}s")


# ============================================================================
# Task Orchestration
# ============================================================================

async def _worker(
    worker_id: int,
    task_queue: asyncio.Queue,
    config: TaskConfig
) -> None:
    """Worker coroutine that processes tasks from the queue."""
    while True:
        try:
            task_idx, config_name = task_queue.get_nowait()
        except asyncio.QueueEmpty:
            logger.info(f"Worker {worker_id}: queue empty, exiting")
            break

        try:
            if config_name in config.run_output_complete_record:
                logger.warning(f"Task {config_name} already completed, skipping")
                continue

            logger.info(f"===== Worker {worker_id} starting task {task_idx}: {config_name} =====")
            token = _current_task_idx.set(task_idx)
            try:
                task = OpenClawDistillationTask(config)
                await task.run(os.path.join(config.task_input_path, config_name), task_idx)
            finally:
                _current_task_idx.reset(token)
            logger.info(f"!!!!! Worker {worker_id} finished task {task_idx}: {config_name} !!!!!")
        except Exception as e:
            logger.error(f"Worker {worker_id} error on task {config_name}: {e}")
        finally:
            task_queue.task_done()


async def run_tasks(
    task_config: TaskConfig,
    task_start: int = 0,
    task_num: int = 10,
    concurrent_num: int = 10,
    run_failed: bool = False,
) -> None:
    """
    Run distillation tasks with specified concurrency.

    Args:
        task_config: Task configuration object.
        task_start: Starting index of tasks to run.
        task_num: Total number of tasks to run.
        concurrent_num: Number of concurrent workers.
        run_failed: If True, only run previously failed tasks.
    """
    config = task_config

    # Load completed records
    complete_file = os.path.join(config.task_output_path, config.task_complete_record)
    if os.path.exists(complete_file):
        with open(complete_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    config.run_output_complete_record.add(line)
    logger.info(f"Completed records: {config.run_output_complete_record}")

    # Load or discover task files
    failed_file = os.path.join(config.task_output_path, config.task_failed_record)
    if run_failed:
        if not os.path.exists(failed_file):
            logger.info(f"No failed record file found ({failed_file}), exiting")
            return
        with open(failed_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    config.run_input_config_files.add(line)
        os.remove(failed_file)
        logger.info(f"Loaded {len(config.run_input_config_files)} failed tasks")
    else:
        # Download config from OBS if needed
        if config.obs_config.user_config_download_path:
            download_path = os.path.join(
                config.task_download_path,
                os.path.basename(config.obs_config.user_config_download_path),
            )
            if not os.path.exists(download_path):
                obs_src = f"{config.obs_config.traj_save_bucket}/{config.obs_config.user_config_download_path}"
                obsutil_bin = config.obs_config.s3_download_script or "obsutil"
                cmd = [obsutil_bin, "cp", obs_src, config.task_download_path, "-r", "-f"]
                await asyncio.to_thread(run_cmd_stream, cmd, timeout=config.obs_config.download_timeout)
                if os.path.exists(config.task_input_path):
                    await asyncio.to_thread(shutil.rmtree, config.task_input_path)

                logger.info(f"copy user config: {config.task_input_path}")
                await asyncio.to_thread(
                    lambda: shutil.copytree(download_path, config.task_input_path, dirs_exist_ok=True)
                )

        # Scan input directory for config files
        with os.scandir(config.task_input_path) as entries:
            for entry in entries:
                if entry.is_file():
                    config.run_input_config_files.add(entry.name)

        if os.path.exists(failed_file):
            os.remove(failed_file)

    logger.info(
        f"Starting {task_num} tasks (offset={task_start}) with {concurrent_num} workers, "
        f"total available: {len(config.run_input_config_files)}"
    )

    # --- Per-task log splitting ---
    logs_dir = os.path.join(config.task_output_path, "logs")
    os.makedirs(logs_dir, exist_ok=True)

    fmt = logger.handlers[0].formatter if logger.handlers else None

    main_handler = logging.FileHandler(os.path.join(logs_dir, "main.log"), encoding='utf-8')
    main_handler.setFormatter(fmt)
    logger.addHandler(main_handler)

    task_handler = TaskFileHandler(logs_dir)
    task_handler.setFormatter(fmt)
    logger.addHandler(task_handler)

    # Trigger ManageLogger init FIRST so it configures level/stderr/filter,
    # then append our task_handler on top. If we getLogger() before ManageLogger
    # runs, it sees existing handlers and skips all configuration.
    ec_logger = ManageLogger("ExecutionClient").get_logger()
    ec_logger.addHandler(task_handler)
    make_logger = ManageLogger("execution_client.client.client").get_logger()
    make_logger.addHandler(task_handler)

    # Auto-pack source directory if main_code_dir is set
    if config.main_code_dir and os.path.isdir(config.main_code_dir):
        dir_basename = os.path.basename(config.main_code_dir.rstrip(os.sep))
        tar_path = os.path.join("uploads", f"{dir_basename}.tar")
        logger.info(f"Auto-packing source dir: {config.main_code_dir} -> {tar_path}")
        parent_dir = os.path.dirname(os.path.abspath(config.main_code_dir))
        await asyncio.to_thread(
            run_cmd_stream,
            ["tar", "-cf", tar_path, "-C", parent_dir, dir_basename],
            timeout=120,
        )
        config.main_code_tar = tar_path
        logger.info(f"Auto-pack complete: {tar_path}")

    # Build task queue
    task_queue = asyncio.Queue()
    sorted_configs = sorted(config.run_input_config_files)
    if task_num is None or task_num == 0:
        task_num = len(sorted_configs)
    for idx, name in enumerate(sorted_configs[task_start:task_start + task_num]):
        await task_queue.put((idx, name))

    # Run workers
    workers = [
        _worker(i, task_queue, config) for i in range(concurrent_num)
    ]
    if workers:
        await asyncio.gather(*workers)

    # Cleanup per-task handlers
    logger.removeHandler(main_handler)
    logger.removeHandler(task_handler)
    ec_logger.removeHandler(task_handler)
    make_logger.removeHandler(task_handler)
    main_handler.close()
    task_handler.close()

    logger.info("所有任务执行完毕！")


# ============================================================================
# Compatibility
# ============================================================================

_SANDBOX_RENAMES = {
    "openclaw_local_config_file": "harness_local_config_file",
    "agent_local_config_file": "harness_local_config_file",
    "agent_remote_config_file": "harness_sandbox_config_file",
    "ai_agent_dir": "harness_dir",
}

def _compat_sandbox(cfg) -> dict:
    """Map legacy sandbox field names to current names."""
    d = dict(cfg) if not isinstance(cfg, dict) else cfg.copy()
    for old, new in _SANDBOX_RENAMES.items():
        if old in d:
            d.setdefault(new, d.pop(old))
    return d


# ============================================================================
# Main Entry Point
# ============================================================================

def main() -> None:
    """Main entry point for the distillation script."""
    parser = argparse.ArgumentParser(description="OpenClaw Distillation Task Runner")
    parser.add_argument("--config", required=True, help="Configuration file path (YAML)")
    parser.add_argument("--failed", action="store_true", help="Run only previously failed tasks")
    args = parser.parse_args()

    config_obj = load_yaml_config(args.config)
    run_cfg = config_obj.run_config
    set_agent_framework(config_obj.remote_server.project_id)
    print(f"  Framework: {AGENT_FRAMEWORK}")

    # Isolate output/download paths by config name to avoid cross-contamination
    config_basename = Path(args.config).stem
    task_dict = OmegaConf.to_container(run_cfg.task, resolve=True)
    task_dict["task_output_path"] = os.path.join(task_dict.get("task_output_path", "outputs"), config_basename)
    task_dict["task_download_path"] = os.path.join(task_dict.get("task_download_path", "downloads"), config_basename)

    start_index = run_cfg.start_index
    total_num = run_cfg.total_num
    concurrent_num = run_cfg.concurrent_num

    print("=" * 60)
    print(f"  Config: {args.config}")
    print(f"  Failed only: {args.failed}")
    print(f"  Start index: {start_index}, Total: {total_num}, Concurrent: {concurrent_num}")
    print(f"  OBS user_config: {run_cfg.obs.user_config_download_path}")
    print(f"  OBS user_profile: {run_cfg.obs.user_profile_download_path}")
    print(f"  Output dir: {task_dict['task_output_path']}")
    print("=" * 60)

    task_config = TaskConfig(
        run_config_file=args.config,
        **task_dict,
        obs_config=ObsBucketConfig(**run_cfg.obs),
        sandbox_config=SandboxConfig(**_compat_sandbox(run_cfg.sandbox))
    )

    asyncio.run(run_tasks(
        task_config=task_config,
        task_start=start_index,
        task_num=total_num,
        concurrent_num=concurrent_num,
        run_failed=args.failed,
    ))


if __name__ == "__main__":
    main()

