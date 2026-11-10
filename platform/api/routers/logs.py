import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from ..core.config import settings
from ..core.database import get_connection, async_execute
from ..core.security import get_current_user
from ..services.secrets import instance_create_params, redact_text

router = APIRouter(prefix="/api/logs", tags=["logs"])

# obs-download 需要脱敏的文本类扩展名。只列「可能承载 key」的配置/数据格式：
# models.json、config.yaml 这类是后端用 create_params 里的 key 填出来的。
# .log 一并纳入是兜底 —— 正文通常不含 key，替换对它是空操作。
_REDACT_DOWNLOAD_EXTS = {
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini",
    ".env", ".conf", ".cfg", ".txt", ".log", ".md", ".sh",
}


def _get_output_dir(config_path: str) -> str:
    config_basename = Path(config_path).stem
    instance_dir = str(Path(config_path).parent)
    return os.path.join(instance_dir, "outputs", config_basename)


def _get_instance(instance_id: str) -> dict:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="实例不存在")
    return dict(row)


_LOGGER_RE = re.compile(r'\|(?:INFO|WARNING|ERROR|DEBUG)\|[^|]*\|[^|]*\|(.+)', re.DOTALL)
_NODE_ID_RE = re.compile(r'^\[node_id:\d+\][^|]*\|')
_SSE_STDOUT_STDERR_RE = re.compile(r',\s*stdout:\s*\[(.*)\],\s*stderr:\s*\[(.*)\]$', re.DOTALL)
_SSE_NO_RESP_RE = re.compile(r'sse data:.*no response')
_SSE_CMD_RE = re.compile(r'sse data:\s*\[#(\d+)\]\[([^\]]*:\[.+?\])\]')


def _join_multiline(lines: list[str]) -> list[str]:
    """SSE stdout/stderr 跨多行，把非 [node_id:] 开头的行合并到上一行。"""
    joined = []
    for line in lines:
        if line.startswith('[node_id:') or not joined:
            joined.append(line)
        else:
            joined[-1] += '\n' + line
    return joined


def _extract_sse_payload(line: str) -> Optional[tuple[str, str, str]]:
    """从一条已 join 的日志行中提取 (cmd_key, stdout, stderr)，非 SSE 行返回 None。"""
    cleaned = line
    m = _LOGGER_RE.search(cleaned)
    if m:
        cleaned = m.group(1)
    cleaned = _NODE_ID_RE.sub('', cleaned).strip()

    if _SSE_NO_RESP_RE.search(cleaned):
        return None
    if 'sse data:' not in cleaned:
        return None

    m_cmd = _SSE_CMD_RE.search(cleaned)
    cmd_key = m_cmd.group(2) if m_cmd else ''

    m_data = _SSE_STDOUT_STDERR_RE.search(cleaned)
    if not m_data:
        return None
    stdout = m_data.group(1) or ''
    stderr = m_data.group(2) or ''
    return (cmd_key, stdout, stderr)


def _concat_sse_groups(lines: list[str]) -> list[str]:
    """把同一命令的连续 SSE stdout/stderr 拼接成完整文本，然后按行输出。"""
    result = []
    current_cmd = None
    stdout_buf = []
    stderr_buf = []

    def _flush():
        text = ''.join(stdout_buf)
        seen_out = set()
        for out_line in text.splitlines():
            stripped = out_line.strip()
            if stripped and stripped not in seen_out:
                seen_out.add(stripped)
                result.append(f'[STDOUT] {stripped}')
        err_text = ''.join(stderr_buf)
        seen_err = set()
        for err_line in err_text.splitlines():
            stripped = err_line.strip()
            if stripped and stripped != 'None' and stripped not in seen_err:
                seen_err.add(stripped)
                result.append(f'[STDERR] {stripped}')

    for line in lines:
        payload = _extract_sse_payload(line)
        if payload is not None:
            cmd_key, stdout, stderr = payload
            if cmd_key != current_cmd and current_cmd is not None:
                _flush()
                stdout_buf.clear()
                stderr_buf.clear()
            current_cmd = cmd_key
            if stdout and stdout != 'None':
                stdout_buf.append(stdout)
            if stderr and stderr != 'None':
                stderr_buf.append(stderr)
        else:
            if current_cmd is not None:
                _flush()
                stdout_buf.clear()
                stderr_buf.clear()
                current_cmd = None
            cleaned = line
            m = _LOGGER_RE.search(cleaned)
            if m:
                cleaned = m.group(1)
            cleaned = _NODE_ID_RE.sub('', cleaned).strip()
            if _SSE_NO_RESP_RE.search(cleaned):
                continue
            cleaned = cleaned.replace('\n', ' ').strip()
            if cleaned:
                result.append(cleaned)

    if current_cmd is not None:
        _flush()

    return result


@router.get("/{instance_id}/main")
async def get_main_log(
    instance_id: str,
    tail: int = Query(default=200, description="返回最后N行"),
    task_filter: str = Query(default="", description="按 env_id 或 config_name 过滤"),
    mode: str = Query(default="verbose", description="verbose 或 concise"),
    source: str = Query(default="main", description="main 优先读 logs/main.log，nohup 读 nohup.log"),
    user: dict = Depends(get_current_user),
):
    inst = _get_instance(instance_id)
    output_dir = _get_output_dir(inst["config_path"])

    structured_log = os.path.join(output_dir, "logs", "main.log")
    legacy_log = os.path.join(output_dir, "nohup.log")

    if source == "nohup" or task_filter:
        log_file = legacy_log
    elif os.path.exists(structured_log):
        log_file = structured_log
    else:
        log_file = legacy_log

    if not os.path.exists(log_file):
        return {"lines": [], "total_lines": 0}

    if task_filter:
        lines = await _extract_task_lines(log_file, task_filter)
    else:
        async with aiofiles.open(log_file, "r", errors="replace") as f:
            all_lines = await f.readlines()
        lines = [l.rstrip("\n") for l in all_lines]

    if mode == "concise":
        lines = _join_multiline(lines)
        lines = _concat_sse_groups(lines)

    total = len(lines)
    lines = lines[-tail:] if tail < total else lines
    return {"lines": lines, "total_lines": total}


