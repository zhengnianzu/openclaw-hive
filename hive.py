"""
Agent Distillation Task Runner

A configuration-driven task orchestration system for running AI Agent tasks
in remote sandboxed environments (k8s/docker). 
- openclaw
- openclaw-tui
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
import re
import random
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Optional, List

import aiofiles
from omegaconf import OmegaConf, DictConfig

from execution_client.client.client import ExecutionClient, make
from execution_client.core.error_code import ErrorCode
from execution_client.core.logger import init_logger, get_logger
from execution_client.core.utils import generate_random_port #  get_obsutil_downloader_command, get_obsutil_uploader_command
from execution_client.models.request import EnvMakeRequest, ExtendExecCommand, ExtendUploadFile
from execution_client.models.response import Result

from env_sdk.compat.legacy_adapter import build_make_config   # 安装新版sdk, 新增导入该项
from prepare_config import (
    _FRAMEWORK_LAYOUTS,
    load_yaml_config,
    prepare_local_agent_config,
    adapt_layout_for_windows,
    HiveError,
    _log_sandbox_detail,
    _classify_task_stdout,
    ERROR_CATALOG,
)
from utils.win_util import (
    ps_command,
    _setup_windows_obsutil,
    _windows_env_assign,
    _start_openclaw_gateway_windows,
    _run_main_script_windows,
)
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

# 每框架的目录布局 — 数据源在 prepare_config._FRAMEWORK_LAYOUTS, 此处仅按当前框架
# 选取布局快照 (运行时可变, set_agent_framework 切换)。框架名不再做模块级全局,
# 由 main() 注入 SandboxConfig.harness_type (同 sandbox_type), 方法读
# self.config.sandbox_config.harness_type。
_FW: dict = _FRAMEWORK_LAYOUTS["openclaw"]
_JIUWENSWARM_TYPES = {"jiuwenswarm", "jiuwenswarm-dev", "jiuwenswarm-xiaoyi"}


def set_agent_framework(name: str, sandbox_type: str = "x86_cpu", home: str = "/home/ma-user") -> str:
    """Set the module-level framework layout switch. Called from ``main()`` exactly once.

    sandbox_type == "windows" 时先对布局做 adapt_layout_for_windows 转换。
    返回规范化后的框架名, 供 main() 注入 SandboxConfig.harness_type。
    """
    global _FW
    name = (name or "").strip().lower()
    if name not in _FRAMEWORK_LAYOUTS:
        raise RuntimeError(
            f"harness_type 必须是 {_FRAMEWORK_LAYOUTS.keys()}, got {name!r}"
        )
    layout = _FRAMEWORK_LAYOUTS[name]
    if sandbox_type == "windows":
        layout = adapt_layout_for_windows(layout, home)
    _FW = layout
    return name


def get_obsutil_downloader_command(s3_config, objects_storage_path, bucket_path):
    command = f"{s3_config.s3_download_script} cp {s3_config.bucket_name}/{bucket_path} {objects_storage_path} -r -f"
    return command


def get_obsutil_uploader_command(s3_config, local_folder_absolute_path, bucket_path):
    command = f"{s3_config.s3_download_script} cp {local_folder_absolute_path} {s3_config.bucket_name}/{bucket_path} -r -f"
    return command


# obsutil 把进度条和摘要写进 stderr（成功时也写）, 直接判定「stderr 非空即告警」
# 会在每次成功上传时刷一串 WARNING。这里只剔除这些已知噪音行。
_OBSUTIL_STDERR_NOISE = re.compile(
    r"^\[[#\-]+\]"                      # 进度条行: [----] / [####] 开头
    r"|\d+\.\d+%\s"                     # 任意进度百分比
    r"|\btps:\S*"                       # ... tps:0.00 ?/s
    r"|\bTotal Size\b"                  # 摘要: Total Size
    r"|\bUpload successfully\b"         # 摘要: Upload successfully
    r"|\bCost \[\d+\]"                  # 摘要: cost [41], status [200]
)


def _meaningful_stderr(stderr: str) -> str:
    """剔除 obsutil 进度条/摘要行, 只留真正的报错文本。"""
    return "\n".join(
        line for line in (stderr or "").splitlines()
        if line.strip() and not _OBSUTIL_STDERR_NOISE.search(line)
    )


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


class DropSandboxDetailFilter(logging.Filter):
    """
    过滤器: 丢掉带 record.sandbox_detail=True 的记录。
    应用在 main.log 的 handler 上——沙箱执行 stdout/stderr 详情只写 task-<idx>.log,
    不污染 main.log。
    """
    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, "sandbox_detail", False)


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

    def __post_init__(self):
        for field_name, field_value in self.__dataclass_fields__.items():
            value = getattr(self, field_name)
            if isinstance(value, str):
                setattr(self, field_name, re.sub(r'\s+', '', value))


@dataclass
class SandboxConfig:
    """Sandbox environment configuration."""
    sandbox_type: str = "x86_cpu"   # "x86_cpu"(Linux) | "windows" — 决定路径/命令形态
    harness_type: str = "openclaw"  # 框架名(openclaw/hermes/opencode/...), 注入自 run_cfg.harness_type
    home: str = "/home/ma-user"
    workspace: str = ""
    result_workdir: str = ""
    result_log: str = "run.log"
    data_config_path: str = ""
    harness_dir: str = field(default_factory=lambda: _FW["harness_dir"])
    default_skill_path: str = ""
    harness_sandbox_config_file: str = field(default_factory=lambda: _FW["harness_sandbox_config"])
    harness_local_config_file: str = field(default_factory=lambda: _FW["harness_local_config"])
    # openclaw用于启动gateway
    openclaw_bash: str = "/usr/local/node24/bin/openclaw"
    gateway_log: str = "gateway.log"
    openclaw_start_timeout: int = 10
    user_proxy_model_local_file: str = "uploads/user_proxy_model.json"
    user_proxy_model_remote_file: str = "configs/user_proxy_model.json"

    def __post_init__(self):
        # 路径按 sandbox_type 选 Pure*Path 派生; Linux 结果与旧类体默认值逐字节一致。
        cls = PureWindowsPath if self.sandbox_type == "windows" else PurePosixPath
        if not self.workspace:
            self.workspace = str(cls(self.home, "workspace"))
        if not self.result_workdir:
            self.result_workdir = str(cls(self.workspace, "workdir"))
        if not self.data_config_path:
            self.data_config_path = str(cls(self.workspace, "config"))
        if not self.default_skill_path:
            self.default_skill_path = str(cls(self.harness_dir, "skills"))
        if self.sandbox_type == "windows":
            self.openclaw_bash = "openclaw"


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
    suffix_api_keys: bool = False  # true: 给模型 key 追加沙箱标识后缀(自部署模型 user_id)
    enable_pip_install: bool = False  # true: 起主脚本前先 pip install -r requirements.txt
    model_api_key: str = ""  # 主 agent 原始 key(平台传入), suffix_api_keys 时在 hive 里打后缀
    harness_md: list = field(default_factory=list)  # 平台选定 harness 配置 md 的本地绝对路径列表
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


def check_bucket_path(bucket_path: str) -> str:
    if bucket_path and not bucket_path.endswith("/"):
        bucket_path += "/"
    return bucket_path


# ============================================================================
# Task Execution
# ============================================================================

class OpenClawDistillationTask:
    """Executes a single distillation task in a sandboxed environment."""

    # Windows 专属方法: 函数体在 utils/win_util.py, 这里挂成类属性让 self 自动绑定。
    _setup_windows_obsutil = _setup_windows_obsutil
    _windows_env_assign = _windows_env_assign
    _start_openclaw_gateway_windows = _start_openclaw_gateway_windows
    _run_main_script_windows = _run_main_script_windows

    def __init__(self, config: TaskConfig):
        self.logger = logger
        self.config = config
        self.complete_record_file: str = ""
        self.failed_record_file: str = ""

        yaml_config = load_yaml_config(self.config.run_config_file)
        self.global_config = yaml_config
        self.client_config = self._init_config(yaml_config)
        self.execution_client: Optional[ExecutionClient] = None

        # 每个任务一个沙箱(=一个 env_id), 取 uuid 作为 key 后缀。
        # env_id = f"{sandbox_id_prefix}-{uuid4().hex}"
        self.sandbox_suffix: str = self.client_config.env_make.env_id.rsplit("-", 1)[-1]
        # 主 agent 原始 key(平台传入), 保持原样; 后缀在 _prepare_local_configs 里做文件内容替换
        self.model_api_key: str = self.config.model_api_key or ""
        # 本地配置路径(共享模板)。每个任务复制成独立副本再改写上传, 副本路径由 _prepare_local_configs 生成。
        self.local_config_file: str = self.config.sandbox_config.harness_local_config_file
        self.local_model_config_file: str = self.config.sandbox_config.user_proxy_model_local_file


    def _init_config(self, yaml_config: DictConfig) -> DictConfig:
        """Initialize and return the client config with runtime values."""
        s3 = yaml_config.s3
        sandbox_id_prefix = yaml_config.sandbox_id_prefix
        config = copy.deepcopy(yaml_config)
        config = build_make_config(self.config.sandbox_config.sandbox_type, config=yaml_config)
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
            detail = f"{file_info}: {local_path} -> {remote_path}"
            self.logger.error(f"[C002] upload failed: {detail}")
            _log_sandbox_detail(self.logger, f"upload_file failed — {detail}",
                                result, level=logging.ERROR)
            raise HiveError("C002", detail=detail, sandbox_result=result)

    def _prepare_local_configs(self, data_config_file: Optional[str] = None, port: Optional[str] = None) -> None:
        """每个任务复制出两份独立副本(harness 配置 + user_proxy_model.json), 再在副本上
        打 key 后缀 / 写主 key / 写端口。副本按 env_id 隔离, 无需锁。
        """
        env_id = self.client_config.env_make.env_id
        local_dir = Path(self.config.task_output_path) / "sandbox_harness_configs" / env_id
        local_dir.mkdir(parents=True, exist_ok=True)
        self.local_config_file = str(local_dir / Path(self.config.sandbox_config.harness_local_config_file).name)
        self.local_model_config_file = str(local_dir / Path(self.config.sandbox_config.user_proxy_model_local_file).name)
        shutil.copy2(self.config.sandbox_config.harness_local_config_file, self.local_config_file)
        shutil.copy2(self.config.sandbox_config.user_proxy_model_local_file, self.local_model_config_file)

        # 1) user_proxy_model.json 读取再追加后缀(provider 注入前做, 保证注入的 provider 也带后缀)
        if bool(self.config.suffix_api_keys):
            model_cfg = json.loads(Path(self.local_model_config_file).read_text(encoding="utf-8"))
            for agent in model_cfg.values():
                if isinstance(agent, dict) and agent.get("api_key"):
                    agent["api_key"] = agent["api_key"] + f"_{self.sandbox_suffix}"
            Path(self.local_model_config_file).write_text(
                json.dumps(model_cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )

        # 2) provider 注入 + agent 预注册 + 路径本地化, 全部走 prepare_config
        #    (openclaw 分支内按 sandbox_type/home 派生, 无后处理)
        # oc_agent_names: OpenClaw 系 harness 专用, task config 里的 agent 名(host 已注册的
        # 交给 prepare_config 补进 agents.list), 其余 framework 传空。
        oc_agent_names = (
            self._derive_agent_names(data_config_file)
            if self.config.sandbox_config.harness_type in {"openclaw", "openclaw-tui"}
            and data_config_file else []
        )
        sandbox_cfg = self.config.sandbox_config
        prepare_local_agent_config(
            self.config.sandbox_config.harness_type,
            data_config_file,
            self.local_config_file,
            self.local_model_config_file,
            home=sandbox_cfg.home,
            sandbox_type=sandbox_cfg.sandbox_type,
            oc_agent_names=oc_agent_names,
        )

        # 3) 主 key 后缀: 用「原始 key + 后缀」直接替换文件里出现的原始 key, 不区分 harness
        if self.model_api_key and bool(self.config.suffix_api_keys):
            suffixed = self.model_api_key + f"_{self.sandbox_suffix}"
            content = Path(self.local_config_file).read_text(encoding="utf-8")
            content = content.replace(self.model_api_key, suffixed)
            Path(self.local_config_file).write_text(content, encoding="utf-8")

        # 4) 端口本地写进 openclaw.json 副本再上传
        if port is not None:
            data = json.loads(Path(self.local_config_file).read_text(encoding="utf-8"))
            if "gateway" in data:
                data["gateway"]["port"] = int(port)
            Path(self.local_config_file).write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        self.logger.info(
            f"local config copies: agent={self.local_config_file}\nmodel={self.local_model_config_file}"
        )

    async def _copy_agent_config(self, data_config_file, port: Optional[str] = None) -> None:
        """复制两份独立副本 → 改写 → 上传 agent config + user_proxy_model。

        每任务一份副本(env_id 隔离), 无需锁; port 不为 None(openclaw)时把端口写进副本。
        """
        await asyncio.to_thread(self._prepare_local_configs, data_config_file, port)
        remote_path = self.config.sandbox_config.harness_sandbox_config_file
        await self._upload_file("agent config", self.local_config_file, remote_path)
        await self._upload_user_proxy_model_config()
        self.logger.info(f"Copied agent config: {self.local_config_file} -> {remote_path}")

    async def _upload_and_extract_code(self, sandbox_type) -> None:
        """Upload and extract the main code tarball in sandbox."""
        tar_basename = os.path.basename(self.config.main_code_tar)
        workspace = self.config.sandbox_config.workspace
        if sandbox_type == "windows":
            remote_path = str(PureWindowsPath(workspace, tar_basename))
        else:
            remote_path = os.path.join(workspace, tar_basename)
        await self._upload_file("code tarball", self.config.main_code_tar, remote_path)

        # Extract code in sandbox
        command = f"cd {self.config.sandbox_config.workspace} && tar -xf {tar_basename}"
        if sandbox_type == "windows":
            command = ["cmd.exe", "/c", f"cd /d {workspace} && tar -xf {tar_basename}"]
        else:
            command = ["/bin/bash", "-c", f"cd {workspace} && tar -xf {tar_basename}"]
        exec_request = ExtendExecCommand(
            command=command,
            timeout=self.config.simple_bash_timeout,
        )
        result = await self.execution_client.extend(args=exec_request.to_dict())
        if result.code != ErrorCode.SUCCESS[0] or result.data["exit_code"] != 0:
            self.logger.error("[C001] extract code failed")
            _log_sandbox_detail(self.logger, "extract code failed",
                                result, level=logging.ERROR)
            raise HiveError("C001", detail=os.path.basename(tar_basename),
                            sandbox_result=result)
        self.logger.info("Code extracted successfully")

    async def _upload_data_config(self, config_file: str, port: Optional[str] = None) -> None:
        """集中改写 task config，保存独立副本后上传沙箱。

        所有运行时字段都在这里注入：
        - platform: sandbox_type == windows 时为 windows，否则为 linux；
        - harness_type: 当前 run_config.harness_type；
        - agents[].config: 仅给 queries[].agent_name 对应的 agent 插入
          harness_md 的 __default__ 路径；
        - gateway_ws_url: port 不为空时写入本次 OpenClaw gateway 端口。

        改写后的文件保存在 task_output_path/sandbox_task_configs/，
        不修改原始 task config。
        """
        sandbox_type = self.config.sandbox_config.sandbox_type
        if sandbox_type == "windows":
            remote_path = str(PureWindowsPath(
                self.config.sandbox_config.data_config_path, os.path.basename(config_file)
            ))
        else:
            remote_path = os.path.join(
                self.config.sandbox_config.data_config_path, os.path.basename(config_file)
            )

        data = json.loads(Path(config_file).read_text(encoding="utf-8"))
        data["platform"] = "windows" if sandbox_type == "windows" else "linux"
        data["harness_type"] = self.config.sandbox_config.harness_type

        harness_configs = [
            f"__default__/{os.path.basename(md_path)}"
            for md_path in (self.config.harness_md or [])
            if md_path and os.path.isfile(md_path)
        ]
        # 去重并保持 harness_md 的配置顺序。
        harness_configs = list(dict.fromkeys(harness_configs))
        query_agent_names = {
            str(query.get("agent_name") or "").strip()
            for query in (data.get("queries") or [])
            if isinstance(query, dict)
        }
        query_agent_names.discard("")
        if harness_configs:
            for agent in data.get("agents") or []:
                if not isinstance(agent, dict):
                    continue
                agent_name = str(agent.get("name") or "").strip()
                if agent_name not in query_agent_names:
                    continue
                agent_configs = agent.get("config") or []
                if not isinstance(agent_configs, list):
                    self.logger.warning(
                        f"agent config is not a list, replace it: {agent_name}"
                    )
                    agent_configs = []
                # 重试上传时也保证幂等：先移除旧的同路径项，再统一放到首部。
                agent["config"] = harness_configs + [
                    item for item in agent_configs if item not in harness_configs
                ]

        if port is not None:
            data["gateway_ws_url"] = f"ws://127.0.0.1:{port}/gateway"

        local_dir = Path(self.config.task_output_path) / "sandbox_task_configs"
        local_dir.mkdir(parents=True, exist_ok=True)
        local_path = local_dir / Path(config_file).name
        local_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        self.logger.info(
            f"Patched data config: platform={data['platform']}, "
            f"harness_type={data['harness_type']}, "
            f"harness_md={len(harness_configs)}, "
            f"query_agents={sorted(query_agent_names)}, port={port or '-'}"
        )
        self.logger.info(
            f"Data config paths: source={Path(config_file).resolve()}, "
            f"copy={local_path.resolve()}, remote={remote_path}"
        )
        await self._upload_file("data config", str(local_path), remote_path)
        self.logger.info(f"Uploaded data config: {local_path} -> {remote_path}")


    async def _upload_user_proxy_model_config(self) -> None:
        # 本地文件此时已被 _copy_agent_config 还原, 这里只负责上传(顺序上跟在它后面)
        local_path = self.local_model_config_file
        code_stem = Path(self.config.main_code_tar).stem
        sandbox_type = self.config.sandbox_config.sandbox_type
        if sandbox_type == "windows":
            remote_path = str(PureWindowsPath(
                self.config.sandbox_config.workspace,
                code_stem,
                self.config.sandbox_config.user_proxy_model_remote_file
            ))
        else:
            remote_path = os.path.join(
                self.config.sandbox_config.workspace,
                code_stem,
                self.config.sandbox_config.user_proxy_model_remote_file
            )
        await self._upload_file("user_proxy_model config", local_path, remote_path)
        self.logger.info(f"Copied user_proxy_model config: {local_path} -> {remote_path}")


    async def _start_openclaw_gateway(self, config_file: str) -> None:
        """Start the OpenClaw gateway in the sandbox."""
        start_time = asyncio.get_running_loop().time()
        max_wait = self.config.openclaw_gateway_timeout

        while True:
            elapsed = asyncio.get_running_loop().time() - start_time
            if elapsed > max_wait:
                self.logger.error(f"[S002] gateway startup timeout ({max_wait}s)")
                raise HiveError("S002", detail=f"{max_wait}s / config={os.path.basename(config_file)}")

            port = str(generate_random_port())
            remote_config_path = os.path.join(
                self.config.sandbox_config.data_config_path, os.path.basename(config_file)
            )

            # 每次尝试重新复制副本, 并把 gateway.port 本地写进 openclaw.json 副本再上传 
            await self._copy_agent_config(config_file, port=port)

            # data config 的 gateway_ws_url 写入正确端口再上传
            await self._upload_data_config(config_file, port=port)

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
                self.logger.error(f"[S001] gateway start failed on port {port}")
                _log_sandbox_detail(self.logger, f"gateway start failed — port {port}",
                                    result, level=logging.ERROR)
                raise HiveError("S001", detail=f"port={port}", sandbox_result=result)

            stdout = result.data.get("stdout", "")
            if f"Port {port} is already in use" in stdout:
                self.logger.warning(f"Port {port} in use, retrying...")
                await asyncio.sleep(2)
            elif "http server listening" in stdout:
                # 成功时也把 gateway 启动详情打进 task-<idx>.log (main.log 里过滤掉)
                _log_sandbox_detail(self.logger, f"gateway startup OK — port {port}", result)
                break
            else:
                self.logger.error(f"[S003] gateway unexpected output on port {port}")
                _log_sandbox_detail(self.logger, f"gateway unexpected output — port {port}",
                                    result, level=logging.ERROR)
                raise HiveError("S003", detail=f"port={port}", sandbox_result=result)

        self.logger.info(f"Gateway started on port {port}")


    async def _start_jiuwenswarm_gateway(self, config_file: str) -> None:
        """在沙箱中启动 JiuwenSwarm 网关 (gateway + agent server)。

        TUI 网关固定监听 19001 (客户端默认 JIUWENSWARM_GATEWAY_URL), 端口不随机。
        先上传 jw_config.yaml (含注入的模型) 到 ~/.jiuwenswarm/config/config.yaml,
        再 nohup 启动 jiuwenswarm-start app, 等其输出「服务已启动」或
        「Gateway server started」后返回。
        """
        # 上传 jw_config.yaml → ~/.jiuwenswarm/config/config.yaml (含注入的模型配置)
        await self._copy_agent_config(config_file)

        # jiuwenswarm 是 pip install --user 装的, console script 在 ~/.local/bin/,
        # 不在非交互 shell 的 PATH 里, 所以必须用全路径调用。
        jiuwenswarm_bin = os.path.join(
            self.config.sandbox_config.home, ".local", "bin", "jiuwenswarm-start"
        )
        poll_interval = 5
        max_polls = max(1, self.config.sandbox_config.openclaw_start_timeout // poll_interval)
        ready_marker = "__HIVE_JIUWENSWARM_READY__"

        start_time = asyncio.get_running_loop().time()
        max_wait = self.config.openclaw_gateway_timeout

        while True:
            elapsed = asyncio.get_running_loop().time() - start_time
            if elapsed > max_wait:
                self.logger.error(f"[S002] jiuwenswarm gateway startup timeout ({max_wait}s)")
                raise HiveError("S002", detail=f"{max_wait}s / config={os.path.basename(config_file)}")

            gateway_log_path = os.path.join(
                self.config.sandbox_config.result_workdir, self.config.sandbox_config.gateway_log
            )
            gateway_pid_path = os.path.join(
                self.config.sandbox_config.result_workdir, "jiuwenswarm-gateway.pid"
            )
            gateway_cmd = [
                "/bin/bash", "-c",
                f"mkdir -p {self.config.sandbox_config.result_workdir} && "
                # 独立进程组 + PID 文件，任务结束后可先停止整个 JiuwenSwarm 服务树。
                f"nohup setsid {jiuwenswarm_bin} > {gateway_log_path} 2>&1 & "
                f"echo $! > {gateway_pid_path}; "
                # 轮询就绪(每 5s grep 一次), 就绪即退出, 而非固定 sleep 180s
                f"ready=0; "
                f"for i in $(seq 1 {max_polls}); do sleep {poll_interval}; "
                f"if grep -qE 'Gateway server started|服务已启动' {gateway_log_path}; "
                f"then ready=1; break; fi; done; "
                # 哨兵必须先于日志输出, 就绪后只打印到标识行为止(不 tail 全量)。
                f"if [ \"$ready\" -eq 1 ]; then echo {ready_marker}; "
                f"awk '/Gateway server started|服务已启动/ {{print; exit}} {{print}}' {gateway_log_path}; "
                f"else echo __HIVE_JIUWENSWARM_NOT_READY__; "
                f"tail -n 200 {gateway_log_path}; exit 124; fi"
            ]
            exec_request = ExtendExecCommand(
                command=gateway_cmd,
                timeout=self.config.sandbox_config.openclaw_start_timeout + 5
            )
            result = await self.execution_client.extend(args=exec_request.to_dict())

            result_data = result.data or {}
            stdout = result_data.get("stdout", "") or ""
            exit_code = result_data.get("exit_code")

            if result.code == ErrorCode.SUCCESS[0] and exit_code == 124:
                self.logger.error(
                    f"[S002] jiuwenswarm gateway startup timeout "
                    f"({self.config.sandbox_config.openclaw_start_timeout}s)"
                )
                _log_sandbox_detail(self.logger, "jiuwenswarm gateway startup timeout",
                                    result, level=logging.ERROR)
                raise HiveError(
                    "S002",
                    detail=(f"{self.config.sandbox_config.openclaw_start_timeout}s / "
                            f"config={os.path.basename(config_file)}"),
                    sandbox_result=result,
                )

            if result.code != ErrorCode.SUCCESS[0] or exit_code != 0:
                self.logger.error("[S001] jiuwenswarm gateway start failed")
                _log_sandbox_detail(self.logger, "jiuwenswarm gateway start failed",
                                    result, level=logging.ERROR)
                raise HiveError("S001", detail=os.path.basename(config_file), sandbox_result=result)

            if ready_marker in stdout:
                _log_sandbox_detail(self.logger, "jiuwenswarm gateway startup OK", result)
                break

            self.logger.error("[S003] jiuwenswarm gateway unexpected output")
            _log_sandbox_detail(self.logger, "jiuwenswarm gateway unexpected output",
                                result, level=logging.ERROR)
            raise HiveError("S003", detail=os.path.basename(config_file), sandbox_result=result)

        self.logger.info("JiuwenSwarm gateway started (TUI on 19001)")


    async def _stop_jiuwenswarm_gateway(self) -> None:
        """停止本任务启动的 JiuwenSwarm 进程组，使会话文件稳定后再上传。"""
        result_workdir = self.config.sandbox_config.result_workdir
        pid_path = os.path.join(result_workdir, "jiuwenswarm-gateway.pid")

        # 第一阶段只发 TERM；等待放在 SDK 命令外，避免长时间占用一次 extend 调用。
        term_script = (
            f"pid_file='{pid_path}'; "
            "if [ -s \"$pid_file\" ]; then "
            "pid=$(cat \"$pid_file\"); "
            "if kill -0 -- \"-$pid\" 2>/dev/null; then "
            "kill -TERM -- \"-$pid\" 2>/dev/null || true; "
            "elif kill -0 \"$pid\" 2>/dev/null; then "
            "kill -TERM \"$pid\" 2>/dev/null || true; fi; "
            "echo HIVE_JIUWENSWARM_TERM_PID=$pid; "
            "else "
            # 兼容旧启动方式或 PID 文件意外丢失；沙箱内仅运行当前任务。
            "pkill -TERM -f '[j]iuwenswarm-start|[j]iuwenswarm.start_services' 2>/dev/null || true; "
            "echo HIVE_JIUWENSWARM_TERM_FALLBACK; fi"
        )
        term_result = await self._exec_sandbox(
            ["/bin/bash", "-c", term_script], self.config.simple_bash_timeout
        )
        _log_sandbox_detail(self.logger, "jiuwenswarm gateway stop TERM", term_result)

        await asyncio.sleep(5)

        # 第二阶段清理仍存活的进程组；正常退出时这里只删除 PID 文件。
        kill_script = (
            f"pid_file='{pid_path}'; forced=0; "
            "if [ -s \"$pid_file\" ]; then "
            "pid=$(cat \"$pid_file\"); "
            "if kill -0 -- \"-$pid\" 2>/dev/null; then "
            "kill -KILL -- \"-$pid\" 2>/dev/null || true; forced=1; "
            "elif kill -0 \"$pid\" 2>/dev/null; then "
            "kill -KILL \"$pid\" 2>/dev/null || true; forced=1; fi; fi; "
            "rm -f \"$pid_file\"; echo HIVE_JIUWENSWARM_STOPPED forced=$forced"
        )
        kill_result = await self._exec_sandbox(
            ["/bin/bash", "-c", kill_script], self.config.simple_bash_timeout
        )
        _log_sandbox_detail(self.logger, "jiuwenswarm gateway stopped", kill_result)
        kill_data = kill_result.data or {}
        if (kill_result.code != ErrorCode.SUCCESS[0]
                or kill_data.get("exit_code") not in (None, 0)):
            raise RuntimeError(
                f"stop jiuwenswarm gateway failed: "
                f"exit_code={kill_data.get('exit_code')} stderr={kill_data.get('stderr', '')}"
            )
        self.logger.info("JiuwenSwarm gateway stopped before OBS upload")


    async def _run_main_script(self, config_file: str, task_idx: int) -> None:
        """Execute the main Python script in the sandbox."""
        python_file = os.path.basename(self.config.main_python_file)
        config_path = os.path.join(
            self.config.sandbox_config.data_config_path, os.path.basename(config_file)
        )
        code_stem = Path(self.config.main_code_tar).stem
        log_path = self.config.sandbox_config.result_workdir

        # enable_pip_install=false 时跳过依赖安装(镜像里已预装, 可省 ~几十秒/任务)。
        pip_step = "pip install -r requirements.txt && " if self.config.enable_pip_install else ""
        command = (
            f"cd {self.config.sandbox_config.workspace} && "
            f"mkdir -p {log_path} && cd {code_stem} && "
            f"{pip_step}"
            f"python {python_file} --config {config_path} --harness {self.config.sandbox_config.harness_type} 2>&1 | "
            f"tee {log_path}/{self.config.sandbox_config.result_log}"
        )

        exec_request = ExtendExecCommand(
            command=["/bin/bash", "-c", command],
            timeout=self.config.main_python_timeout,
            # mode="stream",
        )
        result = await self.execution_client.extend(args=exec_request.to_dict())
        stdout = ((result.data or {}).get("stdout") or "") if (result and result.data) else ""
        sandbox_failed = (result.code != ErrorCode.SUCCESS[0]
                         or result.data["exit_code"] != 0)

        # 无论成功/失败, 沙箱执行详情都进 task-<idx>.log (main.log 已过滤掉)
        title = ("script execution failed" if sandbox_failed else "script execution OK")
        _log_sandbox_detail(self.logger, f"{title} — {python_file}", result,
                            level=(logging.ERROR if sandbox_failed else logging.INFO))

        # ---- 分类优先级: 具体 T-code > T010 (Traceback 兜底) > S008 (沙箱兜底) ----
        t_code, phrase = _classify_task_stdout(stdout)

        if t_code:
            self.logger.error(
                f"[{t_code}] task failure — {ERROR_CATALOG[t_code]}"
                + (f" — matched: {phrase!r}" if phrase else "")
            )
            raise HiveError(t_code, detail=phrase or python_file,
                            sandbox_result=result)

        if sandbox_failed:
            # 非零退出但 stdout 里没有可识别的业务失败线索 → 真·沙箱层故障
            self.logger.error(f"[S008] script execution failed: {python_file}")
            raise HiveError("S008", detail=python_file, sandbox_result=result)
        self.logger.info(f"Script completed: {python_file}")


    async def _download_s3_skills(self, data_config_file: str, sandbox_type: str) -> None:
        """Fetch task/default skills into the sandbox.

        skill_download_path 以 "/" 开头 → 视为沙箱本地路径, 否则视为 OBS bucket 前缀
        """
        data_cfg = parse_data_config(data_config_file)

        code_stem = Path(self.config.main_code_tar).stem
        if sandbox_type == "windows":
            project_dir = str(PureWindowsPath(self.config.sandbox_config.workspace, code_stem))
        else:
            project_dir = os.path.join(self.config.sandbox_config.workspace, code_stem)
        skill_src = self.config.obs_config.skill_download_path
        skill_dir = (data_cfg.input_dir or {}).get("skill_dir", "skills")
        if not isinstance(skill_dir, str):
            skill_dir = "skills"
        # task_skills → workspace/<project>/<skill_dir>; default_skills → default_skill_path
        if sandbox_type == "windows":
            task_target_path = str(PureWindowsPath(project_dir, skill_dir))
        else:
            task_target_path = os.path.join(project_dir, skill_dir)
        default_target_path = self.config.sandbox_config.default_skill_path

        # 按需选取: task config 指定的 task_skills + 配置的 default_skills
        task_skills = [skill for agent in data_cfg.agents if agent.get("skills") for skill in agent["skills"]]
        default_skills = self.config.obs_config.default_skills or []
        if not task_skills and not default_skills:
            self.logger.warning("No skills found, skipping download")
            return

        # skill_src 以 "/" 开头 → 沙箱本地路径, 逐个 cp; 否则 → OBS 前缀, 逐个 obsutil
        if not skill_src:
            self.logger.warning("skill_download_path is empty, skipping skill download")
            return
        is_local = skill_src.startswith("/")

        async def download_skill(skill_path: str, target_path: str) -> None:
            if is_local:
                # 只 cp 需要的单个 skill。目标目录本身已是新沙箱, 且 openclaw-task
                # 里一般没有 skills 目录, 不需要先删 —— 两个 workspace 并发复制同一个
                # skill 时, 多余的 rm -rf 会让另一条链撞上 "Directory not empty" 而失败。
                src = os.path.join(skill_src.rstrip("/"), skill_path)
                inner = (f"mkdir -p {target_path} && cp -r {src} {target_path}/")
            else:
                # windows 只支持obs桶下载的方式
                bucket_path = os.path.join(skill_src, skill_path) + "/"
                obs_cmd = get_obsutil_downloader_command(
                    self.client_config.s3,
                    objects_storage_path=target_path,
                    bucket_path=bucket_path,
                )
                if sandbox_type == "windows":
                    inner = f"mkdir {target_path} & {obs_cmd}"
                else:
                    inner = f"mkdir -p {target_path} && {obs_cmd}"
            await self._run_skill_cmd(inner, detail=skill_path)

        start_time = time.time()
        tasks = [download_skill(s, task_target_path) for s in set(task_skills)]
        tasks += [download_skill(s, default_target_path) for s in set(default_skills)]
        await asyncio.gather(*tasks)
        mode = "local-cp" if is_local else "obsutil"
        self.logger.info(
            f"Downloaded skills ({mode}: task={len(set(task_skills))}, "
            f"default={len(set(default_skills))}) in {time.time() - start_time:.1f}s"
        )

    async def _run_skill_cmd(self, bash: str, *, detail: str) -> None:
        """在沙箱里执行 skill 相关命令, 统一 [S005] 错误处理。"""
        exec_request = ExtendExecCommand(
            command=["/bin/bash", "-c", bash],
            timeout=self.config.obs_config.download_timeout,
            mode="stream",
        )
        result = await self.execution_client.extend(args=exec_request.to_dict())
        if result.code != ErrorCode.SUCCESS[0] or result.data["exit_code"] != 0:
            self.logger.error(f"[S005] skill fetch failed: {detail}")
            _log_sandbox_detail(self.logger, f"skill fetch failed — {detail}",
                                result, level=logging.ERROR)
            raise HiveError("S005", detail=detail, sandbox_result=result)

    async def _download_s3_user_profile(self, data_config_file: str, sandbox_type: str) -> None:
        """Download user profile from S3 to sandbox."""
        if not self.config.obs_config.user_profile_download_path:
            logger.warning("no user profile path found")
            return

        data_cfg = parse_data_config(data_config_file)
        user_profile_path = (data_cfg.input_dir or {}).get("user_dir", {})
        if isinstance(user_profile_path, dict):
            user_profile_path = user_profile_path.get("path", "")
        if not user_profile_path:
            user_profile_path = ""

        if not user_profile_path:
            self.logger.warning("No user profile path found in task config, skipping")
            return

        code_stem = Path(self.config.main_code_tar).stem
        if sandbox_type == "windows":
            objects_storage_path = str(PureWindowsPath(self.config.sandbox_config.workspace, code_stem))
        else:
            objects_storage_path = os.path.join(
                self.config.sandbox_config.workspace,
                code_stem
            )
        user_dir = os.path.dirname(user_profile_path)
        if user_dir:
            if sandbox_type == "windows":
                objects_storage_path = str(PureWindowsPath(objects_storage_path, user_dir))
            else:
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
        if sandbox_type == "windows":
            exec_command = ["cmd.exe", "/c", f"mkdir {objects_storage_path} & {command}"]
        else:
            exec_command = ["/bin/bash", "-c", f"mkdir -p {objects_storage_path} && {command}"]
        exec_request = ExtendExecCommand(
            command=exec_command,
            timeout=self.config.obs_config.download_timeout,
            mode="stream",
        )
        start_time = time.time()
        result = await self.execution_client.extend(args=exec_request.to_dict())
        if result.code != ErrorCode.SUCCESS[0] or result.data["exit_code"] != 0:
            self.logger.error(f"[S006] user profile download failed: {user_profile_path}")
            _log_sandbox_detail(self.logger, "user profile download failed",
                                result, level=logging.ERROR)
            raise HiveError("S006", detail=user_profile_path, sandbox_result=result)
        self.logger.info(f"Downloaded user profile in {time.time() - start_time:.1f}s")

    async def _download_s3_agents(self, sandbox_type) -> None:
        """Download agent configs from S3 to sandbox."""
        bucket_path = self.config.obs_config.agents_download_path
        if not bucket_path:
            logger.warning("No agent bucket path found")
            return

        bucket_path = check_bucket_path(bucket_path)

        code_stem = Path(self.config.main_code_tar).stem
        if sandbox_type == "windows":
            objects_storage_path = str(PureWindowsPath(self.config.sandbox_config.workspace, code_stem))
        else:
            objects_storage_path = os.path.join(self.config.sandbox_config.workspace, code_stem)

        command = get_obsutil_downloader_command(
            self.client_config.s3,
            objects_storage_path=objects_storage_path,
            bucket_path=bucket_path,
        )
        if sandbox_type == "windows":
            exec_command = ["cmd.exe", "/c", f"mkdir {objects_storage_path} & {command}"]
        else:
            exec_command = ["/bin/bash", "-c", f"mkdir -p {objects_storage_path} && {command}"]
        exec_request = ExtendExecCommand(
            command=exec_command,
            timeout=self.config.obs_config.download_timeout,
            mode="stream",
        )
        start_time = time.time()
        result = await self.execution_client.extend(args=exec_request.to_dict())
        if result.code != ErrorCode.SUCCESS[0] or result.data["exit_code"] != 0:
            self.logger.error(f"[S007] agents download failed: {bucket_path}")
            _log_sandbox_detail(self.logger, "agents download failed",
                                result, level=logging.ERROR)
            raise HiveError("S007", detail=bucket_path, sandbox_result=result)
        self.logger.info(f"Downloaded agents in {time.time() - start_time:.1f}s")

    async def _upload_harness_md(self, config_file: str, sandbox_type: str) -> None:
        """harness_md 非空时, 先把其 md 上传到沙箱 agent 源目录作兜底。

        run_config.harness_md 是平台选定的 harness 配置 md(AGENTS.md/SOUL.md/USER.md...)
        的本地绝对路径。逐个上传到
        workspace/<code_stem>/<agent_dir>/__default__/<basename>;
        在 _download_s3_agents 之前执行, OBS agents 下载若有同名文件则覆盖之,
        之后各 harness 的 _copy_agent_configs 再把它拷进 agent workspace。空则完全跳过。
        """
        harness_md = self.config.harness_md or []
        if not harness_md:
            return

        data_cfg = parse_data_config(config_file)
        agent_dir = (data_cfg.input_dir or {}).get("agent_dir") or ""
        if not agent_dir:
            self.logger.warning(
                "harness_md set but task config has no input_dir.agent_dir, skip"
            )
            return

        code_stem = Path(self.config.main_code_tar).stem
        if sandbox_type == "windows":
            target_dir = str(PureWindowsPath(
                self.config.sandbox_config.workspace, code_stem, agent_dir, "__default__"
            ))
        else:
            target_dir = os.path.join(
                self.config.sandbox_config.workspace, code_stem, agent_dir, "__default__"
            )

        uploaded = 0
        for md_path in harness_md:
            if not md_path or not os.path.isfile(md_path):
                self.logger.warning(f"harness_md file missing, skip: {md_path}")
                continue
            name = os.path.basename(md_path)
            if sandbox_type == "windows":
                remote_path = str(PureWindowsPath(target_dir, name))
            else:
                remote_path = os.path.join(target_dir, name)
            await self._upload_file("harness md", md_path, remote_path)
            uploaded += 1
        self.logger.info(
            f"Uploaded harness md ({uploaded}/{len(harness_md)}) -> {target_dir}"
        )

    def _derive_agent_names(self, config_file: str) -> list[str]:
        """从 task config 的 agents[].name 提取去重后的 agent 名列表。"""
        data_cfg = parse_data_config(config_file)
        return list({
            agent.get("name", "").strip()
            for agent in data_cfg.agents
            if isinstance(agent, dict) and agent.get("name")
        })

    def _derive_per_agent_workspaces(self, config_file: str) -> list[str]:
        """按 task config 的 agents[].name 派生 per-agent workspace 绝对路径。

        按 sandbox_type 选 Pure*Path: hive 跑在 Linux 主机上, 用 Path("C:\\...")
        会把整串当成一个相对文件名, 结果既不能拼出沙箱路径, 也会与
        prepare_config.agent_workspace() 的派生不一致。
        """
        ws_base = _FW.get("workspace_base")
        if not ws_base:
            return []
        cls = PureWindowsPath if self.config.sandbox_config.sandbox_type == "windows" else PurePosixPath
        base = cls(ws_base)
        return [
            str(base if n == "main" else base.parent / f"{base.name}-{n}")
            for n in self._derive_agent_names(config_file)
        ]

    async def _exec_sandbox(self, exec_command, timeout: int):
        """一次性 exec, 返回 Result。上传/清理逐条调用, 便于拿到各自的退出码。"""
        exec_request = ExtendExecCommand(command=exec_command, timeout=timeout)
        return await self.execution_client.extend(args=exec_request.to_dict())

    async def _upload_one(
        self, src: str, dst: str, sandbox_type: str, config_dir: str
    ) -> str:
        """上传单个源目录, 返回 'ok' | 'missing' | 'fail:<rc>'。

        每个源单独一次 exec。旧实现把 purge 和所有源串成一条 cmd `&` 链, 而
        `&` 的退出码只等于最后一条 —— 前序全失败也报 OK, 于是 1016 / 1016v1
        两轮 7 个任务只有 1 个真正传上桶, 其余 6 个前缀根本不存在。
        """
        s3 = self.client_config.s3
        exe = s3.s3_download_script or "obsutil"
        remote = f"{s3.bucket_name}/{dst}"
        if sandbox_type == "windows":
            # 显式区分「源不存在」与「上传失败」: 旧写法 if exist 让两者等价。
            script = (
                f"if (Test-Path '{src}') {{ "
                f"& '{exe}' cp '{src}' '{remote}' -r -f; "
                f"Write-Output ('HIVE_RC=' + $LASTEXITCODE) "
                f"}} else {{ Write-Output 'HIVE_MISSING' }}"
            )
            exec_command = ps_command(script, config_dir)
        else:
            script = (
                f"if [ -e '{src}' ]; then {exe} cp '{src}' '{remote}' -r -f; "
                f"echo HIVE_RC=$?; else echo HIVE_MISSING; fi"
            )
            exec_command = ["/bin/bash", "-c", script]

        result = await self._exec_sandbox(
            exec_command, self.config.obs_config.upload_timeout
        )
        result_data = result.data or {}
        stdout = result_data.get("stdout", "") or ""
        stderr = result_data.get("stderr", "") or ""
        if "HIVE_MISSING" in stdout:
            return "missing"
        m = re.search(r"HIVE_RC=(-?\d+)", stdout)
        rc = int(m.group(1)) if m else None
        if rc == 0 and result.code == ErrorCode.SUCCESS[0]:
            noise = _meaningful_stderr(stderr)
            if noise:
                self.logger.warning(
                    f"[upload_traj] obsutil stderr {src} -> {remote}:\n{noise}"
                )
            return "ok"
        effective_rc = rc if rc is not None else result_data.get("exit_code")
        self.logger.error(
            f"[upload_traj] obsutil command failed {src} -> {remote} "
            f"rc={effective_rc} sdk_code={result.code} "
            f"exit_code={result_data.get('exit_code')}\n"
            f"stdout(last 8000 chars):\n{stdout[-8000:]}\n"
            f"stderr:\n{stderr}"
        )
        return f"fail:{effective_rc}"

    async def _upload_traj_to_obs(self, config_file: str) -> bool:
        """Upload execution traj (logs) to OBS.
        兜底调用: 无论 pipeline 走到哪一步, 只要 execution_client 存活就应该被调用一次。
        """
        if self.execution_client is None:
            self.logger.warning("[upload_traj] execution_client is None, skip")
            return False

        sandbox_type = self.config.sandbox_config.sandbox_type
        sandbox_cfg = self.config.sandbox_config
        # 沙箱路径一律用 Pure*Path 拼: hive 跑在 Linux 主机上,
        PathCls = PureWindowsPath if sandbox_type == "windows" else PurePosixPath
        bucket_path = os.path.join(
            self.config.obs_config.traj_save_path, Path(config_file).stem,
        )
        code_stem = Path(self.config.main_code_tar).stem
        task_logs = str(PathCls(sandbox_cfg.workspace, code_stem, "logs"))
        per_agent_workspaces = self._derive_per_agent_workspaces(config_file)
        # 按 _FRAMEWORK_LAYOUTS 中定义的 skill_subdir 清理旧 skills（各 harness 的 skills 目录不同）
        skill_subdir = _FW.get("skill_subdir", "skills")
        # 清理目标 (每条单独执行, 失败只告警 —— 清理不成功不该让整个 traj 上传失败)
        if sandbox_type == "windows":
            purge_targets = [
                str(PathCls(ws, d))
                for ws in per_agent_workspaces
                for d in ([skill_subdir, ".opencode"] if self.config.sandbox_config.harness_type == "opencode" else [skill_subdir])
            ]
        else:
            purge_targets = [
                str(PurePosixPath(ws, d))
                for ws in per_agent_workspaces
                for d in ([skill_subdir, ".opencode"] if self.config.sandbox_config.harness_type == "opencode" else [skill_subdir])
            ]

        # 固定路径直接落到 bucket_path 下（workdir / logs / per-agent workspace）。
        uploads = [
            (sandbox_cfg.result_workdir, bucket_path),
            (task_logs, bucket_path),
        ]
        uploads += [(ws, bucket_path) for ws in per_agent_workspaces]

        if self.config.sandbox_config.harness_type == "hermes":
            # hermes 每个 profile(<agent-name>) 下都有同名 sessions/logs/workspace，保留 profiles/<name>/ 层
            for pattern in _FW["upload_paths"]:
                for n in self._derive_agent_names(config_file):
                    src = pattern.replace("*", n)
                    dst = os.path.join(bucket_path, "profiles", n)
                    uploads.append((src, dst))
        else:
            uploads += [(p, bucket_path) for p in _FW["upload_paths"]]

        config_dir = str(PureWindowsPath(
            sandbox_cfg.harness_sandbox_config_file).parent).replace("\\", "/")

        # ---- 清理旧 skills: 逐条执行, 失败只告警 ----
        for target in purge_targets:
            if sandbox_type == "windows":
                # cmd.exe 不认单引号: `if exist 'x'` 会把引号当字面字符, 永远判不中
                purge_cmd = f'if exist "{target}" rmdir /s /q "{target}"'
                exec_command = ["cmd.exe", "/c", purge_cmd]
            else:
                purge_cmd = f"rm -rf '{target}'"
                exec_command = ["/bin/bash", "-c", purge_cmd]
            try:
                await self._exec_sandbox(exec_command, self.config.simple_bash_timeout)
            except Exception as e:
                self.logger.warning(f"[upload_traj] purge failed {target}: {e}")

        # ---- 上传: 每个源单独 exec, 各自判定 ok / missing / fail ----
        # S009 多为 OBS/网络瞬时抖动, 失败源最多重试 3 次(共 4 次尝试)。
        # 重试间加 random(0~60s) 时延, 让并发任务的重试错开, 均分 obsutil 流量。
        upload_retries = 3
        uploaded_srcs: list[str] = []
        missing_srcs: list[str] = []
        failures: list[str] = []
        pending = list(uploads)
        for attempt in range(1, upload_retries + 2):  # 第 1 次为初试, 之后为重试
            next_pending = []
            attempt_failures: list[str] = []
            for src, dst in pending:
                try:
                    outcome = await self._upload_one(src, dst, sandbox_type, config_dir)
                except Exception as e:
                    attempt_failures.append(f"{src} -> {dst} ({type(e).__name__}: {e})")
                    next_pending.append((src, dst))
                    self.logger.error(
                        f"[upload_traj] FAILED {src} -> {dst} {type(e).__name__}: {e}"
                    )
                    continue
                if outcome == "ok":
                    uploaded_srcs.append(src)
                elif outcome == "missing":
                    # 显式记录缺失, 不再让 if exist 把「没有」伪装成「成功」
                    missing_srcs.append(src)
                    self.logger.warning(f"[upload_traj] skip (src missing): {src}")
                else:
                    attempt_failures.append(f"{src} -> {dst} ({outcome})")
                    next_pending.append((src, dst))
                    self.logger.error(f"[upload_traj] FAILED {src} -> {dst} {outcome}")
            failures = attempt_failures
            if not next_pending:
                break
            if attempt <= upload_retries:
                delay = random.uniform(0, 60)  # 秒, 随机错开重试, 均分 obsutil 流量
                self.logger.warning(
                    f"[upload_traj] retry {attempt}/{upload_retries}: "
                    f"{len(next_pending)} source(s) still failing, backoff {delay:.1f}s"
                )
                await asyncio.sleep(delay)
            pending = next_pending

        self.logger.info(
            f"[upload_traj] bucket={self.client_config.s3.bucket_name}/{bucket_path} "
            f"uploaded={len(uploaded_srcs)} "
            f"missing={len(missing_srcs)} failed={len(failures)}"
        )
        if failures:
            raise HiveError("S009", detail=f"bucket={bucket_path} " + "; ".join(failures))
        if not uploaded_srcs:
            # 一个源都没传上去: 不能算成功
            self.logger.error(f"[upload_traj] FAILED bucket={bucket_path} no source existed")
            return False
        return True


    async def _execute_task(self, config_file: str, task_idx: int) -> None:
        """Execute the full task pipeline.

        各模式主要差异:
          openclaw: 通过 _start_openclaw_gateway 启动 node gateway 进程
                    (内部会先 _copy_agent_config 把 openclaw.json 上传)
          openclaw-tui: 只上传 openclaw.json，由 openclaw-task 在进程内通过
                        ``agent --local --json`` 启动每轮 CLI，不启动 Gateway
          hermes:   只 _copy_agent_config 把 ~/.hermes/config.yaml 上传
                    (没有 gateway 进程, AIAgent 进程内调用)
        其他步骤变量复用, 路径靠 config.yaml 覆写。
        """
        sandbox_type = self.config.sandbox_config.sandbox_type
        if sandbox_type == "windows":
            await self._setup_windows_obsutil()
        await self._upload_and_extract_code(sandbox_type)
        await self._download_s3_skills(config_file, sandbox_type)
        await self._download_s3_user_profile(config_file, sandbox_type)
        await self._upload_harness_md(config_file, sandbox_type)
        await self._download_s3_agents(sandbox_type)
        await self._upload_data_config(config_file)
        harness_type = self.config.sandbox_config.harness_type.strip().lower()
        stop_jiuwenswarm = harness_type in _JIUWENSWARM_TYPES
        try:
            if harness_type == "openclaw":
                if sandbox_type == "windows":
                    await self._start_openclaw_gateway_windows(config_file)
                else:
                    await self._start_openclaw_gateway(config_file)
            elif stop_jiuwenswarm:
                # 先启动 jiuwenswarm 网关, 再跑主程序 (内部会先上传 jw_config.yaml)
                await self._start_jiuwenswarm_gateway(config_file)
            else:
                # 就地改写 → 上传 agent config + user_proxy_model → 还原
                await self._copy_agent_config(config_file)
            if sandbox_type == "windows":
                await self._run_main_script_windows(config_file, task_idx)
            else:
                await self._run_main_script(config_file, task_idx)
        finally:
            if stop_jiuwenswarm:
                try:
                    await self._stop_jiuwenswarm_gateway()
                except Exception as e:
                    # 停止失败不覆盖主任务的原始异常；OBS 上传仍会执行并记录结果。
                    self.logger.error(
                        f"Failed to stop JiuwenSwarm gateway before OBS upload: "
                        f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
                    )

    async def run(self, config_file: str, task_idx: int = 0) -> None:
        """Run a single distillation task.

        Returns a dict {status, error_code, config, elapsed}
        for the orchestrator to aggregate per-run stats.
        """
        await asyncio.sleep(random.uniform(1, 10))
        start_time = time.perf_counter()
        # 三态: 任务成功 / 任务失败 (跑完了但 upload 失败) / 任务异常
        status: str = "任务失败"     # 默认最悲观
        error_msg: str = ""
        error_code: str = ""        # 具体错误码 (C001 / S001 / T001 / X999)
        env_ready: bool = False
        self.logger.info(
            f"===== BEGIN config={os.path.basename(config_file)} "
            f"framework={self.config.sandbox_config.harness_type} ====="
        )

        try:
            init_logger(self.global_config["global"]["logger"])
            request = EnvMakeRequest(**OmegaConf.to_container(self.client_config.env_make, resolve=True))
            result = await make(
                request, config=self.client_config
            )
            if isinstance(result, Result):
                self.logger.error(f"[C003] environment creation failed: {getattr(result, 'msg', '')}")
                _log_sandbox_detail(self.logger, "environment creation failed",
                                    result, level=logging.ERROR)
                raise HiveError("C003",
                                detail=getattr(result, "msg", "") or "",
                                sandbox_result=result)
            self.execution_client = result
            env_ready = True
            self.logger.info(
                f">>>>>> Task {task_idx}: env_id={self.execution_client.get_env_id()} "
                f"elapsed={time.perf_counter() - start_time:.1f}s <<<<<<"
            )
            await self._execute_task(config_file, task_idx)
            status = "任务成功"

        except HiveError as e:
            # 已分类的 pipeline 异常 (C/S/T)
            status = "任务异常"
            error_code = e.code
            error_msg = e.short + (f": {e.detail}" if e.detail else "")
            self.logger.error(
                f"[{e.code}] Task {task_idx} failed: {e}\n{traceback.format_exc()}"
            )

        except Exception as e:
            # 未分类的意外异常
            status = "任务异常"
            error_code = "X999"
            error_msg = f"{type(e).__name__}: {e}"
            self.logger.error(f"[X999] Task {task_idx} failed: {traceback.format_exc()}")
        finally:
            # ---- 兜底: 无论前面走到哪一步, 只要 env 起来了, 一律尝试 upload_traj ----
            uploaded: bool = False
            if env_ready:
                try:
                    self.logger.info("finalize=BEGIN name=upload_traj_to_obs")
                    uploaded = await self._upload_traj_to_obs(config_file)
                    self.logger.info(
                        f"finalize=END   name=upload_traj_to_obs "
                        f"result={'ok' if uploaded else 'FAIL'}"
                    )
                except Exception as e:
                    self.logger.error(
                        f"upload_traj_to_obs raised despite internal try: "
                        f"{e}\n{traceback.format_exc()}"
                    )
                    uploaded = False
            else:
                self.logger.warning("env never became ready, skip upload_traj_to_obs")

            # 若 pipeline 成功但 upload 失败, 状态降级为"失败"
            if status == "任务成功" and not uploaded:
                status = "任务异常"
                if not error_code:
                    error_code = "S009"
                    error_msg = ERROR_CATALOG["S009"]

            # ---- 记录 complete/failed ----
            try:
                if status == "任务成功":
                    await self._save_record(self.complete_record_file, config_file)
                else:
                    await self._save_record(self.failed_record_file, config_file)
            except Exception as e:
                self.logger.error(f"save_record failed: {e}\n{traceback.format_exc()}")

            # ---- 关闭 execution_client ----
            if self.execution_client is not None and isinstance(self.execution_client, ExecutionClient):
                try:
                    await self.execution_client.close()
                except Exception as e:
                    self.logger.error(
                        f"execution_client.close() failed: {e}\n{traceback.format_exc()}"
                    )

            # ---- task-<idx>.log 结尾: 打印任务执行状态 ----
            elapsed = time.perf_counter() - start_time
            self.logger.info(
                f"===== 任务执行状态={status} "
                f"error_code={error_code or '-'} "
                f"upload_traj={'ok' if uploaded else 'FAIL'} "
                f"env_ready={env_ready} "
                f"elapsed={elapsed:.1f}s "
                f"config={os.path.basename(config_file)}"
                + (f' error="{error_msg}"' if error_msg else "")
                + " ====="
            )


# ============================================================================
# Task Orchestration
# ============================================================================

async def _upload_records_to_obs(config: TaskConfig) -> None:
    """把本次运行的 complete.jsonl / failed.jsonl 上传到 OBS traj_save_path 下。"""
    traj_path = config.obs_config.traj_save_path
    obsutil_bin = config.obs_config.s3_download_script or "obsutil"
    dest = check_bucket_path(f"{config.obs_config.traj_save_bucket}/{traj_path}")

    for record_name in (config.task_complete_record, config.task_failed_record):
        local_path = os.path.join(config.task_output_path, record_name)
        if not os.path.exists(local_path):
            logger.info(f"[upload_records] {local_path} not found, skip")
            continue
        cmd = [obsutil_bin, "cp", local_path, dest, "-f"]
        try:
            rc = await asyncio.to_thread(
                run_cmd_stream, cmd, timeout=config.obs_config.upload_timeout
            )
            if rc == 0:
                logger.info(f"[upload_records] uploaded {local_path} -> {dest}")
            else:
                logger.error(f"[upload_records] upload failed rc={rc}: {local_path}")
        except Exception as e:
            logger.error(
                f"[upload_records] exception uploading {local_path}: "
                f"{e}\n{traceback.format_exc()}"
            )


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
    main_handler.addFilter(DropSandboxDetailFilter())  # sandbox 详情不进 main.log
    logger.addHandler(main_handler)

    task_handler = TaskFileHandler(logs_dir)
    task_handler.setFormatter(fmt)
    logger.addHandler(task_handler)

    # 控制台 handler (StreamHandler) 也过滤掉沙箱详情, 避免 stderr 刷屏
    for h in logger.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.addFilter(DropSandboxDetailFilter())

    # Trigger ManageLogger init FIRST so it configures level/stderr/filter,
    # then append our task_handler on top. If we getLogger() before ManageLogger
    # runs, it sees existing handlers and skips all configuration.
    ec_logger = get_logger()
    ec_logger.addHandler(task_handler)
    make_logger = get_logger()
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

    # Upload this run's complete/failed records to OBS
    await _upload_records_to_obs(config)

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
    parser.add_argument("--model-api-key", default="", help="Model API key passed from the platform")
    args = parser.parse_args()

    config_obj = load_yaml_config(args.config)
    run_cfg = config_obj.run_config
    sandbox_keys = list(config_obj.sandbox.keys())
    sandbox_type = sandbox_keys[0] if sandbox_keys else "x86_cpu"
    sandbox_home = run_cfg.sandbox.get("home") or "/home/ma-user"
    harness_type = set_agent_framework(run_cfg.harness_type, sandbox_type, sandbox_home)
    print(f"  Framework: {harness_type}")

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
        sandbox_config=SandboxConfig(
            **_compat_sandbox(run_cfg.sandbox),
            sandbox_type=sandbox_type,
            harness_type=harness_type,
        ),
        model_api_key=args.model_api_key,
        harness_md=list(run_cfg.get("harness_md") or []),
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
