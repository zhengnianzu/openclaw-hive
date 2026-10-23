"""Windows 沙箱 PowerShell -EncodedCommand 命令构造 + Windows 专属执行流程。

参考 win_demo.py 的 ps(): 固定前缀设置 $ErrorActionPreference 和
OPENCLAW_CONFIG_PATH / OPENCLAW_STATE_DIR / OPENCLAW_GATEWAY_TOKEN, 再拼调用方
脚本, 整体 base64(utf-16le) 编码后走 powershell -EncodedCommand。

本模块集中 Windows 沙箱相关逻辑(纯命令构造 + 以 task 实例为第一参的流程函数)。
依赖注入: 框架名从 self.config.sandbox_config.harness_type 取(同 sandbox_type 一样
由 hive.py 构造 SandboxConfig 时注入), 不读模块级全局; HiveError / _log_sandbox_detail
/ _classify_task_stdout / ERROR_CATALOG 从 prepare_config 导入(不 import hive, 避免循环)。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import tarfile
import tempfile
import time
from pathlib import Path, PureWindowsPath

from prepare_config import HiveError, _log_sandbox_detail, _classify_task_stdout, ERROR_CATALOG

from execution_client.core.error_code import ErrorCode
from execution_client.core.utils import generate_random_port
from execution_client.models.request import ExtendExecCommand


def ps_command(script: str, remote: str) -> list[str]:
    """把一段 PowerShell 脚本包成 -EncodedCommand 的 exec 命令列表。

    remote: 沙箱内放置 openclaw.json 的目录(前向斜杠)。前缀会把
    $env:OPENCLAW_CONFIG_PATH / OPENCLAW_STATE_DIR 指到该目录, 并从
    openclaw.json 读出 gateway.auth.token 注入 OPENCLAW_GATEWAY_TOKEN。
    """
    prefix = (
        "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
        f"$env:OPENCLAW_CONFIG_PATH='{remote}/openclaw.json'; "
        f"$env:OPENCLAW_STATE_DIR='{remote}'; "
        f"if(Test-Path $env:OPENCLAW_CONFIG_PATH) {{"
        f"$oc=Get-Content $env:OPENCLAW_CONFIG_PATH -Raw | ConvertFrom-Json; "
        f"$env:OPENCLAW_GATEWAY_TOKEN=$oc.gateway.auth.token}}; "
    )
    encoded = base64.b64encode((prefix + script).encode("utf-16le")).decode()
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


# ---- Windows 远程文件读取: base64 通道 -------------------------------------
# PowerShell 把字符串写回 stdout 时按控制台/OEM 代码页重新编码, 中文必然乱码;
# 且 UTF-8 字节一旦被替换成 U+FFFD 就不可逆。改成让 PS 输出文件**原始字节**的
# base64, 由 Python 端解码, 绕开代码页。
_REMOTE_B64_PREFIX = "__HIVE_B64__"


def ps_read_file_b64(path: str) -> str:
    """PowerShell 片段: 输出 '<前缀><base64>'; 文件不存在/读不到则只输出前缀。

    必须显式用 FileShare.ReadWrite 打开: gateway.log 由仍在运行的 gateway 进程
    持续持有写句柄, [IO.File]::ReadAllBytes 只请求读共享, 会撞成
    "正由另一进程使用" 的 IOException。外层还有 try/catch —— $ErrorActionPreference
    ='Stop' 下任何读取异常都会中断整段脚本, 让 gateway 明明起来了却报 S001。
    """
    return (
        f"$b64=''; "
        f"if(Test-Path '{path}') {{ try {{ "
        f"$fs=[IO.File]::Open('{path}',[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::ReadWrite); "
        f"try {{ $ms=New-Object IO.MemoryStream; $fs.CopyTo($ms); "
        f"$b64=[Convert]::ToBase64String($ms.ToArray()) }} finally {{ $fs.Dispose() }} "
        f"}} catch {{ $b64='' }} }}; "
        f"Write-Output ('{_REMOTE_B64_PREFIX}'+$b64)"
    )


def decode_remote_b64(stdout: str) -> str:
    """从 stdout 里取出 base64 段并解码为文本 (优先 UTF-8, 退回 GBK)。"""
    payload = ""
    for line in (stdout or "").splitlines():
        idx = line.find(_REMOTE_B64_PREFIX)
        if idx != -1:
            payload = line[idx + len(_REMOTE_B64_PREFIX):].strip()
            break
    if not payload:
        return ""
    try:
        data = base64.b64decode(payload, validate=True)
    except Exception:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# Windows 沙箱辅助目录: (本地源目录, 相对 home 的 tar arcname)。
# 打包后统一解压到 sandbox home, 三个目录即落到各自目标位置。
_WINDOWS_AUX_DIRS = [
    ("/root/sandbox-log/windows/skills/serper", ".openclaw/skills/serper"),
    ("/root/sandbox-log/windows/xiaoyi-device-env", "xiaoyi-device-env"),
    ("/root/sandbox-log/windows/claude_code_image_reading_tool", "claude_code_image_reading_tool"),
]


def _read_obs_credentials() -> tuple[str, str, str]:
    """从 <project>/platform/.env 读 obsutil 凭据 IK/KK/EK, 缺 .env 时回落 .env.example。

    返回 (ik, kk, ek) 对应 obsutil config 的 -i/-k/-e。缺任一键抛 C001。
    """
    # 本文件位于 utils/win_util.py, 项目根 = 上两级, platform/ 在其下
    base = Path(__file__).resolve().parent.parent / "platform"
    values: dict[str, str] = {}
    for name in (".env", ".env.example"):
        env_file = base / name
        if not env_file.is_file():
            continue
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                values.setdefault(key.strip(), val.strip().strip('"').strip("'"))
        except OSError:
            continue
        if {"IK", "KK", "EK"} <= values.keys():
            break
    ik, kk, ek = values.get("IK", ""), values.get("KK", ""), values.get("EK", "")
    if not (ik and kk and ek):
        raise HiveError("C001", detail="obsutil 凭据 (IK/KK/EK) 在 platform/.env 缺失")
    return ik, kk, ek


def _build_windows_aux_tar() -> str:
    """把 _WINDOWS_AUX_DIRS 打成一个 tar (arcname 相对 home), 返回本地 tar 路径。"""
    fd, tar_path = tempfile.mkstemp(suffix=".tar")
    os.close(fd)
    try:
        with tarfile.open(tar_path, "w") as tar:
            for src, arc in _WINDOWS_AUX_DIRS:
                if os.path.isdir(src):
                    tar.add(src, arcname=arc, recursive=True)
    except Exception:
        os.unlink(tar_path)
        raise
    return tar_path


class _DetailResult:
    """最小 Result 替身: 只为把本地拼好的文本交给 _fmt_sandbox_block 展示。"""

    def __init__(self, code=0, msg: str = "", exit_code: int = 0,
                 stdout: str = "", stderr: str = "") -> None:
        self.code = code
        self.msg = msg
        self.data = {"exit_code": exit_code, "stdout": stdout, "stderr": stderr}


def _windows_env_assign(self) -> str:
    """把 config 的 sandbox.windows.sandbox.env 拼成 PowerShell 进程级赋值串。

    config_win.yaml 里 sandbox.windows.sandbox.env 只作用在跑 QEMU Win11 的
    Linux Pod 上; 而 gateway / harness / serper 技能进程都跑在 Win11 VM 内,
    Pod 级 env 进不来。且 VM guest agent 以固定 env 派生子进程(不重读注册表),
    机器级 SetEnvironmentVariable 也传不进去 —— 所以只能在起 gateway/harness
    的 PowerShell 脚本里显式 $env:NAME='...' 注入, 让 Start-Process 起的子
    进程继承 (如 SERPER_API_KEY / SEARCH_API_KEY / SEARCH_BACKEND_URL)。
    """
    env = self.global_config.sandbox.windows.sandbox.env
    if not env:
        return ""
    parts = []
    for item in env:
        name = str(item.name).replace("'", "''")
        value = (str(item.value) if item.value is not None else "").replace("'", "''")
        parts.append(f"$env:{name}='{value}'")
    return "; ".join(parts) + ";"


async def _setup_windows_obsutil(self) -> None:
    """配置 Windows 沙箱内 obsutil 的 OBS 凭据, 并上传三个辅助目录到 home。

    1. obsutil config 的 -i/-k/-e 从 <project>/platform/.env 读 (IK/KK/EK),
        .env 不存在时回落 .env.example, 不再硬编码。
    2. 三个辅助目录 (tar 打包 + 沙箱内 tar -xf, 与 _upload_and_extract_code 同
        套路 —— ExtendUploadFile 传目录只会把 zip 字节写成一个文件, 不解压):
            /root/sandbox-log/windows/skills/serper/            -> ~/.openclaw/skills/serper/
            /root/sandbox-log/windows/xiaoyi-device-env/        -> ~/xiaoyi-device-env/
            /root/sandbox-log/windows/claude_code_image_reading_tool/ -> ~/claude_code_image_reading_tool/
    """
    s3 = self.client_config.s3
    obsutil_exe = s3.s3_download_script

    # ---- 1. obsutil 凭据 ----
    ik, kk, ek = _read_obs_credentials()
    config_cmd = f"{obsutil_exe} config -i={ik} -k={kk} -e={ek}"
    exec_request = ExtendExecCommand(
        command=["cmd.exe", "/c", config_cmd],
        timeout=self.config.simple_bash_timeout,
    )
    result = await self.execution_client.extend(args=exec_request.to_dict())
    if result.code != ErrorCode.SUCCESS[0] or result.data["exit_code"] != 0:
        _log_sandbox_detail(self.logger, "obsutil config failed", result, level=logging.ERROR)
        raise HiveError("C001", detail="obsutil config", sandbox_result=result)
    self.logger.info(f"obsutil configured successfully: {obsutil_exe}")

    # ---- 2. 上传三个辅助目录 ----
    if not any(os.path.isdir(src) for src, _ in _WINDOWS_AUX_DIRS):
        self.logger.warning("no windows aux dirs found, skip upload")
        return
    home = self.config.sandbox_config.home
    tar_name = "hive_aux_dirs.tar"
    tar_local = _build_windows_aux_tar()
    try:
        tar_remote = str(PureWindowsPath(home, tar_name))
        await self._upload_file("windows aux dirs", tar_local, tar_remote)
        exec_request = ExtendExecCommand(
            command=["cmd.exe", "/c",
                     f"cd /d {home} && tar -xf {tar_name} && del {tar_name}"],
            timeout=self.config.simple_bash_timeout,
        )
        result = await self.execution_client.extend(args=exec_request.to_dict())
        if result.code != ErrorCode.SUCCESS[0] or result.data["exit_code"] != 0:
            _log_sandbox_detail(self.logger, "extract aux dirs failed", result, level=logging.ERROR)
            raise HiveError("C001", detail="extract aux dirs", sandbox_result=result)
        self.logger.info(f"uploaded aux dirs -> {home}")
    finally:
        os.unlink(tar_local)


async def _start_openclaw_gateway_windows(self, config_file: str) -> None:
    """在 Windows 沙箱里启动 OpenClaw gateway (PowerShell -EncodedCommand 版)。

    端口模型与 Linux 分支一致: 本地把随机端口写进 openclaw.json (gateway.port)
    和 data config (gateway_ws_url) 再上传, 不远程 sed。启动方式参照 win_demo.py
    第 200 行: Start-Process 后台起 `openclaw gateway`, 再 Test-NetConnection
    轮询随机端口就绪 (openclaw gateway 读配置里的 gateway.port, 不带 --port)。
    """
    start_time = asyncio.get_running_loop().time()
    max_wait = self.config.openclaw_gateway_timeout
    sandbox_cfg = self.config.sandbox_config

    # 前向斜杠路径 (win_demo / xiaoying.sh 的写法); openclaw.json 上传到了 ~/.openclaw
    config_dir = str(PureWindowsPath(sandbox_cfg.harness_sandbox_config_file).parent).replace("\\", "/")
    workdir = sandbox_cfg.result_workdir.replace("\\", "/")
    gateway_log = f"{workdir}/{sandbox_cfg.gateway_log}"

    while True:
        elapsed = asyncio.get_running_loop().time() - start_time
        if elapsed > max_wait:
            self.logger.error(f"[S002] gateway startup timeout ({max_wait}s)")
            raise HiveError("S002", detail=f"{max_wait}s / config={os.path.basename(config_file)}")

        port = str(generate_random_port())

        # 每次重试重新写端口再上传 (与 Linux 分支一致)
        await self._copy_agent_config(config_file, port=port)
        await self._upload_data_config(config_file, port=port)

        # Start-Process 后台起 openclaw gateway + Test-NetConnection 轮询就绪
        # 就绪后把 gateway.log 以 base64 读回 (见 ps_read_file_b64 的说明),
        # 由 Python 端解码后落进 task-<idx>.log —— 与 Linux 分支的 cat 对齐。
        # 注意顺序: base64 必须在 throw 之前输出, 否则启动失败时反而拿不到日志。
        launch_script = (
            _windows_env_assign(self) + " "
            f"New-Item -ItemType Directory -Force '{workdir}' | Out-Null; "
            f"$p=Start-Process cmd.exe -ArgumentList '/d /s /c {sandbox_cfg.openclaw_bash} gateway 1> {gateway_log} 2>&1' "
            f"-PassThru -WindowStyle Hidden; "
            f"$p.Id | Set-Content '{workdir}/gateway.pid'; "
            f"$ready=$false; "
            f"for($i=0;$i -lt 90;$i++) {{ "
            f"if (Test-NetConnection 127.0.0.1 -Port {port} -InformationLevel Quiet -WarningAction SilentlyContinue) "
            f"{{$ready=$true;break}}; Start-Sleep 2 }}; "
            f"if($ready) {{Write-Output 'gateway-ready'}}; "
            + ps_read_file_b64(gateway_log) + "; "
            f"if(!$ready) {{throw 'gateway not ready'}}"
        )
        exec_request = ExtendExecCommand(
            command=ps_command(launch_script, config_dir),
            timeout=self.config.sandbox_config.openclaw_start_timeout,
        )
        result = await self.execution_client.extend(args=exec_request.to_dict())

        gateway_content = decode_remote_b64(
            (result.data or {}).get("stdout", "") or ""
        ) if result.data else ""

        if result.code != ErrorCode.SUCCESS[0] or result.data["exit_code"] != 0:
            self.logger.error(f"[S001] gateway start failed on port {port}")
            _log_sandbox_detail(self.logger, f"gateway start failed — port {port}",
                                     result, level=logging.ERROR,
                                     extra_stdout=gateway_content,
                                     extra_label=f"{sandbox_cfg.gateway_log} (tail)")
            raise HiveError("S001", detail=f"port={port}", sandbox_result=result)

        stdout = result.data.get("stdout", "")
        if "gateway-ready" in stdout:
            _log_sandbox_detail(self.logger, f"gateway startup OK — port {port}", result,
                                     extra_stdout=gateway_content,
                                     extra_label=f"{sandbox_cfg.gateway_log} (tail)")
            break
        else:
            self.logger.error(f"[S003] gateway unexpected output on port {port}")
            _log_sandbox_detail(self.logger, f"gateway unexpected output — port {port}",
                                     result, level=logging.ERROR,
                                     extra_stdout=gateway_content,
                                     extra_label=f"{sandbox_cfg.gateway_log} (tail)")
            raise HiveError("S003", detail=f"port={port}", sandbox_result=result)

    self.logger.info(f"Gateway started on port {port}")


async def _run_main_script_windows(self, config_file: str, task_idx: int) -> None:
    """在 Windows 沙箱里执行主脚本 (PowerShell -EncodedCommand 版)。

    参照 win_demo.py 的 admin-scope(202) / scope-patch(206) / launch(207) /
    poll(208-216): openclaw 先批 gateway admin scope、再给 openclaw_client.py 打
    operator_scopes 补丁, 然后写 run.cmd 用 Start-Process 后台起 harness, 轮询
    exit-code.txt 直到结束, 最后读 run.log 用 _classify_task_stdout 分类(与 Linux
    分支一致)。路径用 hive 已知的 workspace/code_stem 与 data_config, 不再做
    win_demo 的 task-path.txt/config-path.txt 间接层。
    """
    sandbox_cfg = self.config.sandbox_config
    python_file = os.path.basename(self.config.main_python_file)
    code_stem = Path(self.config.main_code_tar).stem

    # 前向斜杠路径 (win_demo / xiaoying.sh 的写法)
    task_dir = str(PureWindowsPath(sandbox_cfg.workspace, code_stem)).replace("\\", "/")
    config_path = str(PureWindowsPath(
        sandbox_cfg.data_config_path, os.path.basename(config_file)
    )).replace("\\", "/")
    workdir = sandbox_cfg.result_workdir.replace("\\", "/")
    config_dir = str(PureWindowsPath(sandbox_cfg.harness_sandbox_config_file).parent).replace("\\", "/")
    log_path = f"{workdir}/{sandbox_cfg.result_log}"

    # ---- openclaw 前置: admin scope 批准 + operator_scopes 补丁 (win_demo 202/206) ----
    if self.config.sandbox_config.harness_type.strip().lower() == "openclaw":
        # 本文件位于 utils/win_util.py, __file__.parent 即 utils/ 目录本身
        utils_dir = Path(__file__).resolve().parent

        approve_remote = f"{workdir}/approve_openclaw_admin.py"
        await self._upload_file("approve_openclaw_admin",
                                str(utils_dir / "approve_openclaw_admin.py"),
                                approve_remote)
        exec_request = ExtendExecCommand(
            command=ps_command(
                f"python '{approve_remote}'; "
                f"if($LASTEXITCODE -ne 0) {{throw 'admin scope approval failed'}}",
                config_dir,
            ),
            timeout=180,
        )
        result = await self.execution_client.extend(args=exec_request.to_dict())
        if result.code != ErrorCode.SUCCESS[0] or result.data["exit_code"] != 0:
            _log_sandbox_detail(self.logger, "admin scope approval failed",
                                     result, level=logging.ERROR)
            raise HiveError("S008", detail="admin scope approval", sandbox_result=result)

        patch_remote = f"{workdir}/patch_openclaw_scope.py"
        await self._upload_file("patch_openclaw_scope",
                                str(utils_dir / "patch_openclaw_scope.py"),
                                patch_remote)
        exec_request = ExtendExecCommand(
            command=ps_command(
                f"python '{patch_remote}' '{task_dir}/src/openclaw_client.py'; "
                f"if($LASTEXITCODE -ne 0) {{throw 'scope patch failed'}}; "
                f"Select-String -Path '{task_dir}/src/openclaw_client.py' -Pattern 'operator.admin'",
                config_dir,
            ),
            timeout=180,
        )
        result = await self.execution_client.extend(args=exec_request.to_dict())
        if result.code != ErrorCode.SUCCESS[0] or result.data["exit_code"] != 0:
            _log_sandbox_detail(self.logger, "scope patch failed",
                                     result, level=logging.ERROR)
            raise HiveError("S008", detail="scope patch", sandbox_result=result)

    # ---- 预建 agents 目录: harness 只在 create_agent 时才落 agentDir ----
    # 本流程把 agent 预注册进了 openclaw.json, setup_agent 的 list_agents 命中后
    # 直接跳过 create_agent, 于是 <home>\.openclaw\agents\<name> 谁都不创建;
    # 而 upload_paths 指向 <home>\.openclaw\agents, 上传时源目录不存在 →
    # 被静默跳过, 桶里就一直没有 agents/ 这一层 (Linux 侧由 create_agent 建出来,
    # 所以 Linux 有、Windows 没有)。这里按 task config 的 agent 名单显式建目录,
    # 与 upload_paths / prepare_config 注入的 agentDir 同一套 home 派生路径。
    if self.config.sandbox_config.harness_type.strip().lower() == "openclaw":
        agents_root = str(PureWindowsPath(sandbox_cfg.home, ".openclaw", "agents"))
        ensure_dirs = [agents_root] + self._derive_per_agent_workspaces(config_file)
        ensure_dirs += [
            str(PureWindowsPath(agents_root, n))
            for n in self._derive_agent_names(config_file)
        ]
        ensure_script = "".join(
            f"New-Item -ItemType Directory -Force '{d}' | Out-Null; "
            for d in dict.fromkeys(ensure_dirs)  # 去重保序
        )
        exec_request = ExtendExecCommand(
            command=ps_command(ensure_script, config_dir),
            timeout=self.config.simple_bash_timeout,
        )
        result = await self.execution_client.extend(args=exec_request.to_dict())
        if result.code != ErrorCode.SUCCESS[0] or result.data["exit_code"] != 0:
            self.logger.warning(
                f"pre-create agents dirs failed (upload_traj will skip agents/): "
                f"{ensure_dirs}"
            )
        else:
            self.logger.info(f"pre-created agents dirs: {ensure_dirs}")

    # ---- 写 run.cmd + Start-Process 后台起 harness (win_demo 207) ----
    run_cmd_remote = f"{workdir}/run.cmd"
    exit_code_remote = f"{workdir}/exit-code.txt"
    launch_script = (
        _windows_env_assign(self) + " "
        f"New-Item -ItemType Directory -Force '{workdir}' | Out-Null; "
        f"$bat='@echo off'+[Environment]::NewLine"
        f"+'cd /d \"{task_dir}\"'+[Environment]::NewLine"
        f"+'set PYTHONIOENCODING=utf-8'+[Environment]::NewLine"
        # harness 用 "~/.openclaw/workspace" 决定 agent workspace, 而 `~` 由
        # USERPROFILE 决定 (ntpath.expanduser 优先取它)。本进程以 SYSTEM 跑,
        # 默认 USERPROFILE 指向 systemprofile, 会把 workspace 落到那里, 与
        # 配置/home 派生的路径对不上; 显式指到 sandbox home 让两边一致。
        f"+'set USERPROFILE={sandbox_cfg.home}'+[Environment]::NewLine"
        # enable_pip_install=false 时跳过依赖安装(与 Linux 分支 _run_main_script 一致)。
        + ("+'pip install -r requirements.txt > nul 2>&1'+[Environment]::NewLine"
           if self.config.enable_pip_install else "")
        + f"+'python {python_file} --config \"{config_path}\" --harness {self.config.sandbox_config.harness_type} > \"{log_path}\" 2>&1'+[Environment]::NewLine"
        f"+'echo %errorlevel% > \"{exit_code_remote}\"'; "
        f"Set-Content -Path '{run_cmd_remote}' -Value $bat -Encoding ASCII; "
        f"Start-Process cmd.exe -ArgumentList '/d /c {run_cmd_remote}' -WorkingDirectory '{task_dir}' -WindowStyle Hidden; "
        f"Write-Output 'harness-started'"
    )
    exec_request = ExtendExecCommand(
        command=ps_command(launch_script, config_dir),
        timeout=120,
    )
    result = await self.execution_client.extend(args=exec_request.to_dict())
    if result.code != ErrorCode.SUCCESS[0] or result.data["exit_code"] != 0:
        _log_sandbox_detail(self.logger, "harness launch failed",
                                 result, level=logging.ERROR)
        raise HiveError("S008", detail="harness launch", sandbox_result=result)

    # ---- 轮询 exit-code.txt (win_demo 208-216) ----
    deadline = time.monotonic() + self.config.main_python_timeout
    exit_code = None
    while time.monotonic() < deadline:
        exec_request = ExtendExecCommand(
            command=ps_command(
                f"if(Test-Path '{exit_code_remote}') "
                f"{{Write-Output ('DONE:'+(Get-Content '{exit_code_remote}').Trim())}} "
                f"else {{Write-Output 'RUNNING'}}",
                config_dir,
            ),
            timeout=120,
        )
        result = await self.execution_client.extend(args=exec_request.to_dict())
        stdout = (result.data or {}).get("stdout", "") or ""
        if "DONE:" in stdout:
            exit_code = int(stdout.split("DONE:", 1)[1].strip())
            break
        await asyncio.sleep(20)
    else:
        self.logger.error(f"[S008] harness exceeded timeout ({self.config.main_python_timeout}s)")
        raise HiveError("S008", detail=f"timeout={self.config.main_python_timeout}s")

    # ---- 读 run.log 分类 (与 Linux 分支一致) ----
    # Get-Content -Raw 会把内容按控制台代码页重新编码写回 stdout, 中文必乱;
    # 改走 base64 原始字节通道, 在 Python 端按 UTF-8 解码。
    exec_request = ExtendExecCommand(
        command=ps_command(ps_read_file_b64(log_path), config_dir),
        timeout=120,
    )
    result = await self.execution_client.extend(args=exec_request.to_dict())
    stdout = decode_remote_b64((result.data or {}).get("stdout", "") or "")
    sandbox_failed = exit_code != 0

    title = ("script execution failed" if sandbox_failed else "script execution OK")
    _log_sandbox_detail(self.logger, f"{title} — {python_file}",
                             _DetailResult(
                                 code=result.code,
                                 msg=getattr(result, "msg", "") or "",
                                 exit_code=exit_code,
                             ),
                             level=(logging.ERROR if sandbox_failed else logging.INFO),
                             extra_stdout=stdout,
                             extra_label=f"{sandbox_cfg.result_log} ({log_path})")

    t_code, phrase = _classify_task_stdout(stdout)
    if t_code:
        self.logger.error(
            f"[{t_code}] task failure — {ERROR_CATALOG[t_code]}"
            + (f" — matched: {phrase!r}" if phrase else "")
        )
        raise HiveError(t_code, detail=phrase or python_file, sandbox_result=result)
    if sandbox_failed:
        self.logger.error(f"[S008] script execution failed: {python_file}")
        raise HiveError("S008", detail=python_file, sandbox_result=result)
    self.logger.info(f"Script completed: {python_file}")