async def _extract_task_lines(log_file: str, task_filter: str) -> list[str]:
    """
    按 Worker 区间提取某个任务的完整日志。
    Worker 从 "Worker X starting task Y: config_name" 开始，
    到 "Worker X finished task Y" 或 "Worker X starting task Z" 结束。
    """
    start_re = re.compile(r"Worker (\d+) starting task (\d+): (.+?) =")
    env_re = re.compile(r"Task (\d+): env=(\w+)")
    finish_re = re.compile(r"Worker (\d+) (?:finished|error on) task")

    # 第一遍：找到目标任务对应的 worker_id 和 task_idx
    target_workers = set()
    async with aiofiles.open(log_file, "r", errors="replace") as f:
        async for line in f:
            m = start_re.search(line)
            if m and task_filter in line:
                target_workers.add((m.group(1), m.group(2)))
            m = env_re.search(line)
            if m and task_filter in line:
                target_workers.add((None, m.group(1)))

    if not target_workers:
        return []

    target_task_idxs = {t[1] for t in target_workers}

    # 第二遍：按 Worker 区间提取日志
    result = []
    active_workers = {}
    async with aiofiles.open(log_file, "r", errors="replace") as f:
        async for line in f:
            stripped = line.rstrip("\n")

            m = start_re.search(stripped)
            if m:
                worker_id, task_idx = m.group(1), m.group(2)
                if task_idx in target_task_idxs:
                    active_workers[worker_id] = True
                    result.append(stripped)
                    continue
                else:
                    active_workers.pop(worker_id, None)

            m = finish_re.search(stripped)
            if m:
                worker_id = m.group(1)
                if worker_id in active_workers:
                    result.append(stripped)
                    active_workers.pop(worker_id, None)
                    continue

            if active_workers:
                for wid in list(active_workers):
                    if f"Worker {wid}" in stripped or f"Task " in stripped:
                        result.append(stripped)
                        break
                else:
                    if any(active_workers.values()):
                        result.append(stripped)

    return result


@router.get("/{instance_id}/clean")
async def get_clean_log(
    instance_id: str,
    tail: int = Query(default=100),
    user: dict = Depends(get_current_user),
):
    inst = _get_instance(instance_id)
    output_dir = _get_output_dir(inst["config_path"])
    log_file = os.path.join(output_dir, "nohup_clean.log")
    if not os.path.exists(log_file):
        return {"lines": [], "total_lines": 0}

    async with aiofiles.open(log_file, "r", errors="replace") as f:
        all_lines = await f.readlines()

    total = len(all_lines)
    lines = all_lines[-tail:] if tail < total else all_lines
    return {"lines": [l.rstrip("\n") for l in lines], "total_lines": total}


@router.get("/{instance_id}/tasks")
async def list_log_tasks(
    instance_id: str,
    user: dict = Depends(get_current_user),
):
    """解析 nohup.log 提取所有任务的 task_idx / env_id / config_name。"""
    inst = _get_instance(instance_id)
    output_dir = _get_output_dir(inst["config_path"])
    log_file = os.path.join(output_dir, "nohup.log")
    if not os.path.exists(log_file):
        return {"tasks": []}

    # 匹配: "Worker {w} starting task {idx}: {config_name}"
    start_pattern = re.compile(r"Worker (\d+) starting task (\d+): (.+?) =")
    # 匹配: "Task {idx}: env={env_id}"
    env_pattern = re.compile(r"Task (\d+): env=(\w+)")

    tasks = {}
    async with aiofiles.open(log_file, "r", errors="replace") as f:
        async for line in f:
            m = start_pattern.search(line)
            if m:
                idx = m.group(2)
                tasks.setdefault(idx, {"task_idx": idx, "config_name": m.group(3), "env_id": ""})
            m = env_pattern.search(line)
            if m:
                idx = m.group(1)
                tasks.setdefault(idx, {"task_idx": idx, "config_name": "", "env_id": ""})
                tasks[idx]["env_id"] = m.group(2)

    sorted_tasks = sorted(tasks.values(), key=lambda t: int(t["task_idx"]))
    return {"tasks": sorted_tasks}


@router.get("/{instance_id}/task-log-list")
async def list_task_logs(
    instance_id: str,
    user: dict = Depends(get_current_user),
):
    inst = _get_instance(instance_id)
    logs_dir = os.path.join(_get_output_dir(inst["config_path"]), "logs")
    if not os.path.isdir(logs_dir):
        return {"files": [], "mtimes": {}}
    names = [f for f in os.listdir(logs_dir) if f.startswith("task-") and f.endswith(".log")]
    files = sorted(
        names,
        key=lambda f: int(m.group()) if (m := re.search(r'\d+', f)) else 0,
    )

    # mtime 用于前端左栏的「执行顺序」视图：task_records 里没有任何开始/结束
    # 时间戳，日志文件的最后写入时间是现成的近似——它代表该 task **最后一次
    # 产生输出**的时刻，约等于完成时间，不是开始时间。并发跑时同一批 task 的
    # mtime 会很接近，所以这个顺序是「谁先收尾」而非严格的调度顺序。
    # 取不到 stat 的文件（并发清理等）直接跳过，不让整个接口 500。
    mtimes = {}
    for f in files:
        try:
            mtimes[f] = os.path.getmtime(os.path.join(logs_dir, f))
        except OSError:
            pass
    return {"files": files, "mtimes": mtimes}


