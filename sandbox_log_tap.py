"""按需读取活动任务沙箱中的 run.log / gateway.log。

Hive 进程启动一个仅监听 127.0.0.1 的轻量 JSON-lines 控制服务。服务空闲时
不会访问沙箱；调用 fetch 后才复用活动任务已有的 ExecutionClient，以短命
exec 命令按字节偏移读取日志，并把结果写入本地 log_exports 目录。

本模块不依赖 execution_client 的具体实现，兼容 legacy ExecutionClient 和
CurrentArchitectureExecutionClient。

用法：
    终端A： 启动Hive
        python hive.py --config config.yaml
    等待日志出现：
        Sandbox log tap ready: ...\outputs\instance_config\.sandbox-log-tap.json
    终端B：
    1）查看当前活动沙箱：
        python sandbox_log_tap.py --control-file ".\outputs\instance_config\.sandbox-log-tap.json" --list
    2）点击式加载沙箱 gateway.log 内容
        python sandbox_log_tap.py --control-file ".\outputs\instance_config\.sandbox-log-tap.json" --task-idx 0 --log gateway
    3）持续跟随run.log新增日志，只看调用之后的新内容
        python sandbox_log_tap.py --control-file ".\outputs\instance_config\.sandbox-log-tap.json" 
            --task-idx 0 --log run --follow

注意：只有任务沙箱仍处于活动状态时才能远程读取。任务结束后连接会注销；已经保存到 log_exports 的本地日志仍然保留。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import codecs
import json
import logging
import os
import secrets
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator


CONTROL_FILE_NAME = ".sandbox-log-tap.json"
PROBE_PREFIX = "__HIVE_LOG_TAP__"
DEFAULT_MAX_BYTES = 256 * 1024
DEFAULT_INTERVAL = 2.0
MAX_INTERVAL = 10.0


class LogTapError(RuntimeError):
    """按需日志读取失败。"""


@dataclass
class RemoteChunk:
    exists: bool
    size: int
    start: int
    data: bytes

    @property
    def next_cursor(self) -> int:
        return self.start + len(self.data)


@dataclass
class SandboxHandle:
    env_id: str
    task_idx: int
    config_name: str
    client: Any
    sandbox_type: str
    paths: dict[str, str]
    closing: asyncio.Event = field(default_factory=asyncio.Event)
    read_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stream_tasks: set[asyncio.Task[Any]] = field(default_factory=set)


def _normalise_remote_path(path: str, sandbox_type: str) -> str:
    value = str(path or "").strip().replace("\\", "/")
    while "//" in value:
        value = value.replace("//", "/")
    if sandbox_type == "windows":
        return value.casefold()
    return value


def _encode_powershell(script: str) -> list[str]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        encoded,
    ]


def _linux_probe_command(path: str, cursor: int, max_bytes: int) -> list[str]:
    code = (
        "import base64,os;"
        f"p={path!r};o={int(cursor)};limit={int(max_bytes)};"
        "exists=os.path.isfile(p);"
        "size=os.path.getsize(p) if exists else 0;"
        "start=size if o<0 else (0 if o>size else o);"
        "f=open(p,'rb') if exists else None;"
        "f.seek(start) if f else None;"
        "data=f.read(limit) if f else b'';"
        "f.close() if f else None;"
        f"print('{PROBE_PREFIX}'+str(int(exists))+':'+str(size)+':'"
        "+str(start)+':'+base64.b64encode(data).decode('ascii'))"
    )
    return ["/bin/bash", "-c", f"python -c {shlex.quote(code)}"]


def _windows_probe_command(path: str, cursor: int, max_bytes: int) -> list[str]:
    escaped = path.replace("'", "''")
    script = (
        f"$p='{escaped}'; $requested=[Int64]{int(cursor)}; "
        "$exists=Test-Path -LiteralPath $p -PathType Leaf; "
        "$size=0L; $start=0L; $b64=''; "
        "if($exists) { "
        "$fs=[IO.File]::Open($p,[IO.FileMode]::Open,[IO.FileAccess]::Read,"
        "[IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete); "
        "try { $size=$fs.Length; "
        "$start=if($requested -lt 0){$size}elseif($requested -gt $size){0L}else{$requested}; "
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
        raise LogTapError("sandbox response does not contain a log probe marker")
    try:
        exists_raw, size_raw, start_raw, payload = marker.split(":", 3)
        data = base64.b64decode(payload, validate=True) if payload else b""
        return RemoteChunk(
            exists=bool(int(exists_raw)),
            size=max(0, int(size_raw)),
            start=max(0, int(start_raw)),
            data=data,
        )
    except (TypeError, ValueError) as exc:
        raise LogTapError(f"invalid sandbox log probe response: {marker[:200]}") from exc


async def _read_remote_chunk(
    handle: SandboxHandle,
    path: str,
    cursor: int,
    max_bytes: int,
) -> RemoteChunk:
    if handle.sandbox_type == "windows":
        command = _windows_probe_command(path, cursor, max_bytes)
    else:
        command = _linux_probe_command(path, cursor, max_bytes)
    request = {
        "cmd_name": "exec_command",
        "args": {"command": command, "timeout": 30, "mode": "standard"},
    }
    async with handle.read_lock:
        if handle.closing.is_set():
            raise LogTapError("sandbox task is closing")
        result = await handle.client.extend(args=request)
    data = getattr(result, "data", None) or {}
    if getattr(result, "code", None) != 200 or data.get("exit_code") not in (None, 0):
        raise LogTapError(
            "sandbox log read failed: "
            f"code={getattr(result, 'code', None)} "
            f"exit_code={data.get('exit_code')} "
            f"stderr={(data.get('stderr') or '')[-500:]}"
        )
    return _parse_probe(data.get("stdout", "") or "")


class SandboxLogService:
    """活动沙箱注册表和本地按需日志控制服务。"""

    def __init__(self, output_dir: str | os.PathLike[str], logger: logging.Logger | None = None):
        self.output_dir = Path(output_dir).resolve()
        self.control_file = self.output_dir / CONTROL_FILE_NAME
        self.export_root = self.output_dir / "log_exports"
        self.logger = logger or logging.getLogger(__name__)
        self.token = secrets.token_urlsafe(32)
        self.server: asyncio.AbstractServer | None = None
        self.handles: dict[str, SandboxHandle] = {}
        self.task_index: dict[int, str] = {}
        self._shutdown = asyncio.Event()
        self._writers: set[asyncio.StreamWriter] = set()
        self._control_metadata: dict[str, Any] | None = None

    def _write_control_file(self, status: str) -> None:
        if self._control_metadata is None:
            return
        self._control_metadata["status"] = status
        self.control_file.write_text(
            json.dumps(self._control_metadata, ensure_ascii=False), encoding="utf-8"
        )
        try:
            os.chmod(self.control_file, 0o600)
        except OSError:
            pass

    async def start(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.export_root.mkdir(parents=True, exist_ok=True)
        self.server = await asyncio.start_server(self._handle_connection, "127.0.0.1", 0)
        socket = self.server.sockets[0]
        port = int(socket.getsockname()[1])
        self._control_metadata = {
            "version": 1,
            "pid": os.getpid(),
            "host": "127.0.0.1",
            "port": port,
            "token": self.token,
        }
        # write_text 会覆盖上一次运行留下的 stopped 控制文件。
        self._write_control_file("running")
        self.logger.info(f"Sandbox log tap ready: {self.control_file}")

    def register(
        self,
        *,
        env_id: str,
        task_idx: int,
        config_name: str,
        client: Any,
        sandbox_type: str,
        run_log_path: str,
        gateway_log_path: str,
    ) -> None:
        handle = SandboxHandle(
            env_id=env_id,
            task_idx=int(task_idx),
            config_name=config_name,
            client=client,
            sandbox_type=sandbox_type,
            paths={"run": run_log_path, "gateway": gateway_log_path},
        )
        self.handles[env_id] = handle
        self.task_index[int(task_idx)] = env_id

    async def unregister(self, env_id: str) -> None:
        handle = self.handles.pop(env_id, None)
        if handle is None:
            return
        handle.closing.set()
        if self.task_index.get(handle.task_idx) == env_id:
            self.task_index.pop(handle.task_idx, None)
        tasks = [task for task in handle.stream_tasks if not task.done()]
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=2)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    def _resolve_handle(self, request: dict[str, Any]) -> SandboxHandle:
        env_id = str(request.get("env_id") or "").strip()
        if not env_id and request.get("task_idx") is not None:
            try:
                env_id = self.task_index.get(int(request["task_idx"]), "")
            except (TypeError, ValueError):
                pass
        handle = self.handles.get(env_id)
        if handle is None:
            raise LogTapError("active sandbox not found")
        return handle

    @staticmethod
    def _resolve_remote_path(handle: SandboxHandle, request: dict[str, Any]) -> tuple[str, str]:
        log_name = str(request.get("log") or "run").strip().lower()
        if log_name not in handle.paths:
            raise LogTapError("log must be 'run' or 'gateway'")
        configured = handle.paths[log_name]
        supplied = str(request.get("sandbox_path") or "").strip()
        if supplied and (
            _normalise_remote_path(supplied, handle.sandbox_type)
            != _normalise_remote_path(configured, handle.sandbox_type)
        ):
            raise LogTapError("sandbox_path is outside the configured log allowlist")
        return log_name, configured

    def _local_path(self, handle: SandboxHandle, log_name: str) -> Path:
        directory = self.export_root / f"task-{handle.task_idx}"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{log_name}.log"

    async def _send(self, writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
        writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()

    async def _stream_fetch(
        self,
        request: dict[str, Any],
        writer: asyncio.StreamWriter,
        handle: SandboxHandle | None = None,
    ) -> None:
        handle = handle or self._resolve_handle(request)
        log_name, remote_path = self._resolve_remote_path(handle, request)
        follow = bool(request.get("follow", False))
        max_bytes = min(
            DEFAULT_MAX_BYTES,
            max(1024, int(request.get("max_bytes", DEFAULT_MAX_BYTES))),
        )
        local_path = self._local_path(handle, log_name)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        cursor = 0
        interval = DEFAULT_INTERVAL

        await self._send(writer, {
            "event": "start",
            "env_id": handle.env_id,
            "task_idx": handle.task_idx,
            "config_name": handle.config_name,
            "log": log_name,
            "sandbox_path": remote_path,
            "local_path": str(local_path),
            "follow": follow,
        })

        with local_path.open("wb") as local_file:
            while not self._shutdown.is_set() and not handle.closing.is_set():
                chunk = await _read_remote_chunk(handle, remote_path, cursor, max_bytes)

                # 文件被截断或重新创建：本地镜像也从头开始，避免新旧日志拼接。
                if chunk.start < cursor:
                    local_file.seek(0)
                    local_file.truncate(0)
                    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

                if chunk.data:
                    local_file.write(chunk.data)
                    local_file.flush()
                    text = decoder.decode(chunk.data, final=False)
                    cursor = chunk.next_cursor
                    interval = DEFAULT_INTERVAL
                    await self._send(writer, {
                        "event": "chunk",
                        "remote_size": chunk.size,
                        "text": text,
                    })
                    # 单次读取有上限；未追上 EOF 时立即取下一块，不 sleep。
                    if cursor < chunk.size:
                        continue
                else:
                    cursor = chunk.start

                if not follow:
                    break
                try:
                    await asyncio.wait_for(handle.closing.wait(), timeout=interval)
                    break
                except asyncio.TimeoutError:
                    pass
                interval = min(MAX_INTERVAL, interval * 2)

        tail = decoder.decode(b"", final=True)
        if tail:
            await self._send(writer, {"event": "chunk", "text": tail})
        await self._send(writer, {
            "event": "done",
            "env_id": handle.env_id,
            "task_idx": handle.task_idx,
            "local_path": str(local_path),
            "reason": "task_closing" if handle.closing.is_set() else "complete",
        })

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._writers.add(writer)
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=10)
            if not raw:
                return
            request = json.loads(raw.decode("utf-8"))
            if not secrets.compare_digest(str(request.get("token") or ""), self.token):
                raise LogTapError("invalid token")
            action = str(request.get("action") or "fetch").strip().lower()
            if action == "list":
                tasks = [
                    {
                        "env_id": h.env_id,
                        "task_idx": h.task_idx,
                        "config_name": h.config_name,
                        "logs": h.paths,
                    }
                    for h in sorted(self.handles.values(), key=lambda item: item.task_idx)
                ]
                await self._send(writer, {"event": "tasks", "tasks": tasks})
                await self._send(writer, {"event": "done"})
            elif action == "fetch":
                handle = self._resolve_handle(request)
                task = asyncio.current_task()
                if task is not None:
                    handle.stream_tasks.add(task)
                try:
                    await self._stream_fetch(request, writer, handle=handle)
                finally:
                    if task is not None:
                        handle.stream_tasks.discard(task)
            else:
                raise LogTapError("action must be 'list' or 'fetch'")
        except (BrokenPipeError, ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception as exc:
            try:
                await self._send(writer, {
                    "event": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                })
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            self._writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    async def close(self) -> None:
        self._shutdown.set()
        for env_id in list(self.handles):
            await self.unregister(env_id)
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        for writer in list(self._writers):
            writer.close()
        self._writers.clear()
        try:
            # 文件保留给调用方识别任务已结束；下次 Hive 启动时原位覆盖。
            self._write_control_file("stopped")
        except OSError as exc:
            self.logger.warning(f"update sandbox log tap control file failed: {exc}")


_active_service: SandboxLogService | None = None


async def start_log_tap_service(
    output_dir: str | os.PathLike[str],
    logger: logging.Logger | None = None,
) -> SandboxLogService:
    global _active_service
    if _active_service is not None:
        return _active_service
    service = SandboxLogService(output_dir, logger=logger)
    await service.start()
    _active_service = service
    return service


def get_log_tap_service() -> SandboxLogService | None:
    return _active_service


async def stop_log_tap_service() -> None:
    global _active_service
    service, _active_service = _active_service, None
    if service is not None:
        await service.close()


async def iter_log_request(
    control_file: str | os.PathLike[str],
    *,
    action: str = "fetch",
    task_idx: int | None = None,
    env_id: str | None = None,
    log: str = "run",
    sandbox_path: str | None = None,
    follow: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """按钮后端/CLI 使用的客户端：连接运行中的 Hive 日志控制服务。"""
    metadata = json.loads(Path(control_file).read_text(encoding="utf-8"))
    if metadata.get("status", "running") != "running":
        raise LogTapError("Hive sandbox log service is not running")
    reader, writer = await asyncio.open_connection(metadata["host"], int(metadata["port"]))
    request = {
        "token": metadata["token"],
        "action": action,
        "task_idx": task_idx,
        "env_id": env_id,
        "log": log,
        "sandbox_path": sandbox_path,
        "follow": follow,
    }
    writer.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
    await writer.drain()
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            event = json.loads(line.decode("utf-8"))
            yield event
            if event.get("event") in {"done", "error"}:
                break
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


async def _cli_async(args: argparse.Namespace) -> int:
    action = "list" if args.list else "fetch"
    try:
        async for event in iter_log_request(
            args.control_file,
            action=action,
            task_idx=args.task_idx,
            env_id=args.env_id,
            log=args.log,
            sandbox_path=args.sandbox_path,
            follow=args.follow,
        ):
            if event.get("event") == "chunk":
                print(event.get("text", ""), end="", flush=True)
            else:
                print(json.dumps(event, ensure_ascii=False), flush=True)
            if event.get("event") == "error":
                return 1
    except (LogTapError, OSError, ValueError) as exc:
        print(json.dumps({
            "event": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False), flush=True)
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="按需读取运行中 Hive 任务的沙箱日志")
    parser.add_argument("--control-file", required=True, help="Hive 输出目录下的控制文件")
    parser.add_argument("--task-idx", type=int)
    parser.add_argument("--env-id")
    parser.add_argument("--log", choices=("run", "gateway"), default="run")
    parser.add_argument("--sandbox-path", help="可选；必须与该任务配置的日志路径一致")
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--list", action="store_true", help="列出活动沙箱")
    args = parser.parse_args()
    if not args.list and args.task_idx is None and not args.env_id:
        parser.error("fetch requires --task-idx or --env-id")
    raise SystemExit(asyncio.run(_cli_async(args)))


if __name__ == "__main__":
    main()
