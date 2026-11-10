"""沙箱实时日志：按需 attach 正在运行的任务沙箱，增量缓存到本地文件。

设计要点（与 doc/sandbox_log_tap.md 的约束一致）：

* **仅在有人看时才连接**。前端打开「实时」tab 才会打到这里；关掉后不再有请求，
  attach 的 client 由 TTL 自然过期。没人看 == 零 RPC。
* **本地缓存文件为准**。每次探测把新字节 append 到
  ``outputs/<cfg>/sandbox_live/<env_id>/run.log``，响应回传的内容一律从该文件读。
  这样多个浏览器/多次刷新看到的是同一份、且已拉到的历史不会因为换个会话而丢。
* **绝不调 ``client.close()``** —— env_sdk 里那会关掉远端沙箱本身（见
  sandbox_log_tap 模块文档）。缓存过期时只丢引用。

「任务是否还在跑」沿用 ``_scan_events`` 的判据：有 env_id（沙箱已建）且
``summary["status"]`` 为空（还没出现 ``任务执行状态=`` 行）。task_records.status
做不到 —— 运行中与未执行都是 NULL。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.security import get_current_user
from .logs import _get_instance, _get_output_dir, _resolve_log_file, _scan_events

router = APIRouter(prefix="/api/logs", tags=["logs"])

# sandbox_log_tap.py 在仓库根（hive.py 旁边），不是平台包的一部分。
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 单次响应回传上限。沙箱侧探测本身也按这个值取，避免一次拉太多把接口拖慢。
CHUNK_BYTES = 256 * 1024
# attach 到的 client 复用窗口。前端 2s 一轮，60s 足够跨过偶发卡顿；
# 过期只丢引用（不 close，否则会关掉远端沙箱）。
_ATTACH_TTL = 60.0

# env_id -> (client, last_used)
_clients: dict[str, tuple[object, float]] = {}
# 同一 env_id 的并发请求串行化：避免两个浏览器同时 attach / 同时 append 同一文件。
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(env_id: str) -> asyncio.Lock:
    lock = _locks.get(env_id)
    if lock is None:
        lock = _locks[env_id] = asyncio.Lock()
    return lock


def _sweep_clients(now: float) -> None:
    for env_id, (_client, last) in list(_clients.items()):
        if now - last > _ATTACH_TTL:
            # 只丢引用：close() 会关掉远端沙箱。
            _clients.pop(env_id, None)
            _locks.pop(env_id, None)


async def _get_client(env_id: str, config):
    """取一个可用 client，优先复用 —— attach 本身有控制面开销。"""
    now = time.time()
    _sweep_clients(now)
    cached = _clients.get(env_id)
    if cached is not None:
        _clients[env_id] = (cached[0], now)
        return cached[0]

    from sandbox_log_tap import _attach_client

    client = await _attach_client(env_id, config)
    _clients[env_id] = (client, now)
    return client


def _cache_path(output_dir: str, env_id: str, log_name: str) -> Path:
    """缓存文件位置：outputs/<cfg>/sandbox_live/<env_id>/<log_name>。

    注意：该目录不会自动清理。它与 outputs/ 同生命周期，随实例产物一起被
    现有的清理流程带走（.gitignore 已覆盖 platform/instances/ 与 outputs/）。
    单个沙箱 run.log 实测 ~90KB 量级，按需拉取下只有被真正查看过的任务才落盘。
    """
    return Path(output_dir) / "sandbox_live" / env_id / log_name


@router.get("/{instance_id}/sandbox-live/{filename}")
async def sandbox_live(
    instance_id: str,
    filename: str,
    offset: int = Query(default=0, ge=0, description="本地缓存已读到的字节偏移"),
    log: str = Query(default="run.log", pattern="^(run|gateway)\\.log$"),
    user: dict = Depends(get_current_user),
):
    """拉取运行中任务的沙箱日志增量。

    返回 ``{text, next_offset, size, task_running, sandbox_alive, truncated, note}``。

    ``text`` 总是从本地缓存文件读出的 ``offset`` 之后的内容：先尽力向沙箱探测一次
    把新字节落盘，再统一从文件回读。沙箱不可达时不报 5xx —— 回传已缓存内容并在
    ``note`` 里说明，前端据此显示「重连中」而非错误。
    """
    # 复用 logs.py 的白名单正则（不接受 '/' 与 '..'），顺带确认实例存在。
    task_log = _resolve_log_file(instance_id, filename)
    if not filename.startswith("task-"):
        raise HTTPException(status_code=400, detail="实时日志仅适用于 task-<idx>.log")

    inst = _get_instance(instance_id)
    output_dir = _get_output_dir(inst["config_path"])

    if not os.path.exists(task_log):
        return {"text": "", "next_offset": offset, "size": 0,
                "task_running": False, "sandbox_alive": False,
                "truncated": False, "note": "任务日志尚未生成"}

    scan = await asyncio.to_thread(_scan_events, task_log)
    summary = scan.get("summary") or {}
    env_id = summary.get("env_id")

    # 已出现 任务执行状态= 行 → 任务收尾，不再 attach（前端据此隐藏「实时」tab）。
    if summary.get("status"):
        return {"text": "", "next_offset": offset, "size": 0,
                "task_running": False, "sandbox_alive": False,
                "truncated": False, "note": "任务已结束"}

    if not env_id:
        return {"text": "", "next_offset": offset, "size": 0,
                "task_running": True, "sandbox_alive": False,
                "truncated": False, "note": "环境尚未就绪"}

    from omegaconf import OmegaConf
    from sandbox_log_tap import (
        LogExportError, SandboxClosed, SandboxUnreachable,
        _read_remote_chunk, resolve_export_settings,
    )

    cache = _cache_path(output_dir, env_id, log)
    note = ""
    sandbox_alive = True

    async with _lock_for(env_id):
        try:
            config = await asyncio.to_thread(OmegaConf.load, inst["config_path"])
            # 只用 settings 的 remote_path / sandbox_type（沙箱内路径与平台分支）；
            # 本地文件由本模块自己 append，不走 tap 的 follow 循环。output_dir
            # 传成缓存目录的父级，使 settings.local_path 正好等于 cache，便于排查。
            export_settings = resolve_export_settings(
                config, config_path=inst["config_path"], env_id=env_id,
                log_name=log, output_dir=str(cache.parent.parent),
            )
            client = await _get_client(env_id, config)

            # 从缓存文件当前大小续拉：缓存就是进度本身，不额外存游标。
            cached_size = cache.stat().st_size if cache.exists() else 0
            chunk = await _read_remote_chunk(client, export_settings, cached_size, CHUNK_BYTES)

            if not chunk.exists:
                note = "沙箱内日志尚未创建"
            else:
                cache.parent.mkdir(parents=True, exist_ok=True)
                if chunk.start < cached_size:
                    # 远端被截断/重建：本地镜像整体重置，让前端清屏重来。
                    await asyncio.to_thread(cache.write_bytes, chunk.data)
                else:
                    if chunk.data:
                        def _append() -> None:
                            with cache.open("ab") as fh:
                                fh.write(chunk.data)
                        await asyncio.to_thread(_append)
        except SandboxClosed as exc:
            # 控制面确认终态：沙箱没了，但已缓存的内容仍然有价值。
            sandbox_alive = False
            note = str(exc)
            _clients.pop(env_id, None)
        except SandboxUnreachable as exc:
            # 分不清沙箱刚关还是网络抖动 —— 按可重试处理，别把 tab 判死。
            note = f"暂时连不上沙箱，重连中：{exc}"
            _clients.pop(env_id, None)
        except LogExportError as exc:
            note = f"探测失败：{exc}"
            _clients.pop(env_id, None)

    # 统一从本地缓存回读：缓存即事实来源。
    if not cache.exists():
        return {"text": "", "next_offset": 0, "size": 0,
                "task_running": True, "sandbox_alive": sandbox_alive,
                "truncated": False, "note": note or "暂无内容"}

    size = cache.stat().st_size
    truncated = offset > size          # 远端重建后本地变小 → 前端清屏
    start = 0 if truncated else offset

    def _read() -> bytes:
        with cache.open("rb") as fh:
            fh.seek(start)
            return fh.read(CHUNK_BYTES)

    data = await asyncio.to_thread(_read)

    # 按 CHUNK_BYTES 截断可能切断一个多字节 UTF-8 字符。直接 errors="replace"
    # 会把它变成永久的 '�'（下一轮从被切断处之后继续，坏字节再也补不回来）。
    # 把尾部不完整的那几个字节退回去，留给下一轮与后续字节拼成完整字符。
    if len(data) == CHUNK_BYTES:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            data = data[: exc.start]

    return {
        "text": data.decode("utf-8", errors="replace"),
        "next_offset": start + len(data),
        "size": size,
        "task_running": True,
        "sandbox_alive": sandbox_alive,
        "truncated": truncated,
        "note": note,
    }