def _tail_bytes(log_file: str, tail: int, chunk_size: int = 65536, end: int = -1) -> tuple[str, int, int]:
    """从文件里取「结束于字节 end 之前的最后 tail 行」，反向分块读，读取量与 tail 成正比、与文件总大小无关。
    end<0 表示从文件末尾（EOF）起。返回 (text, 返回的行数, start_offset)，
    其中 start_offset = 返回窗口首字节在文件中的偏移，是上一页翻页的下一个 end；start_offset==0 表示已到文件头。"""
    with open(log_file, "rb") as f:
        f.seek(0, os.SEEK_END)
        file_size = f.tell()
        end_pos = file_size if end is None or end < 0 else min(end, file_size)
        blocks = []
        newlines = 0
        pos = end_pos
        # 需要 tail 行 -> 累积到 tail 个换行符即可覆盖 end_pos 之前的最后 tail 行（含末行）
        while pos > 0 and newlines <= tail:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            block = f.read(read_size)
            blocks.append(block)
            newlines += block.count(b"\n")
        data = b"".join(reversed(blocks))  # data 覆盖文件区间 [pos, end_pos)
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    if len(lines) > tail:
        dropped = "".join(lines[:-tail])
        # 丢弃的前缀字节数即返回窗口相对 pos 的偏移；用同样的 replace 解码保证字节数自洽
        start_offset = pos + len(dropped.encode("utf-8", "replace"))
        lines = lines[-tail:]
    else:
        start_offset = pos  # 已读到文件头
    return "".join(lines), len(lines), start_offset


_LOG_FILENAME_RE = re.compile(r'^(task-\d+|main|nohup)\.log$')


def _resolve_log_file(instance_id: str, filename: str) -> str:
    """校验日志文件名并解析出绝对路径。nohup.log 在 output_dir 下，其余在 logs/ 下。

    文件名白名单同时起到路径穿越防护的作用 —— 正则不接受 '/' 与 '..'。
    """
    if not _LOG_FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="无效的日志文件名")
    inst = _get_instance(instance_id)
    output_dir = _get_output_dir(inst["config_path"])
    if filename == "nohup.log":
        return os.path.join(output_dir, filename)
    return os.path.join(output_dir, "logs", filename)


@router.get("/{instance_id}/task-log/{filename}")
async def get_task_log(
    instance_id: str,
    filename: str,
    tail: int = Query(default=200),
    mode: str = Query(default="verbose"),
    raw: bool = Query(default=False, description="true 时返回原始文本，不切行、不改换行"),
    end: int = Query(default=-1, description="raw+tail>0 时的字节游标：取结束于该字节之前的最后 tail 行；<0 表示从 EOF 起"),
    user: dict = Depends(get_current_user),
):
    log_file = _resolve_log_file(instance_id, filename)
    if not os.path.exists(log_file):
        return {"text": "", "lines": [], "total_lines": 0}
    if raw:
        if tail and tail > 0:
            # 只反向读取尾部（可带字节游标 end 向上翻页），避免把整个大文件读进内存
            text, cnt, start_offset = await asyncio.to_thread(_tail_bytes, log_file, tail, 65536, end)
            return {"text": text, "total_lines": cnt, "start_offset": start_offset, "has_more": start_offset > 0}
        # tail<=0：确实要全量，才整文件读
        async with aiofiles.open(log_file, "rb") as f:
            data = await f.read()
        text = data.decode("utf-8", errors="replace")
        total = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        return {"text": text, "total_lines": total}

    async with aiofiles.open(log_file, "r", errors="replace") as f:
        all_lines = await f.readlines()
    lines = [l.rstrip("\n") for l in all_lines]

    if mode == "concise":
        lines = _join_multiline(lines)
        lines = _concat_sse_groups(lines)

    total = len(lines)
    lines = lines[-tail:] if tail < total else lines
    return {"lines": lines, "total_lines": total}


# ============================================================================
# 关键事件抽取（日志中心右栏时间线）
# ============================================================================
# hive.py 在每个任务的生命周期里打了一组固定格式的锚点行，这里把它们抽成时间线。
# 全部锚点均在真实日志上实测过：开始 → 环境就绪 → 各阶段下载 → gateway 就绪
# → [错误] → finalize → 终态行。
#
# 不做的：重试事件。重试发生在调度层，单个 task-<idx>.log 里没有记录。

# 标准日志行：时间|文件:行号|函数|pid|tid|线程|级别|正文
_LOG_LINE_RE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\|'
    r'[^|]*\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|'
    r'(?P<level>INFO|WARNING|ERROR|DEBUG)\|'
    r'(?P<body>.*)$'
)

