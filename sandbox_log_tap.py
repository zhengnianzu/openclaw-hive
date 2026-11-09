r"""Independent, on-demand sandbox log exporter.

The exporter attaches to an existing sandbox by ``env_id``. It never reuses
Hive's ``ExecutionClient`` and never calls ``client.close()`` because that SDK
method closes the sandbox itself.

Examples::

    python sandbox_log_tap.py --config config.yaml --env-id zx-xxx --log run.log
    python sandbox_log_tap.py --config config.yaml --env-id zx-xxx \
        --log gateway.log --follow
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import inspect
import logging
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Awaitable, Callable

from omegaconf import DictConfig, OmegaConf
import httpx

_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TransportError,
    ConnectionError,
    TimeoutError,
)

PROBE_PREFIX = "__HIVE_LOG_EXPORT__"
DEFAULT_CHUNK_BYTES = 256 * 1024
DEFAULT_POLL_INTERVAL = 2.0
MAX_POLL_INTERVAL = 10.0
# 单次探测的请求超时。沙箱关闭后探测会一直挂到这里才报错，之后由控制面给出
# 准确结论，所以它只决定「发现异常」的延迟，不参与判定，无需对外可调。
PROBE_TIMEOUT = 30.0
# 探测失败但控制面没有确认沙箱终态时，持续退避重试。
# 不能用重试次数推断沙箱死亡，否则网络抖动会让日志进程早于沙箱退出。
UNREACHABLE_RETRY_DELAY = 2.0
MAX_UNREACHABLE_RETRY_DELAY = 30.0
_SAFE_ENV_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_LOG_NAMES = {"run.log", "gateway.log"}


# 沙箱没了之后 SDK 可能抛自己的类型而不是 httpx 的，按类名识别避免硬依赖。
_UNREACHABLE_EXC_NAMES = {"RouteRefreshBackoffError"}
# 控制面认为这个 env 已经不存在/无权访问时的状态码。
_GONE_STATUS_CODES = {404, 410}
_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
# 兜底的文本特征：SDK 这条路径抛的是裸 RuntimeError，没有专用异常类型。
_UNREACHABLE_TEXT_TOKENS = (
    "sandbox was closed",
    "closed by user request",
    "sandbox_not_ready",
    "sandbox not found",
    "sandbox is closed",
    "sandbox has been deleted",
)


class LogExportError(RuntimeError):
    """The sandbox log could not be exported."""


class SandboxUnreachable(LogExportError):
    """探测请求没能到达沙箱，但控制面没给出确定答案。

    可能是沙箱刚关、也可能只是网络抖动 —— 分不清时按可重试处理。
    """


class SandboxClosed(SandboxUnreachable):
    """控制面确认沙箱已 closed/deleted，不会再恢复。

    follow 模式下这是正常终态：hive 在任务 finally 里关沙箱，跟随者本来
    就该在这时收尾退出，所以不重试、直接收尾。
    """


class SandboxNotFound(SandboxClosed):
    """控制面查无此沙箱（404）。

    和「关闭」分开是因为两者的处理方式不同：这个通常意味着 env_id 写错，
    或者 env_id 不属于当前 ``--config`` 指向的集群。
    """


# 控制面的终态；running 之外的其它值（creating/pending…）都当「还没好」处理。
_TERMINAL_SANDBOX_STATES = {
    "close_pending",
    "closing",
    "closed",
    "deleted",
    "expired",
    "terminated",
    "failed",
}


@dataclass(frozen=True)
class SandboxState:
    """控制面对某个 env_id 的说法。``status`` 为空表示问不出来。"""

    status: str
    detail: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_SANDBOX_STATES

    @property
    def is_missing(self) -> bool:
        """控制面查无此沙箱（404），而不是「曾经存在、现已关闭」。"""
        return self.detail.startswith("sandbox_not_found")

    @property
    def is_known(self) -> bool:
        return bool(self.status)

    def describe(self) -> str:
        suffix = f" {self.detail}" if self.detail else ""
        return f"status={self.status or 'unknown'}{suffix}"


# 由 export_sandbox_log 注入。为 None 时退回异常启发式（单测、或自带 client
# 直接调 export_with_client 的场景拿不到 config，问不了控制面）。
StateCheck = Callable[[], Awaitable[SandboxState]]


async def fetch_sandbox_state(env_id: str, config: Any) -> SandboxState:
    """问控制面这个沙箱还在不在。失败只返回 unknown，绝不往外抛。

    这是判定「沙箱是否已关闭」的权威来源。``get_sandbox_info`` 把状态放在
    ``payload["sandbox"]["status"]``（与 SDK 内部 ``wait_sandbox_ready`` 的取值
    方式一致），已关闭的还会带 ``close_reason`` / ``closed_at``。
    """
    try:
        from env_sdk.controlplane import get_sandbox_info
    except ImportError:  # pragma: no cover - deployment dependency
        return SandboxState("", "env_sdk is not installed")

    try:
        payload = await get_sandbox_info(env_id, config=config)
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code == 404:
            # 控制面明确说没这个沙箱 —— 和 closed 一样是终态。
            return SandboxState("deleted", "sandbox_not_found")
        # 控制面自己连不上时不能反过来污染判定，返回 unknown 让调用方走启发式。
        return SandboxState("", f"{type(exc).__name__}: {str(exc)[:160]}")

    if not isinstance(payload, dict):
        payload = getattr(payload, "data", None) or {}
    payload = payload.get("data") or payload.get("response") or payload
    sandbox = payload.get("sandbox") or (
        payload if payload.get("status") else {}
    )
    if not sandbox:
        sandbox = (payload.get("state") or {}).get("persisted") or {}
    status = str(sandbox.get("status") or "").strip().lower()
    detail = " ".join(
        f"{key}={sandbox[key]}"
        for key in ("close_reason", "closed_at")
        if sandbox.get(key)
    )
    return SandboxState(status, detail)


async def _query_state(state_check: StateCheck | None) -> SandboxState:
    if state_check is None:
        return SandboxState("")
    try:
        return await state_check()
    except Exception as exc:  # 状态查询永远不能成为新的失败来源
        return SandboxState("", f"{type(exc).__name__}: {str(exc)[:160]}")


def _is_unreachable(exc: BaseException) -> bool:
    """控制面问不出来时的兜底：从异常本身判断是否「连不上沙箱」。"""
    if type(exc).__name__ in _UNREACHABLE_EXC_NAMES:
        return True
    # env_sdk 在 attach/等待路由就绪失败时会给异常挂上 sdk_debug
    # (controlplane._attach_wait_failure_debug)，抛的却是裸 RuntimeError。
    # 这些失败一律意味着到不了 dataplane：沙箱 closed/deleted，或等待超时。
    if hasattr(exc, "sdk_debug") or hasattr(exc, "sdk_payload"):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in _GONE_STATUS_CODES or status in _RETRYABLE_STATUS_CODES:
        return True
    text = str(exc).lower()
    return any(token in text for token in _UNREACHABLE_TEXT_TOKENS)


def _unreachable_message(exc: BaseException) -> str:
    # ReadTimeout 等异常的 str() 常常是空的，退回类名才能给出可读信息。
    detail = str(exc).strip() or type(exc).__name__
    return f"sandbox is unreachable ({detail})"


@dataclass(frozen=True)
class ExportSettings:
    env_id: str
    log_name: str
    sandbox_type: str
    remote_path: str
    local_path: Path


@dataclass(frozen=True)
class RemoteChunk:
    exists: bool
    size: int
    start: int
    data: bytes

    @property
    def next_byte(self) -> int:
        return self.start + len(self.data)


ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


def _encode_powershell(script: str) -> list[str]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [
        "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
        "-EncodedCommand", encoded,
    ]


def _linux_probe_command(path: str, next_byte: int, max_bytes: int) -> list[str]:
    code = (
        "import base64,os;"
        f"p={path!r};o={int(next_byte)};limit={int(max_bytes)};"
        "exists=os.path.isfile(p);"
        "size=os.path.getsize(p) if exists else 0;"
        "start=0 if o>size else o;"
        "f=open(p,'rb') if exists else None;"
        "f.seek(start) if f else None;"
        "data=f.read(limit) if f else b'';"
        "f.close() if f else None;"
        f"print('{PROBE_PREFIX}'+str(int(exists))+':'+str(size)+':'"
        "+str(start)+':'+base64.b64encode(data).decode('ascii'))"
    )
    return ["/bin/bash", "-c", f"python -c {shlex.quote(code)}"]


def _windows_probe_command(path: str, next_byte: int, max_bytes: int) -> list[str]:
    escaped = path.replace("'", "''")
    script = (
        f"$p='{escaped}'; $requested=[Int64]{int(next_byte)}; "
        "$exists=Test-Path -LiteralPath $p -PathType Leaf; "
        "$size=0L; $start=0L; $b64=''; "
        "if($exists) { "
        "$fs=[IO.File]::Open($p,[IO.FileMode]::Open,[IO.FileAccess]::Read,"
        "[IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete); "
        "try { $size=$fs.Length; "
        "$start=if($requested -gt $size){0L}else{$requested}; "
        "$null=$fs.Seek($start,[IO.SeekOrigin]::Begin); "
        f"$buf=New-Object byte[] {int(max_bytes)}; "
        "$read=$fs.Read($buf,0,$buf.Length); "
        "if($read -gt 0){$b64=[Convert]::ToBase64String($buf,0,$read)} "
        "} finally {$fs.Dispose()} }; "
        f"Write-Output ('{PROBE_PREFIX}'+([int]$exists)+':'"
        "+$size+':'+$start+':'+$b64)"
    )
    return _encode_powershell(script)


def _parse_probe(stdout: str) -> RemoteChunk:
    marker = None
    for line in (stdout or "").splitlines():
        index = line.find(PROBE_PREFIX)
        if index >= 0:
            marker = line[index + len(PROBE_PREFIX):].strip()
    if marker is None:
        raise LogExportError("sandbox response does not contain a log probe marker")
    try:
        exists_raw, size_raw, start_raw, payload = marker.split(":", 3)
        return RemoteChunk(
            exists=bool(int(exists_raw)),
            size=max(0, int(size_raw)),
            start=max(0, int(start_raw)),
            data=base64.b64decode(payload, validate=True) if payload else b"",
        )
    except (TypeError, ValueError) as exc:
        raise LogExportError(f"invalid sandbox log probe response: {marker[:200]}") from exc


def _response_fields(result: Any) -> tuple[int | None, str, str]:
    """Normalise current and legacy SDK command results."""
    if isinstance(result, dict):
        response = result.get("response") or result.get("data") or result
        return response.get("exit_code"), response.get("stdout", ""), response.get("stderr", "")
    data = getattr(result, "data", None) or {}
    return data.get("exit_code"), data.get("stdout", ""), data.get("stderr", "")


async def _execute_probe(
    client: Any, command: list[str], timeout: float = PROBE_TIMEOUT
) -> str:
    """跑一次探测，把「连不上沙箱」和「探测脚本本身失败」分成两类异常。

    沙箱被关掉后 dataplane 不再应答，请求会一直挂到超时，底层抛的是
    ``httpx.ReadTimeout`` —— 它不是 ``OSError`` 也不是 ``ValueError``，
    不归一成 ``SandboxUnreachable`` 的话会直接穿透到 CLI 顶层变成 traceback。
    """
    try:
        result = await _dispatch_probe(client, command, timeout)
    except _TRANSPORT_ERRORS as exc:
        raise SandboxUnreachable(_unreachable_message(exc)) from exc
    except Exception as exc:
        if _is_unreachable(exc):
            raise SandboxUnreachable(_unreachable_message(exc)) from exc
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is not None:
            raise LogExportError(
                f"sandbox log request failed: HTTP {status}: {str(exc)[:500]}"
            ) from exc
        raise

    exit_code, stdout, stderr = _response_fields(result)
    if exit_code not in (None, 0):
        raise LogExportError(
            f"sandbox log probe failed: exit_code={exit_code}, stderr={stderr[-500:]}"
        )
    return stdout


async def _dispatch_probe(client: Any, command: list[str], timeout: float) -> Any:
    async_exec = getattr(client, "async_exec_command", None)
    sync_exec = getattr(client, "exec_command", None)
    legacy_extend = getattr(client, "extend", None)

    if callable(async_exec):
        result = async_exec(command, timeout_seconds=timeout)
        if inspect.isawaitable(result):
            result = await result
    elif callable(sync_exec):
        # Some SDK versions bind only the synchronous convenience method to
        # clients returned by attach_sandbox. Keep it off the event loop.
        result = await asyncio.to_thread(
            sync_exec, command, timeout_seconds=timeout
        )
        if inspect.isawaitable(result):
            result = await result
    elif callable(legacy_extend):
        result = legacy_extend(args={
            "cmd_name": "exec_command",
            "args": {"command": command, "timeout": timeout, "mode": "standard"},
        })
        if inspect.isawaitable(result):
            result = await result
    else:
        result = await _execute_probe_with_module_api(client, command, timeout)
    return result


async def _execute_probe_with_module_api(
    client: Any, command: list[str], timeout: float
) -> Any:
    """Use SDK module-level execution when attach did not bind client methods."""
    execute = None
    try:
        from env_sdk import async_execute_command as execute
    except (ImportError, AttributeError):
        try:
            from env_sdk.dataplane import async_execute_command as execute
        except (ImportError, AttributeError):
            pass

    if execute is None:
        methods = sorted(
            name for name in dir(client)
            if any(token in name.lower() for token in ("exec", "command", "extend", "stream"))
        )
        client_type = f"{type(client).__module__}.{type(client).__name__}"
        raise LogExportError(
            "attached sandbox client has no supported command execution method: "
            f"type={client_type}, related_methods={methods}"
        )

    result = execute(client, command, timeout_seconds=timeout)
    if inspect.isawaitable(result):
        result = await result
    return result


async def _read_remote_chunk(
    client: Any,
    settings: ExportSettings,
    next_byte: int,
    max_bytes: int,
) -> RemoteChunk:
    if settings.sandbox_type == "windows":
        command = _windows_probe_command(settings.remote_path, next_byte, max_bytes)
    else:
        command = _linux_probe_command(settings.remote_path, next_byte, max_bytes)
    return _parse_probe(await _execute_probe(client, command, PROBE_TIMEOUT))


async def _read_chunk_with_retry(
    client: Any,
    settings: ExportSettings,
    next_byte: int,
    max_bytes: int,
    *,
    stop_event: asyncio.Event,
    state_check: StateCheck | None = None,
    progress: ProgressCallback | None = None,
) -> RemoteChunk:
    """读一块日志；连不上时问控制面，再决定重试还是判定沙箱已关闭。

    这是「沙箱是否已关闭」的唯一判定点 —— 探测失败正是需要分辨的时刻，
    平时不查，所以 follow 空闲轮询不产生额外控制面请求。

    控制面说终态就直接抛 ``SandboxClosed``，不重试。状态是
    running 或问不出来时持续退避重试，直到沙箱终态或调用方停止。
    探测脚本自身的失败（非 SandboxUnreachable）立刻上抛。
    """
    attempt = 0
    while True:
        try:
            return await _read_remote_chunk(client, settings, next_byte, max_bytes)
        except SandboxUnreachable as exc:
            state = await _query_state(state_check)
            if state.is_terminal:
                raise SandboxClosed(
                    f"sandbox is closed ({state.describe()})"
                ) from exc
            if stop_event.is_set():
                raise
            attempt += 1
            retry_in = min(
                MAX_UNREACHABLE_RETRY_DELAY,
                UNREACHABLE_RETRY_DELAY * (2 ** min(attempt - 1, 4)),
            )
            await _emit(progress, {
                "event": "retry",
                "env_id": settings.env_id,
                "log": settings.log_name,
                "attempt": attempt,
                "retry_in": retry_in,
                "sandbox_status": state.status or "unknown",
                "error": str(exc),
            })
            await _wait_or_stop(stop_event, retry_in)


async def _attach_client(env_id: str, config: DictConfig) -> Any:
    # Lazy import keeps tests usable when the deployment SDK is not installed
    # in the local development interpreter.
    try:
        from env_sdk.controlplane import attach_sandbox
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise LogExportError(
            "env_sdk is not installed; install omni_env_sdk before exporting logs"
        ) from exc
    try:
        return await attach_sandbox(env_id=env_id, config=config)
    except _TRANSPORT_ERRORS as exc:
        raise SandboxUnreachable(_unreachable_message(exc)) from exc
    except Exception as exc:
        # attach 阶段沙箱就已经关闭/删除时，SDK 同样抛裸 RuntimeError。
        if _is_unreachable(exc):
            raise SandboxUnreachable(_unreachable_message(exc)) from exc
        raise


def _config_value(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    try:
        value = config.get(key, default)
    except AttributeError:
        return default
    return default if value is None else value


def _normalise_log_name(log_name: str) -> str:
    canonical = str(log_name or "").strip().lower()
    if canonical not in _LOG_NAMES:
        raise LogExportError("log_name must be 'run.log' or 'gateway.log'")
    return canonical


def resolve_export_settings(
    config: DictConfig,
    *,
    config_path: str | os.PathLike[str],
    env_id: str,
    log_name: str,
    output_dir: str | os.PathLike[str] | None = None,
) -> ExportSettings:
    """Resolve allowlisted remote and local paths without importing hive.py."""
    if not _SAFE_ENV_ID.fullmatch(env_id):
        raise LogExportError("env_id contains unsupported characters")
    log_name = _normalise_log_name(log_name)

    sandbox_names = list(_config_value(config, "sandbox", {}).keys())
    sandbox_type = sandbox_names[0] if sandbox_names else "x86_cpu"
    run_config = _config_value(config, "run_config", {})
    sandbox_config = _config_value(run_config, "sandbox", {})
    task_config = _config_value(run_config, "task", {})

    is_windows = sandbox_type == "windows"
    path_cls = PureWindowsPath if is_windows else PurePosixPath
    default_home = r"C:\Users\win11admin" if is_windows else "/home/ma-user"
    home = str(_config_value(sandbox_config, "home", default_home))
    workspace = str(_config_value(sandbox_config, "workspace", path_cls(home, "workspace")))
    workdir = str(_config_value(sandbox_config, "result_workdir", path_cls(workspace, "workdir")))
    filename_key = "result_log" if log_name == "run.log" else "gateway_log"
    default_filename = log_name
    filename = str(_config_value(sandbox_config, filename_key, default_filename))
    remote_path = str(path_cls(workdir, filename))

    if output_dir is None:
        task_output = str(_config_value(task_config, "task_output_path", "outputs"))
        output_root = Path(task_output) / Path(config_path).stem / "log_exports"
    else:
        output_root = Path(output_dir)
    local_path = output_root.resolve() / env_id / log_name

    return ExportSettings(
        env_id=env_id,
        log_name=log_name,
        sandbox_type=sandbox_type,
        remote_path=remote_path,
        local_path=local_path,
    )


async def _emit(callback: ProgressCallback | None, event: dict[str, Any]) -> None:
    if callback is None:
        return
    result = callback(event)
    if asyncio.iscoroutine(result):
        await result


async def _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def export_with_client(
    client: Any,
    settings: ExportSettings,
    *,
    follow: bool = False,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    stop_event: asyncio.Event | None = None,
    progress: ProgressCallback | None = None,
    state_check: StateCheck | None = None,
) -> Path:
    """Mirror one sandbox log locally.

    The initial file size is captured by the first successful probe. Every
    byte in that range is written before follow mode starts, so the first
    snapshot is complete rather than a tail window.

    ``state_check`` 是控制面状态查询，只在探测失败时被调用 —— 那正是需要分辨
    「沙箱关了」和「网络抖了」的时刻，平时不查，所以 follow 空闲轮询不产生额外
    控制面请求。控制面确认终态后立即正常收尾；日志未读完时使用
    ``reason="sandbox_closed_incomplete"`` 并保留已导出字节。沙箱仍存活或控制面
    状态未知时持续退避重试，不会早于沙箱退出。
    """
    if chunk_bytes < 1024:
        raise ValueError("chunk_bytes must be at least 1024")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")

    stop_event = stop_event or asyncio.Event()
    settings.local_path.parent.mkdir(parents=True, exist_ok=True)
    next_byte = 0
    snapshot_end: int | None = None
    interval = poll_interval
    reason = "complete"
    detail: str | None = None

    await _emit(progress, {
        "event": "start", "env_id": settings.env_id,
        "log": settings.log_name, "remote_path": settings.remote_path,
        "local_path": str(settings.local_path), "follow": follow,
    })

    try:
        with settings.local_path.open("wb") as local_file:
            while not stop_event.is_set():
                request_bytes = chunk_bytes
                if snapshot_end is not None and next_byte < snapshot_end:
                    request_bytes = min(request_bytes, snapshot_end - next_byte)

                try:
                    chunk = await _read_chunk_with_retry(
                        client, settings, next_byte, request_bytes,
                        stop_event=stop_event,
                        state_check=state_check,
                        progress=progress,
                    )
                except SandboxClosed as exc:
                    # 沙箱生命周期结束就是 follow 的正常终点。无论快照
                    # 是否读完都保留已写入字节并以成功状态收尾。
                    snapshot_complete = (
                        snapshot_end is not None and next_byte >= snapshot_end
                    )
                    reason = (
                        "sandbox_closed"
                        if snapshot_complete
                        else "sandbox_closed_incomplete"
                    )
                    detail = str(exc)
                    break
                except SandboxUnreachable:
                    if stop_event.is_set():
                        reason = "stopped"
                        break
                    raise

                if not chunk.exists:
                    if not follow:
                        raise LogExportError(
                            f"sandbox log does not exist: {settings.remote_path}"
                        )
                    await _wait_or_stop(stop_event, interval)
                    interval = min(MAX_POLL_INTERVAL, interval * 2)
                    continue

                if snapshot_end is None:
                    snapshot_end = chunk.size

                # A smaller remote file means truncation/recreation. Reset the
                # local mirror and establish a new complete snapshot boundary.
                if chunk.start < next_byte:
                    local_file.seek(0)
                    local_file.truncate(0)
                    next_byte = 0
                    snapshot_end = chunk.size

                data = chunk.data
                if next_byte < snapshot_end:
                    data = data[: snapshot_end - next_byte]
                if data:
                    local_file.write(data)
                    local_file.flush()
                    next_byte = chunk.start + len(data)
                    interval = poll_interval
                    await _emit(progress, {
                        "event": "chunk", "bytes": len(data),
                        "local_size": next_byte,
                    })

                if next_byte < snapshot_end:
                    continue

                if not follow:
                    break

                # Initial snapshot complete. Subsequent probes read only bytes
                # appended after next_byte.
                snapshot_end = chunk.size
                if chunk.size > next_byte:
                    continue
                await _wait_or_stop(stop_event, interval)
                interval = min(MAX_POLL_INTERVAL, interval * 2)
    except Exception:
        if settings.local_path.exists() and settings.local_path.stat().st_size == 0:
            settings.local_path.unlink(missing_ok=True)
        raise

    if reason == "complete" and stop_event.is_set():
        reason = "stopped"
    done_event: dict[str, Any] = {
        "event": "done", "env_id": settings.env_id,
        "log": settings.log_name, "local_path": str(settings.local_path),
        "bytes": settings.local_path.stat().st_size, "reason": reason,
    }
    if detail:
        done_event["detail"] = detail
    await _emit(progress, done_event)
    return settings.local_path


async def export_sandbox_log(
    *,
    config_path: str | os.PathLike[str],
    env_id: str,
    log_name: str = "run.log",
    follow: bool = False,
    output_dir: str | os.PathLike[str] | None = None,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    stop_event: asyncio.Event | None = None,
    progress: ProgressCallback | None = None,
) -> Path:
    """Attach by env_id and export a sandbox log without touching Hive."""
    config = OmegaConf.load(config_path)
    settings = resolve_export_settings(
        config, config_path=config_path, env_id=env_id,
        log_name=log_name, output_dir=output_dir,
    )

    client = await _attach_client(env_id, config)
    # get_sandbox_info 要求 sandbox_id，不是兼容层的 env_id。两者通常
    # 相同，但路由架构下不应依赖这个假设。
    sandbox_id = str(getattr(client, "sandbox_id", None) or env_id)

    async def state_check() -> SandboxState:
        return await fetch_sandbox_state(sandbox_id, config)

    # Do not call client.close(): in env_sdk that closes the remote sandbox.
    return await export_with_client(
        client, settings, follow=follow, chunk_bytes=chunk_bytes,
        poll_interval=poll_interval,
        stop_event=stop_event, progress=progress, state_check=state_check,
    )


async def _cli_async(args: argparse.Namespace) -> int:
    _REASON_TEXT = {
        "complete": "log fully exported",
        "sandbox_closed": "sandbox closed (confirmed by control plane); stopped following",
        "sandbox_closed_incomplete": "sandbox closed; preserved incomplete log",
        "stopped": "interrupted",
    }

    async def print_progress(event: dict[str, Any]) -> None:
        if event["event"] == "start":
            # flush：进度走 stdout、错误走 stderr(logging)，不刷新会乱序。
            print(f"attach env_id={event['env_id']}", flush=True)
            print(f"remote={event['remote_path']}", flush=True)
            print(f"local={event['local_path']}", flush=True)
        elif event["event"] == "done":
            reason = event.get("reason", "complete")
            note = _REASON_TEXT.get(reason, reason)
            print(f"exported {event['bytes']} bytes -> {event['local_path']}", flush=True)
            print(f"finished: {note}", flush=True)
        elif event["event"] == "retry":
            print(
                "sandbox is still alive or its state is unknown; "
                f"retry #{event['attempt']} in {event['retry_in']:.1f}s: "
                f"{event['error']}",
                flush=True,
            )

    try:
        await export_sandbox_log(
            config_path=args.config, env_id=args.env_id, log_name=args.log,
            follow=args.follow, output_dir=args.output_dir,
            chunk_bytes=args.chunk_bytes, poll_interval=args.poll_interval,
            progress=print_progress,
        )
    except SandboxNotFound as exc:
        logging.getLogger(__name__).error(
            "%s. 请检查 env_id 是否拼写正确，以及它是否属于 --config "
            "(%s) 指向的集群。",
            exc, args.config,
        )
        return 4
    except SandboxClosed as exc:
        # 控制面确认沙箱已关闭，而我们一个字节都没导出：说明来晚了。
        logging.getLogger(__name__).error(
            "%s. 日志已随沙箱销毁，请改用任务日志或 OBS 交付物。", exc
        )
        return 3
    except SandboxUnreachable as exc:
        # 快照还没拿全就连不上，且控制面也问不出沙箱状态。
        logging.getLogger(__name__).error(
            "%s (env_id=%s). 控制面未确认沙箱状态；可能是网络问题，"
            "或 env_id 不属于当前 --config 的集群。",
            exc, args.env_id,
        )
        return 2
    except (LogExportError, OSError, ValueError) as exc:
        logging.getLogger(__name__).error("%s: %s", type(exc).__name__, exc)
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Attach to a sandbox by env_id and export run.log/gateway.log"
    )
    parser.add_argument("--config", required=True, help="Hive/SDK YAML config")
    parser.add_argument("--env-id", required=True, help="existing sandbox env_id")
    parser.add_argument(
        "--log", choices=("run.log", "gateway.log"), default="run.log"
    )
    parser.add_argument(
        "--follow", action="store_true",
        help="after the complete initial snapshot, keep appending new content",
    )
    parser.add_argument(
        "--output-dir",
        help="log_exports root; defaults to outputs/<config-name>/log_exports",
    )
    parser.add_argument("--chunk-bytes", type=int, default=DEFAULT_CHUNK_BYTES)
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    args = parser.parse_args()
    try:
        code = asyncio.run(_cli_async(args))
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)


if __name__ == "__main__":
    main()