# 正文锚点规则：(正则, kind, 标题生成器)。顺序匹配，首个命中即停。
# 顺序有讲究：终态行正文里也含 error_code=，必须排在泛化的错误行规则之前。
_EVENT_RULES: list[tuple[re.Pattern, str, object]] = [
    (re.compile(r'===== 任务执行状态=(?P<status>\S+)\s+error_code=(?P<code>\S+)'
                r'.*?elapsed=(?P<elapsed>[\d.]+)s'),
     "end", lambda m: m.group('status')),
    (re.compile(r'===== BEGIN config=(?P<config>\S+)(?:\s+framework=(?P<fw>\S+))?'),
     "begin", lambda m: "任务开始"),
    (re.compile(r'>>>>>> Task (?P<idx>\d+): env_id=(?P<env>\S+)\s+elapsed=(?P<elapsed>[\d.]+)s'),
     "env_ready", lambda m: "环境就绪"),
    (re.compile(r'Downloaded skills \((?P<info>[^)]*)\) in (?P<elapsed>[\d.]+)s'),
     "stage", lambda m: "Skill 下载完成"),
    (re.compile(r'Downloaded user profile in (?P<elapsed>[\d.]+)s'),
     "stage", lambda m: "用户配置下载完成"),
    (re.compile(r'Downloaded agents in (?P<elapsed>[\d.]+)s'),
     "stage", lambda m: "Agents 下载完成"),
    (re.compile(r'Code extracted successfully'),
     "stage", lambda m: "代码解压完成"),
    (re.compile(r'Gateway started on port (?P<port>\d+)'),
     "stage", lambda m: "Gateway 就绪"),
    (re.compile(r'Script completed: (?P<file>\S+)'),
     "stage", lambda m: "主脚本执行完成"),
    (re.compile(r'finalize=BEGIN\s+name=(?P<name>\S+)'),
     "finalize", lambda m: f"收尾开始：{m.group('name')}"),
    (re.compile(r'finalize=END\s+name=(?P<name>\S+)\s+result=(?P<result>\S+)'),
     "finalize", lambda m: f"收尾结束：{m.group('name')}"),
    # 错误行：错误码就在正文行首方括号里
    (re.compile(r'\[(?P<code>[CSTX]\d{3})\]'),
     "error", lambda m: f"错误 {m.group('code')}"),
]

# sandbox_detail 方框块边界。块内是沙箱原始 stdout/stderr（自带时间戳、含
# [gateway] security warning 之类），逐行匹配会污染时间线，必须整块跳过。
_BOX_OPEN = "┌"
_BOX_CLOSE = "└"


def _scan_events(log_file: str, max_bytes: int = 8 * 1024 * 1024,
                 max_events: int = 500) -> dict:
    """扫整个日志文件抽取关键事件。返回 {events, summary, truncated}。

    task 日志实测仅 50～60 行，整文件顺序扫即可（无需 _tail_bytes，那是翻页用的）。
    main.log / nohup.log 是全实例编排日志（实测 15.9MB、上万个锚点），时间线本就
    不适用于它们，用 max_bytes / max_events 双重兜底，避免回传巨量无用数据。
    """
    events: list[dict] = []
    # env_id 供前端判断「该任务是否还活着、能不能看沙箱实时日志」：
    # 有 env_id（沙箱已建）且 status 为空（没出现 任务执行状态= 行）== 运行中。
    # task_records.status 做不到这个判断 —— 运行中和未执行都是 NULL。
    summary: dict = {"status": None, "error_code": None, "total_elapsed": None,
                     "config": None, "framework": None, "started_at": None,
                     "ended_at": None, "env_id": None}
    in_box = False
    read = 0
    truncated = False

    with open(log_file, "r", errors="replace") as f:
        for lineno, line in enumerate(f, start=1):
            read += len(line)
            if read > max_bytes or len(events) >= max_events:
                truncated = True
                break
            # 方框块整体跳过（块内行不参与任何匹配）
            stripped = line.lstrip()
            if stripped.startswith(_BOX_OPEN):
                in_box = True
                continue
            if in_box:
                if stripped.startswith(_BOX_CLOSE):
                    in_box = False
                continue

            lm = _LOG_LINE_RE.match(line)
            if not lm:
                continue
            body = lm.group("body")
            for pattern, kind, title_fn in _EVENT_RULES:
                m = pattern.search(body)
                if not m:
                    continue
                groups = m.groupdict()
                ev = {
                    "ts": lm.group("ts"),
                    "level": lm.group("level"),
                    "kind": kind,
                    "title": title_fn(m),
                    "line": lineno,
                    "elapsed": float(groups["elapsed"]) if groups.get("elapsed") else None,
                    "code": groups.get("code") if groups.get("code") != "-" else None,
                    "detail": "",
                }
                if kind == "begin":
                    ev["detail"] = groups.get("config") or ""
                    summary["config"] = groups.get("config")
                    summary["framework"] = groups.get("fw")
                    summary["started_at"] = lm.group("ts")
                elif kind == "env_ready":
                    ev["detail"] = f"env_id={groups.get('env', '')}"
                    summary["env_id"] = groups.get("env") or None
                elif kind == "stage":
                    ev["detail"] = groups.get("info") or groups.get("port") or ""
                elif kind == "finalize":
                    ev["detail"] = f"result={groups['result']}" if groups.get("result") else ""
                elif kind == "error":
                    # 错误正文去掉前缀码，保留可读原因
                    ev["detail"] = body.split(f"[{groups['code']}]", 1)[-1].strip()[:200]
                    # 同一个错误码常被打两次：raise 点一次、run() 的 except handler
                    # 再包一次（实测 task-1216 的 S003 出现在 L32/L39）。时间线只留
                    # 第一次——它更靠近真正的出错位置，detail 也更干净。
                    if events and events[-1]["kind"] == "error" \
                            and events[-1]["code"] == ev["code"]:
                        break
                elif kind == "end":
                    ev["detail"] = f"error_code={groups.get('code')}" \
                        if groups.get("code") not in (None, "-") else ""
                    summary["status"] = groups.get("status")
                    summary["error_code"] = ev["code"]
                    summary["total_elapsed"] = ev["elapsed"]
                    summary["ended_at"] = lm.group("ts")
                events.append(ev)
                break

    return {"events": events, "summary": summary, "truncated": truncated}


@router.get("/{instance_id}/task-log/{filename}/events")
async def get_task_log_events(
    instance_id: str,
    filename: str,
    user: dict = Depends(get_current_user),
):
    """抽取单个任务日志的关键事件时间线（供日志中心右栏）。"""
    log_file = _resolve_log_file(instance_id, filename)
    if not os.path.exists(log_file):
        return {"events": [], "summary": {}, "truncated": False}
    return await asyncio.to_thread(_scan_events, log_file)


@router.websocket("/ws/{instance_id}")
async def websocket_log_stream(websocket: WebSocket, instance_id: str):
    """WebSocket endpoint for real-time log streaming."""
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="缺少token")
        return

    from jose import JWTError, jwt as jose_jwt
    from ..core.config import settings as app_settings
    try:
        jose_jwt.decode(token, app_settings.SECRET_KEY, algorithms=[app_settings.ALGORITHM])
    except JWTError:
        await websocket.close(code=4001, reason="token无效")
        return

    await websocket.accept()

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_instances WHERE id=?", (instance_id,)).fetchone()
    if not row:
        await websocket.send_json({"error": "实例不存在"})
        await websocket.close()
        return

    inst = dict(row)
    output_dir = _get_output_dir(inst["config_path"])
    log_file = os.path.join(output_dir, "nohup.log")

    try:
        while True:
            if not os.path.exists(log_file):
                await websocket.send_json({"type": "waiting", "message": "等待日志文件..."})
                await asyncio.sleep(2)
                continue

            async with aiofiles.open(log_file, "r", errors="replace") as f:
                await f.seek(0, 2)
                while True:
                    line = await f.readline()
                    if line:
                        await websocket.send_json({"type": "log", "data": line.rstrip("\n")})
                    else:
                        await asyncio.sleep(0.5)
                        try:
                            await asyncio.wait_for(websocket.receive_text(), timeout=0.01)
                        except asyncio.TimeoutError:
                            pass
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass


@router.get("/{instance_id}/obs-logs")
async def list_obs_logs(
    instance_id: str,
    user: dict = Depends(get_current_user),
):
    """List log files on OBS for a completed task instance."""
    inst = _get_instance(instance_id)

    config_path = inst["config_path"]
    if not os.path.exists(config_path):
        raise HTTPException(status_code=404, detail="配置文件不存在")

    from omegaconf import OmegaConf
    cfg = OmegaConf.load(config_path)
    traj_bucket = cfg.run_config.obs.traj_save_bucket
    traj_path = cfg.run_config.obs.traj_save_path
    obs_path = f"{traj_bucket}/{traj_path}/"

    proc = await asyncio.create_subprocess_exec(
        settings.OBSUTIL_PATH, "ls", obs_path, "-limit=0",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)

    items = []
    for line in stdout.decode().splitlines():
        if "obs://" in line:
            parts = line.split()
            for p in parts:
                if p.startswith("obs://"):
                    name = p.replace(obs_path, "").strip("/")
                    if name:
                        items.append({"name": name, "path": p, "is_dir": p.endswith("/")})
    return {"obs_path": obs_path, "items": items}


# 缓存: instance_id -> (timestamp, dirs)
_obs_tree_cache: dict[str, tuple[float, list[str]]] = {}
_OBS_TREE_TTL = 10  # 秒


@router.get("/{instance_id}/obs-tree")
async def list_obs_tree(
    instance_id: str,
    refresh: bool = Query(default=False),
    user: dict = Depends(get_current_user),
):
    """用 obsutil ls -d 列出一级目录，返回目录名列表。结果缓存10秒。"""
    now = time.time()
    if not refresh and instance_id in _obs_tree_cache:
        cached_time, cached_dirs = _obs_tree_cache[instance_id]
        if now - cached_time < _OBS_TREE_TTL:
            inst = _get_instance(instance_id)
            obs_path = _get_obs_base_path(inst)
            return {"obs_path": obs_path, "dirs": cached_dirs}

    inst = _get_instance(instance_id)
    obs_path = _get_obs_base_path(inst)

    dirs = await _list_obs_dirs(obs_path)

    _obs_tree_cache[instance_id] = (now, dirs)
    return {"obs_path": obs_path, "dirs": dirs}


# 缓存: (instance_id, subdir) -> (timestamp, items)
_obs_subtree_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_OBS_SUBTREE_TTL = 30  # 秒


@router.get("/{instance_id}/obs-subtree")
async def list_obs_subtree(
    instance_id: str,
    subdir: str = Query(description="一级子目录名"),
    refresh: bool = Query(default=False),
    user: dict = Depends(get_current_user),
):
    """用 obsutil ls 列出某个子目录下所有文件，返回扁平路径列表。结果缓存30秒。"""
    now = time.time()
    cache_key = (instance_id, subdir)
    if not refresh and cache_key in _obs_subtree_cache:
        cached_time, cached_items = _obs_subtree_cache[cache_key]
        if now - cached_time < _OBS_SUBTREE_TTL:
            return {"items": cached_items}

    inst = _get_instance(instance_id)
    obs_path = _get_obs_base_path(inst)
    subdir_path = f"{obs_path}{subdir}/"

    proc = await asyncio.create_subprocess_exec(
        settings.OBSUTIL_PATH, "ls", subdir_path, "-limit=0",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)

    items = []
    for line in stdout.decode().splitlines():
        line = line.strip()
        if not line.startswith("obs://"):
            continue
        parts = line.split()
        full_path = parts[0]
        name = full_path.replace(subdir_path, "").strip("/")
        if not name:
            continue
        is_dir = full_path.endswith("/")
        if is_dir:
            name = name.rstrip("/")
        size = None
        size_match = re.search(r'(\d+(?:\.\d+)?(?:B|KB|MB|GB|TB))', line)
        if size_match:
            size = size_match.group(1)
        items.append({"name": name, "path": full_path, "is_dir": is_dir, "size": size})

    _obs_subtree_cache[cache_key] = (now, items)
    return {"items": items}


@router.get("/{instance_id}/obs-download")
async def download_obs_log(
    instance_id: str,
    file_path: str = Query(description="OBS上的文件路径"),
    user: dict = Depends(get_current_user),
):
    """Download a specific log file from OBS."""
    inst = _get_instance(instance_id)

    if not file_path.startswith("obs://"):
        raise HTTPException(status_code=400, detail="无效的OBS路径")

    tmp_dir = os.path.join(settings.HIVE_ROOT, "platform", "tmp", instance_id)
    os.makedirs(tmp_dir, exist_ok=True)
    filename = os.path.basename(file_path.rstrip("/"))
    local_path = os.path.join(tmp_dir, filename)

    proc = await asyncio.create_subprocess_exec(
        settings.OBSUTIL_PATH, "cp", file_path, local_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=120)

    if not os.path.exists(local_path):
        raise HTTPException(status_code=404, detail="文件下载失败")

    # 下载通道同样能取到 models.json / config.yaml 这类含 key 的文本文件，
    # 与 obs-view 同口径脱敏后再下发。产物可能是 pptx/zip 等二进制，
    # 对它们做文本替换会损坏文件，故只处理文本类扩展名；
    # 二进制产物本来也不是 key 的载体。
    if os.path.splitext(filename)[1].lower() in _REDACT_DOWNLOAD_EXTS:
        try:
            async with aiofiles.open(local_path, "r", errors="replace") as f:
                raw = await f.read()
            safe = redact_text(raw, instance_create_params(instance_id))
            if safe != raw:
                # 原地改会污染缓存文件（下次命中就是脱敏版，operator 也拿不到原文），
                # 故另写一份脱敏副本再下发。
                local_path = local_path + ".redacted"
                async with aiofiles.open(local_path, "w") as f:
                    await f.write(safe)
        except (OSError, UnicodeDecodeError):
            pass  # 读不成文本就按原样下发（扩展名判断可能误判）

    return FileResponse(local_path, filename=filename)


@router.get("/{instance_id}/obs-view")
async def view_obs_log(
    instance_id: str,
    file_path: str = Query(description="OBS上的文件路径"),
    offset: int = Query(default=0, ge=0, description="从第几个字符开始（0-based），按字符从头分页"),
    limit: int = Query(default=200000, ge=1, le=2000000, description="本页返回的字符数"),
    tail: int = Query(default=0, ge=0, description="兼容旧调用：>0 时返回末尾N行，忽略 offset/limit"),
    user: dict = Depends(get_current_user),
):
    """View log content from OBS without downloading.

    文件整体下载到本地 tmp，再按【字符】offset/limit 从头分页返回，配合前端「滚到底加载更多」。
    按字符（而非行）分页，是因为 trajectory.jsonl / models.json 这类文件常常行数很少但
    单行长达十几万字符，按行分页无法拆分、前端渲染整坨超长单行会卡死。
    传 tail>0 时保持旧的「返回末尾N行」语义（兼容未合并的旧页面）。
    """
    inst = _get_instance(instance_id)

    if not file_path.startswith("obs://"):
        raise HTTPException(status_code=400, detail="无效的OBS路径")

    tmp_dir = os.path.join(settings.HIVE_ROOT, "platform", "tmp", instance_id)
    os.makedirs(tmp_dir, exist_ok=True)
    filename = os.path.basename(file_path.rstrip("/"))
    local_path = os.path.join(tmp_dir, filename)

    proc = await asyncio.create_subprocess_exec(
        settings.OBSUTIL_PATH, "cp", file_path, local_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=120)

    if not os.path.exists(local_path):
        raise HTTPException(status_code=404, detail="文件下载失败")

    async with aiofiles.open(local_path, "r", errors="replace") as f:
        text = await f.read()

    # 本接口能读实例产物目录下的任意文件，其中 models.json / config.yaml 这类
    # 是后端拿 create_params 里的 key 填出来的，原样下发等于绕开实例详情
    # 「配置文件」tab 的脱敏。故与那里同口径：全文替换掉本实例的 key 原文。
    # 对所有角色生效（与 /configs/{filename} 一致）—— 日志正文不含 key，
    # 替换对它是空操作，不影响排查。
    text = redact_text(text, instance_create_params(instance_id))

    total_chars = len(text)
    total_lines = text.count("\n") + 1 if text else 0

    if tail > 0:
        # 旧语义：末尾 N 行
        all_lines = text.splitlines(keepends=True)
        window = all_lines[-tail:] if tail < len(all_lines) else all_lines
        return {
            "lines": [l.rstrip("\n") for l in window],
            "total_lines": len(all_lines),
            "file": filename,
        }

    # 全量返回整个文件内容（不分页）
    return {
        "content": text,
        "offset": 0,
        "next_offset": total_chars,
        "has_more": False,
        "total_chars": total_chars,
        "total_lines": total_lines,
        "file": filename,
    }


def _get_obs_base_path(inst: dict) -> str:
    config_path = inst["config_path"]
    if not os.path.exists(config_path):
        raise HTTPException(status_code=404, detail="配置文件不存在")
    from omegaconf import OmegaConf
    cfg = OmegaConf.load(config_path)
    traj_bucket = cfg.run_config.obs.traj_save_bucket
    traj_path = cfg.run_config.obs.traj_save_path
    return f"{traj_bucket}/{traj_path}/"


# 内存缓存
_task_completed_cache: dict[str, dict[str, bool]] = {}  # instance_id -> {task: True/False}


async def _list_obs_dirs(obs_base: str) -> list[str]:
    """用 obsutil ls -d 列出 OBS 目录下所有子目录名。"""
    proc = await asyncio.create_subprocess_exec(
        settings.OBSUTIL_PATH, "ls", obs_base, "-d", "-limit=0",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    dirs = []
    for line in stdout.decode().splitlines():
        line = line.strip()
        if not line.startswith("obs://"):
            continue
        full_path = line.split()[0]
        if not full_path.endswith("/"):
            continue
        name = full_path.replace(obs_base, "").strip("/")
        if name and not name.startswith("."):
            dirs.append(name)
    return dirs


_EVAL_SCORE_FILE = "_eval_score.json"
_TRAJ_SCORE_FILE = "_traj_score.json"


async def _download_eval_score(obs_base: str, task_dir: str, tmp_dir: str,
                               semaphore: asyncio.Semaphore) -> tuple[str, dict | None]:
    """下载单个任务的 evaluator_use.log，提取评估明细并自行计算分数。"""
    async with semaphore:
        eval_path = f"{obs_base}{task_dir}/logs/evaluator_use.log"
        local_file = os.path.join(tmp_dir, f"eval_{task_dir}.log")
        try:
            proc = await asyncio.create_subprocess_exec(
                settings.OBSUTIL_PATH, "cp", eval_path, local_file,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=60)

            if not os.path.exists(local_file):
                return (task_dir, None)

            last_eval = None
            async with aiofiles.open(local_file, "r", errors="replace") as f:
                async for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    evaluation = record.get("evaluation")
                    if evaluation and isinstance(evaluation, dict) and "completion" in evaluation:
                        last_eval = evaluation

            if last_eval is None:
                return (task_dir, None)

            # eval_score 定义为 evaluator 给出完成度的评分（evaluation.completion），
            # 与 task_traj_records.completion 同源。不再用 gate_status/bucket_scores
            # 那套「gate × 桶加权」累加公式——它算的是 rubric 加权通过率，语义与
            # evaluator 评分不同，且新版 evaluator log 常无 bucket 明细，公式恒得 0。
            completion = last_eval.get("completion")
            try:
                completion = float(completion) if completion is not None else None
            except (ValueError, TypeError):
                completion = None

            return (task_dir, {
                "score": completion,
                "completion": completion,
                "gate": last_eval.get("gate"),
            })
        except Exception:
            return (task_dir, None)
        finally:
            try:
                os.remove(local_file)
            except OSError:
                pass


async def _check_run_completed(obs_base: str, task_dir: str, tmp_dir: str,
                               semaphore: asyncio.Semaphore) -> tuple[str, bool]:
    """下载 {task_dir}/workdir/run.log，检查是否包含 '所有任务执行完成!'。"""
    async with semaphore:
        run_path = f"{obs_base}{task_dir}/workdir/run.log"
        local_file = os.path.join(tmp_dir, f"run_{task_dir}.log")
        try:
            proc = await asyncio.create_subprocess_exec(
                settings.OBSUTIL_PATH, "cp", run_path, local_file,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=60)

            if not os.path.exists(local_file):
                return (task_dir, False)

            async with aiofiles.open(local_file, "r", errors="replace") as f:
                async for line in f:
                    if "所有任务执行完成!" in line:
                        return (task_dir, True)
            return (task_dir, False)
        except Exception:
            return (task_dir, False)
        finally:
            try:
                os.remove(local_file)
            except OSError:
                pass


@router.get("/{instance_id}/eval-stats")
async def get_eval_stats(
    instance_id: str,
    refresh: bool = Query(default=False, description="强制刷新全部缓存"),
    user: dict = Depends(get_current_user),
):
    """扫描每个任务目录的 evaluator_use.log 和 run.log，返回 per-task 分数和完成状态。"""
    inst = _get_instance(instance_id)
    obs_base = _get_obs_base_path(inst)

    # 本地缓存文件路径: instances/<id>/_eval_score.json
    inst_dir = str(Path(inst["config_path"]).parent)
    local_cache_file = os.path.join(inst_dir, _EVAL_SCORE_FILE)

    # 1) 尝试读本地缓存（非全量刷新时）
    cached_scores: dict[str, dict] = {}
    if not refresh and os.path.exists(local_cache_file):
        try:
            async with aiofiles.open(local_cache_file, "r") as f:
                cached_scores = json.loads(await f.read())
        except Exception:
            cached_scores = {}

    if refresh:
        _task_completed_cache.pop(instance_id, None)
    cached_completed = _task_completed_cache.get(instance_id, {})

    all_dirs = await _list_obs_dirs(obs_base)
    task_dirs = [d for d in all_dirs if d != "logs"]

    new_dirs_scores = [d for d in task_dirs if d not in cached_scores]
    new_dirs_completed = [d for d in task_dirs if d not in cached_completed]

    tmp_dir = os.path.join(settings.HIVE_ROOT, "platform", "tmp", instance_id)
    os.makedirs(tmp_dir, exist_ok=True)
    semaphore = asyncio.Semaphore(10)

    all_tasks = []
    for d in new_dirs_scores:
        all_tasks.append(_download_eval_score(obs_base, d, tmp_dir, semaphore))
    for d in new_dirs_completed:
        all_tasks.append(_check_run_completed(obs_base, d, tmp_dir, semaphore))

    new_scores = {}
    new_completed = {}
    if all_tasks:
        results = await asyncio.gather(*all_tasks)
        score_count = len(new_dirs_scores)
        for task_dir, val in results[:score_count]:
            if val is not None:
                new_scores[task_dir] = val
        for task_dir, val in results[score_count:]:
            new_completed[task_dir] = val

    all_scores = {**cached_scores, **new_scores}

    # 2) 有新数据时写回本地缓存
    if new_scores:
        try:
            async with aiofiles.open(local_cache_file, "w") as f:
                await f.write(json.dumps(all_scores, ensure_ascii=False, indent=2))
        except Exception:
            pass
        # 新评分落盘后，即时并入 task_records（延迟导入避免模块级循环依赖）
        try:
            from .instances import _sync_task_records
            # 新评分刚落盘，需全量刷回 task_records 对应行（eval/traj 列）
            await async_execute(_sync_task_records, instance_id, inst["config_path"], True)
        except Exception:
            pass

    all_completed = {**cached_completed, **new_completed}
    _task_completed_cache[instance_id] = all_completed

    simple_scores = {k: v["score"] for k, v in all_scores.items()}

    return {
        "available": bool(all_scores) or bool(all_completed),
        "total_samples": inst.get("total_tasks", 0),
        "uploaded_trajs": len(task_dirs),
        "task_scores": simple_scores,
        "task_eval_details": all_scores,
        "task_completed": all_completed,
    }


async def _download_traj_level(obs_base: str, task_dir: str, tmp_dir: str,
                               semaphore: asyncio.Semaphore) -> tuple[str, dict | None]:
    """下载单个任务的 traj_stats_result.json，提取轨迹分级 task_level。"""
    async with semaphore:
        traj_path = f"{obs_base}{task_dir}/logs/traj_stats_result.json"
        local_file = os.path.join(tmp_dir, f"traj_{task_dir}.json")
        try:
            proc = await asyncio.create_subprocess_exec(
                settings.OBSUTIL_PATH, "cp", traj_path, local_file,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=60)

            if not os.path.exists(local_file):
                return (task_dir, None)

            async with aiofiles.open(local_file, "r", errors="replace") as f:
                raw = await f.read()
            data = json.loads(raw)

            level = data.get("task_level")
            if not level:
                return (task_dir, None)

            completion = data.get("best_completion")
            try:
                completion = float(completion) if completion is not None else None
            except (ValueError, TypeError):
                completion = None

            return (task_dir, {"level": str(level), "completion": completion})
        except Exception:
            return (task_dir, None)
        finally:
            try:
                os.remove(local_file)
            except OSError:
                pass


# 轨迹分级的固定档位（x 轴顺序）
_TRAJ_LEVELS = ["none", "L0", "L1", "L1.5", "L2", "L3"]


@router.get("/{instance_id}/traj-stats")
async def get_traj_stats(
    instance_id: str,
    refresh: bool = Query(default=False, description="强制刷新全部缓存"),
    cache_only: bool = Query(default=False, description="只读本地缓存，不触发 OBS 下载（页面加载用）"),
    user: dict = Depends(get_current_user),
):
    """扫描每个任务目录的 traj_stats_result.json，返回 L0–L3 轨迹分级分布。"""
    inst = _get_instance(instance_id)

    inst_dir = str(Path(inst["config_path"]).parent)
    local_cache_file = os.path.join(inst_dir, _TRAJ_SCORE_FILE)

    # 1) 读本地缓存（非全量刷新时）
    cached_levels: dict[str, dict] = {}
    if not refresh and os.path.exists(local_cache_file):
        try:
            async with aiofiles.open(local_cache_file, "r") as f:
                cached_levels = json.loads(await f.read())
        except Exception:
            cached_levels = {}

    # cache_only：仅返回缓存，不列 OBS、不下载（页面加载路径，避免昂贵扫描）
    if cache_only:
        return _aggregate_traj_dist(inst, cached_levels)

    obs_base = _get_obs_base_path(inst)
    all_dirs = await _list_obs_dirs(obs_base)
    task_dirs = [d for d in all_dirs if d != "logs"]

    new_dirs = [d for d in task_dirs if d not in cached_levels]

    tmp_dir = os.path.join(settings.HIVE_ROOT, "platform", "tmp", instance_id)
    os.makedirs(tmp_dir, exist_ok=True)
    semaphore = asyncio.Semaphore(10)

    new_levels: dict[str, dict] = {}
    if new_dirs:
        results = await asyncio.gather(
            *[_download_traj_level(obs_base, d, tmp_dir, semaphore) for d in new_dirs]
        )
        for task_dir, val in results:
            if val is not None:
                new_levels[task_dir] = val

    all_levels = {**cached_levels, **new_levels}

    # 2) 有新数据时写回本地缓存，并即时并入 task_records
    if new_levels:
        try:
            async with aiofiles.open(local_cache_file, "w") as f:
                await f.write(json.dumps(all_levels, ensure_ascii=False, indent=2))
        except Exception:
            pass
        try:
            from .instances import _sync_task_records
            # 新评分刚落盘，需全量刷回 task_records 对应行（eval/traj 列）
            await async_execute(_sync_task_records, instance_id, inst["config_path"], True)
        except Exception:
            pass

    # 3) 聚合分布
    return _aggregate_traj_dist(inst, all_levels)


def _aggregate_traj_dist(inst: dict, all_levels: dict) -> dict:
    """把 {task_dir: {level, completion}} 聚合成固定六档分布，未知档位归到 none。"""
    level_dist = {lv: 0 for lv in _TRAJ_LEVELS}
    task_levels = {}
    for task_dir, v in all_levels.items():
        lv = (v or {}).get("level") or "none"
        if lv not in level_dist:
            lv = "none"
        level_dist[lv] += 1
        task_levels[task_dir] = lv

    return {
        "available": bool(all_levels),
        "total_samples": inst.get("total_tasks", 0),
        "graded_trajs": len(all_levels),
        "level_dist": level_dist,
        "task_levels": task_levels,
    }
