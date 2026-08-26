# -*- coding: utf-8 -*-
"""离线分析引擎：从 OBS 轨迹批次计算 Lx 漏斗 + 轨迹查看 + 缓存核查（openclaw & hermes）。

设计（与 README_trajdb.md「加载分层」对齐，服务 openclaw-hive/platform）：
  - 只依赖平台本仓：复用 src/traj_pipeline（枚举/快路径/聚合）、src/traj_stats_light（TASK_DONE）；
    轨迹正文解析（analyze_trajectory / hermes messages[]）与裁决抽取从源仓最小迁移至此，
    不跨仓 import（平台 src 的既有约定）。
  - 快路径优先：openclaw 用 logs/traj_stats_result.json（1 文件/task）；hermes 用 workdir/run.log 重算
    （stats 陈旧）；stats 缺失/陈旧 → 回退慢路径（assistant 轨迹 + 主 log，2 文件/task）。
  - harness 判定按文件布局（profiles/*/sessions/*.json → hermes；agents/*/sessions/*.jsonl → openclaw），
    不使用 harness_home（hermes 批次也写 .openclaw，不可靠）。
  - 主日志统一 workdir/run.log（实测 3 批次均为最全正源；logs/<task>.log 是截断副本）。

⚠ 已证实的源仓 bug（本模块不复用）：traj_stats.analyze_hermes_session 用 content 部件数 toolCall 统计，
真实 hermes 工具调用在顶层 tool_calls[]（OpenAI chat 式）→ 旧函数得 tool_calls=0/plain_rounds=25，
正确为 24/1。本模块 analyze_hermes_messages 读顶层 tool_calls[]（见 test/test_offline_analysis.py S4）。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

# platform 根（本文件在 <platform>/src/ 下）
PLATFORM_DIR = Path(__file__).resolve().parent.parent
if str(PLATFORM_DIR) not in sys.path:
    sys.path.insert(0, str(PLATFORM_DIR))

# 平台 src 已迁入的既有能力（快路径/聚合/枚举/TASK_DONE）——不重复实现
from src.traj_pipeline import (  # noqa: E402
    list_task_dirs,
    _fetch_one_task_stats,
    fetch_per_task_stats_files,
    harness_tsr_to_entries,
    stats_from_per_task,
)
from src.traj_stats_light import has_task_done_marker  # noqa: E402

# ============ 本地配置（output_cache / obsutil 路径，统一取自 .env） ============

def _load_env_value(key: str) -> str | None:
    """读 platform/.env 单键值（本平台 KEY=VALUE 格式，不 import dotenv）。"""
    env_path = os.path.join(PLATFORM_DIR, ".env")
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _load_env_cache_dir() -> str:
    """读 platform/.env 的 OUTPUT_CACHE 目录（离线工具统一本地缓存根）。

    取值为绝对路径直接用；相对路径基于 platform/ 解析；未配置回退 platform/output_cache。
    """
    val = _load_env_value("OUTPUT_CACHE")
    if not val:
        return str(PLATFORM_DIR / "output_cache")
    if os.path.isabs(val):
        return val
    return str(PLATFORM_DIR / val)


DEFAULT_OUTPUT_CACHE = _load_env_cache_dir()
DEFAULT_OBSUTIL = _load_env_value("OBSUTIL_PATH") or "obsutil"
DEFAULT_OBS_BUCKET = _load_env_value("OBS_BUCKET") or "obs://s3-asset-b-hd-cce-aifm-nlp-exp"
# 桶内批次所在前缀（无则空串：批次直接在桶根下）
DEFAULT_BATCH_PREFIX = _load_env_value("BATCH_PREFIX") or ""

# 旧目录名 origin（本轮已废弃，仅兼容读取旧缓存）
LEGACY_CACHE_DIR = os.path.join(str(PLATFORM_DIR), "origin")

def _task_obs_parse(task_obs: str) -> tuple[str, str, str]:
    """解析 task obs URL → (正常化 URL, 桶名, 桶内相对路径)。

    obs://<bucket>/<owner>/<batch>/<sub...>/<leaf>
    → (url, '<bucket>', '<owner>/<batch>/sub.../leaf')。
    """
    task_obs = task_obs if task_obs.endswith("/") else task_obs + "/"
    scheme_rest = task_obs.split("://", 1)
    if len(scheme_rest) != 2:
        return task_obs, "", task_obs.strip("/")
    rest = scheme_rest[1].strip("/")
    parts = rest.split("/")
    bucket = parts[0] if parts else ""
    rel = "/".join(parts[1:]) if len(parts) > 1 else rest
    return task_obs, bucket, rel


def _cache_subdir_for(task_obs: str) -> str:
    """obs://bucket/<owner>/<batch>/<sub...>/<leaf> → <batch>/<sub...>/<leaf>。

    丢弃桶名与桶内第一个 in-bucket 段（属主/项目前缀，如 zhangchen——用户示例：
    obs://.../zhangchen/smoke_test_..._oc_traj/acad_000012 → cache/smoke_test_..._oc_traj/acad_000012）；
    保留 batch 段避免同名 task 撞目录。桶内仅 1 段（直接传 batch/任务根，无 owner）→ 整段保留。
    与 traj_pipeline.cache_subdir_for 同规则（快/慢路径下载到同一缓存目录）。
    """
    _url, _bucket, rel = _task_obs_parse(task_obs)
    parts = rel.split("/")
    return "/".join(parts[1:]) if len(parts) > 1 else rel


# 主日志统一为 workdir/run.log（见 memory obs-traj-formats：3 批次均最全正源）
# 回退候选：traj_stats 旧逻辑读 logs/<task>.log / logs/harness_automation.log
LOG_CANDIDATES = ("workdir/run.log", "logs/harness_automation.log", "logs/{task}.log")
GATEWAY_LOG_REL = "workdir/gateway.log"                          # openclaw-only 排障日志
EVAL_USE_LOG_REL = "logs/evaluator_use.log"                      # verdict 回退源（两 harness 均有）
TSR_REL = "logs/traj_stats_result.json"                          # openclaw 快路径 stats
TRAJ_DONE_MARKER = "【Task_Done】"

_MAX_TRAJ_LINES = 5000      # 详情单文件最大读取行数（超出截断，防 OBS 大文件拖垮）
_MAX_TRAJ_BYTES = 20 * 1024 * 1024  # 详情单文件最大读取字节（约 20MB，超过截断）
_LOG_MAX_BYTES = 2 * 1024 * 1024    # 主 log 详情回传尾部 2MB（与 data_viewer 口径一致）


# ============ obsutil 薄封装 ============

def _obsutil_cmd(obsutil: str, args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([obsutil, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"obsutil 超时: {args[0]} {' '.join(args[1:3])}")


def run_obsutil_ls(obsutil: str, prefix: str, one_level: bool = False) -> list[str]:
    """obsutil ls 列对象/目录；one_level=True 用 -d 单层。返回行列表。"""
    cmd = ["ls", prefix]
    if one_level:
        cmd += ["-d"]
    res = _obsutil_cmd(obsutil, cmd)
    if res.returncode != 0:
        raise RuntimeError(f"obsutil ls 失败(exit {res.returncode}): {res.stdout}\n{res.stderr}")
    return (res.stdout or "").splitlines()


def run_obsutil_cp(obsutil: str, src: str, dst: str, include=None, exclude=None,
                   recursive: bool = False) -> None:
    """obsutil cp 单个/整目录；include/exclude 为 obsutil 匹配模式列表。"""
    cmd = ["cp", src, dst, "-f"]
    if recursive:
        cmd += ["-r"]
    for p in (include or []):
        cmd += ["-include", p]
    for p in (exclude or []):
        cmd += ["-exclude", p]
    res = _obsutil_cmd(obsutil, cmd)
    if res.returncode != 0:
        raise RuntimeError(f"obsutil cp 失败(exit {res.returncode}) {src}: "
                           f"{(res.stdout or '')[-400:]}\n{(res.stderr or '')[-400:]}")


# ============ harness 判定 + 路径解析 ============

def detect_harness(task_file_list: list[str]) -> str:
    """按文件布局判定 harness：profiles/*/sessions/*.json → hermes；agents/*/sessions/*.jsonl → openclaw。

    不用 harness_home（hermes 批次的 traj_stats_result.json 也写 /home/ma-user/.openclaw）。
    claude-code 布局（projects/<ws>/*.jsonl，顶层 type=assistant/user）归 openclaw 处理。
    """
    has_profiles = any("profiles/" in p and "/sessions/" in p and p.endswith(".json")
                       for p in task_file_list)
    has_agents = any(("agents/" in p and "/sessions/" in p and p.endswith(".jsonl"))
                     or ("projects/" in p and p.endswith(".jsonl"))
                     for p in task_file_list)
    return "hermes" if has_profiles else "openclaw" if has_agents else "unknown"


def parse_task_obs_path(task_obs: str) -> tuple[str, str]:
    """把 task OBS 路径拆成 (batch_base, leaf)。接受 obs://bucket/prefix/<batch>/<task>/。"""
    task_obs = task_obs.rstrip("/")
    parts = task_obs.split("/")
    leaf = parts[-1]
    batch_base = "/".join(parts[:-1]) + "/"
    return batch_base, leaf


def _batch_url_from_bucket(bucket: str, batch_name: str) -> str:
    bucket = bucket.rstrip("/")
    if not bucket.startswith("obs://"):
        bucket = "obs://" + bucket
    return f"{bucket}/{batch_name.strip('/')}/"


# ============ 轨迹文件枚举 ============

def _is_assistant_agent_dir(name: str) -> bool:
    """assistant 侧的 agent 目录名: 有的 harness 叫 assistant1, 有的叫 main。排除 evaluator。"""
    return (name.startswith("assistant") or name == "main") and name != "evaluator"


def find_openclaw_sessions(task_dir: str) -> list[str]:
    """openclaw：agents/{assistant*,main}/sessions/*.jsonl（排除 .trajectory.jsonl）。

    递归扫描（任务目录下可能嵌套一层同名 <task>/ 或 user_profile_*/ 再挂 agents/）。
    claude-code 布局兜底：projects/<workspace>/*.jsonl（每行 type=user/assistant/…，
    tool 调用在 content 的 tool_use part 里，analyze/parse 统一按 openclaw jsonl 处理）。
    """
    cands = []
    for root, dirs, files in os.walk(task_dir):
        if os.path.basename(root) != "agents":
            continue
        rel = os.path.relpath(root, task_dir)
        if rel.count(os.sep) > 4:
            continue
        for name in sorted(dirs):
            if not _is_assistant_agent_dir(name):
                continue
            sessions_dir = os.path.join(root, name, "sessions")
            if not os.path.isdir(sessions_dir):
                continue
            # 递归扫 sessions 子树：真实布局可能嵌套 <ws>/<session_id>/session.jsonl
            # （obsutil cp -r 生成），file 可能再深一层，不能只 os.listdir 一层。
            for sroot, sdirs, sfiles in os.walk(sessions_dir):
                for fn in sorted(sfiles):
                    if fn.endswith(".jsonl") and "trajectory" not in fn:
                        cands.append(os.path.join(sroot, fn))
    # claude-code 兜底：<task_dir>/projects/<workspace>/*.jsonl
    # （os.walk 的 root 以 task_dir 开头，projects 不是顶层 basename；用 rel 判断）
    if not cands:
        for root, dirs, files in os.walk(task_dir):
            rel = os.path.relpath(root, task_dir)
            parts = rel.split(os.sep)
            if "projects" not in parts:
                continue
            if len(parts) > 3:          # 限制嵌套深度（projects/<ws>/）
                continue
            for fn in sorted(files):
                if fn.endswith(".jsonl") and "trajectory" not in fn:
                    cands.append(os.path.join(root, fn))
    return cands


def find_hermes_sessions(task_dir: str) -> list[str]:
    """hermes：profiles/{assistant*,main}/sessions/session_*.json（排除 evaluator）。

    递归扫描（兼容嵌套布局）。
    """
    cands = []
    for root, dirs, files in os.walk(task_dir):
        if os.path.basename(root) != "profiles":
            continue
        rel = os.path.relpath(root, task_dir)
        if rel.count(os.sep) > 4:
            continue
        for agent_name in sorted(dirs):
            if not _is_assistant_agent_dir(agent_name):
                continue
            sessions_dir = os.path.join(root, agent_name, "sessions")
            if not os.path.isdir(sessions_dir):
                continue
            # 递归扫 sessions 子树（嵌套布局，见 find_openclaw_sessions）
            for sroot, sdirs, sfiles in os.walk(sessions_dir):
                for fn in sorted(sfiles):
                    if fn.endswith(".json"):
                        cands.append(os.path.join(sroot, fn))
    return cands


def list_task_trajectories(task_dir: str) -> list[dict]:
    """列出该 task 本地目录下的全部轨迹文件（assistant1/main/evaluator，两 harness）。

    返回 [{path, role, kind, size, mtime, is_parsed_source, note}]。
    递归扫描（openclaw 任务目录下可能嵌套一层同名 <task>/ 或 user_profile_*/ 再挂 agents/）。
    """
    out = []
    seen = set()

    def add(path, role, note=""):
        ap = os.path.abspath(path)
        if ap in seen or not os.path.isfile(path):
            return
        seen.add(ap)
        st = os.stat(path)
        kind = "jsonl" if path.endswith(".jsonl") else "json" if path.endswith(".json") else "other"
        out.append({"path": path, "role": role, "kind": kind,
                    "size": st.st_size, "mtime": st.st_mtime, "note": note})

    # 递归找 agents/ 与 profiles/ 目录（任意深度）
    for root, dirs, files in os.walk(task_dir):
        rel = os.path.relpath(root, task_dir)
        base = os.path.basename(root)
        if base not in ("agents", "profiles") or rel.count(os.sep) > 4:
            continue
        kind_dir = "agents" if base == "agents" else "profiles"
        for name in sorted(dirs):
            sess = os.path.join(root, name, "sessions")
            if not os.path.isdir(sess):
                continue
            role = "evaluator" if name == "evaluator" else "assistant"
            # 递归扫 sessions 子树（嵌套布局 <ws>/<session_id>/session.jsonl）
            for sroot, sdirs, sfiles in os.walk(sess):
                for fn in sorted(sfiles):
                    if kind_dir == "agents":
                        if fn.endswith(".jsonl") and "trajectory" not in fn:
                            note = "解析取最大文件" if role == "assistant" else ""
                            add(os.path.join(sroot, fn), role, note)
                    else:
                        if fn.endswith(".json"):
                            add(os.path.join(sroot, fn), role)
    # claude-code：projects/<workspace>/*.jsonl（assistant 轨迹）
    for root, dirs, files in os.walk(task_dir):
        rel = os.path.relpath(root, task_dir)
        parts = rel.split(os.sep)
        if "projects" not in parts or len(parts) > 3:
            continue
        for fn in sorted(files):
            if fn.endswith(".jsonl") and "trajectory" not in fn:
                add(os.path.join(root, fn), "assistant", "解析取最大文件")
    return out


def find_primary_assistant_trajectory(task_dir: str) -> str | None:
    """返回解析实际使用的 assistant 轨迹（多候选取最大者，与源仓 find_assistant_trajectories 一致）。"""
    oc = find_openclaw_sessions(task_dir)
    if oc:
        return max(oc, key=lambda p: os.path.getsize(p))
    hm = find_hermes_sessions(task_dir)
    if hm:
        return max(hm, key=lambda p: os.path.getsize(p))
    return None


def find_primary_log(task_dir: str, task: str | None = None) -> str | None:
    """主日志：workdir/run.log 优先，回退 logs/harness_automation.log / logs/<task>.log。"""
    for rel in LOG_CANDIDATES:
        p = os.path.join(task_dir, *rel.format(task=task).split("/")) if "{task}" in rel \
            else os.path.join(task_dir, *rel.split("/"))
        if os.path.isfile(p):
            return p
    return None


def find_gateway_log(task_dir: str) -> str | None:
    p = os.path.join(task_dir, *GATEWAY_LOG_REL.split("/"))
    return p if os.path.isfile(p) else None


def find_eval_use_log(task_dir: str) -> str | None:
    p = os.path.join(task_dir, *EVAL_USE_LOG_REL.split("/"))
    return p if os.path.isfile(p) else None


# ============ 主日志「【Task_Done】」 ============

def has_task_done_marker_in_file(log_path: str) -> bool:
    """主 log 是否含「【Task_Done】」标记（复用 src/traj_stats_light）。"""
    return has_task_done_marker(log_path)


# ============ openclaw jsonl 轨迹解析（从源仓迁入，无外部依赖） ============

def analyze_trajectory(path: str) -> dict:
    """分析 openclaw assistant 轨迹（jsonl 事件流）。返回 {tool_calls, plain_rounds, assistant_rounds, *_tokens}。

    与源仓 traj_stats.analyze_trajectory 逐字等价（旧格式）：type=='message' 行、role==assistant、
    content parts 统计 toolCall；claude-code 兼容：顶层 type=='assistant'/'user'
    （role 在 message 里，同样解析）。
    新增事件流格式（2026-08 实测 0822_rubrics 批）：顶层 type 为 assistant/message、
    user/message、tool/call、tool/result、step/start、step/end（data 里带 turn/step）。
    tool_calls 数 tool/call 事件；plain_rounds 数无 tool/call 的 assistant step。
    """
    tool_calls = 0
    plain_rounds = 0
    assistant_rounds = 0
    input_tk = output_tk = reasoning_tk = 0
    # 事件流格式：按 (turn, step) 记录该 step 是否伴随 tool/call，用于统计 plain_rounds
    tool_msgs: dict = {}

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            top_type = obj.get("type")

            # ── 新事件流格式：assistant/message + tool/call（data.turn/step 关联）──
            if top_type in ("assistant/message", "tool/call", "tool/result",
                            "user/message"):
                data = obj.get("data") or {}
                if top_type == "assistant/message":
                    assistant_rounds += 1
                    msg = data.get("message") or {}
                    usage = msg.get("usage")
                    if isinstance(usage, dict):
                        input_tk += usage.get("input") or 0
                        output_tk += usage.get("output") or 0
                        reasoning_tk += usage.get("reasoningTokens") or 0
                    key = (data.get("turn"), data.get("step"))
                    # 该 assistant step 是否伴随 tool/call（稍后回填）→ 见 tool_calls 计数
                    tool_msgs.setdefault(key, {"tool": False, "plain": True})
                elif top_type == "tool/call":
                    tool_calls += 1
                    _tk = (data.get("turn"), data.get("step"))
                    _m = tool_msgs.get(_tk)
                    if _m is None:
                        tool_msgs[_tk] = {"tool": True, "plain": False}
                    else:
                        _m["tool"] = True
                        _m["plain"] = False
                continue

            # ── 旧格式：type in (message, assistant, user) ──
            if top_type not in ("message", "assistant", "user"):
                continue
            msg = obj.get("message") or {}
            role = msg.get("role")
            if top_type == "message" and role != "assistant":
                continue
            if top_type in ("assistant", "user") and role not in ("assistant", "user"):
                continue

            assistant_rounds += 1
            usage = msg.get("usage")
            if isinstance(usage, dict):
                input_tk += usage.get("input") or 0
                output_tk += usage.get("output") or 0
                reasoning_tk += usage.get("reasoningTokens") or 0

            content = msg.get("content")
            if isinstance(content, str):
                parts_types = ["text"] if content else []
            elif isinstance(content, list):
                parts_types = [p.get("type") for p in content if isinstance(p, dict)]
            else:
                parts_types = []

            n_tc = parts_types.count("toolCall") + parts_types.count("tool_use")
            tool_calls += n_tc
            if n_tc == 0:
                plain_rounds += 1

    # 事件流：assistant step 无 tool/call 的计为 plain_rounds
    for key, flag in tool_msgs.items():
        if flag["plain"] and not flag["tool"]:
            plain_rounds += 1

    return {
        "tool_calls": tool_calls,
        "plain_rounds": plain_rounds,
        "assistant_rounds": assistant_rounds,
        "input_tokens": input_tk,
        "output_tokens": output_tk,
        "reasoning_tokens": reasoning_tk,
        "total_tokens": input_tk + output_tk + reasoning_tk,
    }


def parse_openclaw_trajectory(path: str, max_lines: int = _MAX_TRAJ_LINES,
                              max_bytes: int = _MAX_TRAJ_BYTES) -> list[dict]:
    """openclaw 轨迹 → 归一化消息流（供 print_trajectory / 前端详情）。

    每块 {role, part_type, content, tool_name, args, isError, exitCode, details, truncated}。

    支持两种 openclaw jsonl 布局：
      旧格式  type ∈ (message, assistant, user)（content parts 里 toolCall/tool_use）
      事件流  2026-08 实测：assistant/message、user/message、tool/result（data.message.content
              parts 为 reasoning/text/tool-call），配套 assistant/chunk + *-chunks 分片流不承载
              最终正文（正文在 assistant/message 里），不单独解析。
    事件流按 data.seq 重排保证时序（user/assistant/tool 结果交错还原）。
    """
    # 读入全部行（受 max_lines/max_bytes 截断），先判布局再解析。
    raw_lines: list[str] = []
    n_lines = 0
    n_bytes = 0
    truncated = False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            n_lines += 1
            n_bytes += len(line.encode("utf-8", "replace"))
            if n_lines > max_lines or n_bytes > max_bytes:
                truncated = True
                break
            line = line.strip()
            if line:
                raw_lines.append(line)
    if truncated:
        tail = (f"[截断] 达到 {max_lines} 行 / {max_bytes // (1024*1024)}MB 限制",)
    else:
        tail = ()

    # 判布局：任一事件流类型行 → 事件流；否则旧格式。
    is_stream = False
    objs: list[dict] = []
    for line in raw_lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            obj = None
        objs.append(obj)
        if obj and obj.get("type") in ("assistant/message", "user/message", "tool/result"):
            is_stream = True
    if not is_stream:
        return _parse_openclaw_traj_legacy(objs, tail)

    # ── 事件流格式：收集 (seq, block) 排序还原时序 ──
    seq_blocks: list[tuple[int, dict]] = []
    for obj in objs:
        if not obj:
            continue
        top_type = obj.get("type")
        data = obj.get("data") or {}
        seq = obj.get("seq") or 0
        if top_type == "user/message":
            for pt in data.get("content") or []:
                if not isinstance(pt, dict):
                    continue
                if pt.get("type") == "text":
                    seq_blocks.append((seq, {"role": "user", "part_type": "text",
                                             "content": pt.get("text"),
                                             "tool_name": None, "args": None,
                                             "isError": None, "exitCode": None, "details": None}))
        elif top_type == "assistant/message":
            msg = data.get("message") or {}
            for pt in msg.get("content") or []:
                if not isinstance(pt, dict):
                    continue
                ptype = pt.get("type")
                if ptype == "reasoning":
                    seq_blocks.append((seq, {"role": "assistant", "part_type": "thinking",
                                             "content": pt.get("text"),
                                             "tool_name": None, "args": None,
                                             "isError": None, "exitCode": None, "details": None}))
                elif ptype == "text":
                    seq_blocks.append((seq, {"role": "assistant", "part_type": "text",
                                             "content": pt.get("text"),
                                             "tool_name": None, "args": None,
                                             "isError": None, "exitCode": None, "details": None}))
                elif ptype == "tool-call":
                    seq_blocks.append((seq, {"role": "assistant", "part_type": "toolCall",
                                             "content": None,
                                             "tool_name": pt.get("name"),
                                             "args": pt.get("arguments"),
                                             "isError": None, "exitCode": None, "details": None,
                                             "tool_call_id": pt.get("id")}))
        elif top_type == "tool/result":
            msg = data.get("message") or {}
            # content 可能为字符串或 [{type:tool-result, toolCallId, content, isError}]
            content = msg.get("content")
            tcid = None
            is_err = False
            if isinstance(content, list):
                for pt in content:
                    if not isinstance(pt, dict):
                        continue
                    if pt.get("type") == "tool-result":
                        content = pt.get("content")
                        tcid = pt.get("toolCallId")
                        is_err = bool(pt.get("isError"))
                        break
                if not isinstance(content, str) and isinstance(content, list):
                    content = _parts_to_text(content)
            seq_blocks.append((seq, {"role": "toolResult", "part_type": "toolResult",
                                     "content": content, "tool_name": None,
                                     "args": None, "isError": is_err,
                                     "exitCode": None, "details": None,
                                     "tool_call_id": tcid}))
    seq_blocks.sort(key=lambda t: t[0])
    blocks = [b for _seq, b in seq_blocks]
    if truncated:
        blocks.append({"role": None, "part_type": "truncated", "content": tail[0],
                       "tool_name": None, "args": None, "isError": None,
                       "exitCode": None, "details": None})
    return blocks


def _parts_to_text(parts) -> str:
    """tool-result 正文 content 是 parts 数组时拍平为文本（拼接各 text）。"""
    out = []
    for pt in parts:
        if isinstance(pt, dict):
            if pt.get("type") == "text" and pt.get("text"):
                out.append(pt["text"])
            elif isinstance(pt.get("content"), (str, list)):
                inner = pt["content"]
                out.append(inner if isinstance(inner, str) else _parts_to_text(inner))
    return "\n".join(out)


def _parse_openclaw_traj_legacy(objs, tail=()) -> list[dict]:
    """旧格式（type ∈ message/assistant/user）：逐行拆 parts，尾部截断标记可选追加。"""
    blocks: list[dict] = []
    for obj in objs:
        if not obj or obj.get("type") not in ("message", "assistant", "user"):
            continue
        msg = obj.get("message") or {}
        role = msg.get("role")
        if not role:
            role = obj.get("type") if obj.get("type") in ("assistant", "user") else None
        content = msg.get("content")
        if role == "assistant":
            if isinstance(content, str):
                blocks.append({"role": role, "part_type": "text", "content": content,
                               "tool_name": None, "args": None, "isError": None,
                               "exitCode": None, "details": None})
            elif isinstance(content, list):
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    ptype = p.get("type")
                    tool_call = p.get("toolCall")
                    if ptype == "thinking":
                        content_text, tool_name, args = p.get("thinking"), None, None
                    elif ptype == "toolCall" or ptype == "tool_use":
                        # 兼容三种真实布局：
                        #   扁平   {type, id, name, arguments}     （claude-code tool_use）
                        #   扁平   {type, id, name, arguments}     （openclaw toolCall 扁平）
                        #   嵌套   {type, toolCall:{function:{name, arguments}}}
                        content_text = None
                        if isinstance(tool_call, dict):
                            fn = tool_call.get("function") or {}
                            tool_name = fn.get("name")
                            args = fn.get("arguments")
                            if tool_name is None and "name" in tool_call:
                                tool_name = tool_call.get("name")
                                args = tool_call.get("arguments")
                        else:
                            tool_name = p.get("name")
                            args = p.get("arguments")
                            if args is None:
                                args = p.get("input")   # claude-code: input 即 tool args
                    else:
                        content_text, tool_name, args = p.get("text"), None, None
                    blocks.append({"role": role, "part_type": ptype,
                                   "content": content_text,
                                   "tool_name": tool_name, "args": args,
                                   "isError": None, "exitCode": None, "details": None})
            elif role == "toolResult":
                details = msg.get("details") or {}
                blocks.append({"role": role, "part_type": "toolResult",
                               "content": msg.get("content"),
                               "tool_name": (msg.get("tool") or {}).get("name"),
                               "args": None,
                               "isError": bool(msg.get("isError")),
                               "exitCode": (details.get("exitCode")
                                            if isinstance(details, dict) else None),
                               "details": details})
            else:
                blocks.append({"role": role, "part_type": "text", "content": content,
                               "tool_name": None, "args": None, "isError": None,
                               "exitCode": None, "details": None})
    if tail:
        blocks.append({"role": None, "part_type": "truncated", "content": tail[0],
                       "tool_name": None, "args": None, "isError": None,
                       "exitCode": None, "details": None})
    return blocks


# ============ hermes messages[] 解析（新实现，修源仓 bug） ============

def analyze_hermes_messages(path: str) -> dict:
    """hermes session_*.json（OpenAI-chat messages[]）统计。

    返回 {tool_calls, plain_rounds, assistant_rounds}。
    - tool_calls  : assistant 消息顶层 tool_calls[] 总数（实测 24/25 assistant 轮）
    - plain_rounds: 无 tool_calls 的 assistant 消息数（实测 1）
    - assistant_rounds: assistant 消息总数
    ⚠ 不复用源仓 analyze_hermes_session（其按 content 部件 toolCall 数，真实 hermes 为 0）。
    """
    tool_calls = 0
    plain_rounds = 0
    assistant_rounds = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"tool_calls": 0, "plain_rounds": 0, "assistant_rounds": 0}

    for msg in (data.get("messages") or []):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        assistant_rounds += 1
        tcs = msg.get("tool_calls")
        n_tc = len(tcs) if isinstance(tcs, list) else 0
        tool_calls += n_tc
        if n_tc == 0:
            plain_rounds += 1

    return {"tool_calls": tool_calls, "plain_rounds": plain_rounds,
            "assistant_rounds": assistant_rounds}


def parse_hermes_messages(path: str, max_lines: int = _MAX_TRAJ_LINES,
                          max_bytes: int = _MAX_TRAJ_BYTES) -> list[dict]:
    """hermes session → 归一化消息流（与 parse_openclaw_trajectory 同结构）。

    - assistant 顶层 tool_calls[] → part_type=toolCall（每个函数调用一块）
    - reasoning_content → part_type=thinking
    - 纯字符串 content → part_type=text
    - role=tool → part_type=toolResult
    """
    blocks: list[dict] = []
    n_lines = 0
    n_bytes = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return blocks
    n_lines = raw.count("\n")
    n_bytes = len(raw.encode("utf-8", "replace"))
    if n_lines > max_lines or n_bytes > max_bytes:
        blocks.append({"role": None, "part_type": "truncated",
                       "content": f"[截断] 达到 {max_lines} 行 / {max_bytes // (1024*1024)}MB 限制",
                       "tool_name": None, "args": None, "isError": None,
                       "exitCode": None, "details": None})
        return blocks
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return blocks

    for msg in (data.get("messages") or []):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            content = msg.get("content")
            reasoning = msg.get("reasoning_content") or msg.get("reasoning")
            if reasoning:
                blocks.append({"role": role, "part_type": "thinking", "content": reasoning,
                               "tool_name": None, "args": None, "isError": None,
                               "exitCode": None, "details": None})
            if isinstance(content, str) and content.strip():
                blocks.append({"role": role, "part_type": "text", "content": content,
                               "tool_name": None, "args": None, "isError": None,
                               "exitCode": None, "details": None})
            elif isinstance(content, list):
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    blocks.append({"role": role, "part_type": p.get("type"),
                                   "content": p.get("text") or p.get("content"),
                                   "tool_name": None, "args": None, "isError": None,
                                   "exitCode": None, "details": None})
            tcs = msg.get("tool_calls")
            if isinstance(tcs, list):
                for tc in tcs:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    blocks.append({"role": role, "part_type": "toolCall",
                                   "content": fn.get("arguments") or "",
                                   "tool_name": fn.get("name"),
                                   "args": fn,
                                   "isError": None, "exitCode": None, "details": None})
        elif role == "tool":
            blocks.append({"role": role, "part_type": "toolResult",
                           "content": msg.get("content"),
                           "tool_name": msg.get("name") or msg.get("tool_name"),
                           "args": None,
                           "isError": None, "exitCode": None, "details": None})
        else:
            blocks.append({"role": role, "part_type": "text", "content": msg.get("content"),
                           "tool_name": None, "args": None, "isError": None,
                           "exitCode": None, "details": None})
    return blocks


# ============ 主 log 裁决解析（从源仓迁入，纯正则无外部依赖） ============

_EVAL_MARKER = re.compile(r"\[Evaluator\]\s+turn=(\d+)\s+agent=\S+.*输出")


def _parse_json_block_after(lines: list[str], idx: int):
    """从 lines[idx] 之后第一个以 '{' 开头的行起，大括号计数取完整 JSON 块。非法返回 '__BADJSON__'。"""
    j = idx + 1
    while j < len(lines) and lines[j].strip() != "{":
        j += 1
    if j >= len(lines):
        return None
    depth = 0
    buf = []
    for k in range(j, len(lines)):
        buf.append(lines[k])
        depth += lines[k].count("{") - lines[k].count("}")
        if depth <= 0:
            break
    try:
        return json.loads("".join(buf))
    except json.JSONDecodeError:
        return "__BADJSON__"


def extract_first_evaluator_obj(log_path: str):
    """主 log 中「编号最小」的 [Evaluator] turn=N 裁决块（评测可能从 turn=2/3 开始）。"""
    if not os.path.isfile(log_path):
        return None
    with open(log_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    marks = []
    for i, l in enumerate(lines):
        m = _EVAL_MARKER.search(l)
        if m:
            marks.append((int(m.group(1)), i))
    if not marks:
        return None
    marks.sort(key=lambda x: (x[0], x[1]))
    _, idx = marks[0]
    return _parse_json_block_after(lines, idx)


def extract_first_evaluator_verdict(log_path: str) -> tuple[bool, float | None]:
    """返回 (has_verdict, completion)。completion：float 分数；无数值（或 null/非法）时 None。"""
    obj = extract_first_evaluator_obj(log_path)
    if obj is None:
        return False, None
    if obj == "__BADJSON__":
        return True, None
    comp = obj.get("completion")
    if isinstance(comp, (int, float)):
        return True, float(comp)
    return True, None


# ============ Lx 漏斗口径（单点权威，与 README 二章一致） ============

def compute_level(harness: str, tool_calls: int, plain_rounds: int,
                  completion=None) -> str:
    """按 README 口径出 L0/L1/L1.5/L2/L3（单点打标函数）。

    - L0   = 有轨迹（调用方保证）
    - L1   : openclaw = tool_calls>=3 且 plain_rounds>0；hermes = plain_rounds>0
    - L1.5 : L1 且有数值 completion
    - L2   : L1.5 且 completion>=0.5
    - L3   : L1.5 且 completion==1
    """
    if harness == "openclaw":
        passed = tool_calls >= 3 and plain_rounds > 0
    elif harness == "hermes":
        passed = plain_rounds > 0
    else:
        passed = False
    if not passed:
        return "L0"
    if completion is None:
        return "L1"
    if completion >= 0.5:
        return "L2" if completion < 1.0 else "L3"
    return "L1.5"


def stats_from_per_task_compact(stats: dict) -> dict:
    """把 stats_from_per_task 的 filter_stats.json 聚合成 show_lx_summary 紧凑输出。"""
    filtered = stats.get("filtered_count", 0)       # L1
    with_eval = stats.get("with_eval_count", 0)     # L1.5
    ge05 = stats.get("completion_ge_0.5", 0)        # L2
    eq1 = stats.get("completion_eq_1", 0)           # L3
    dropped = stats.get("dropped_count", 0)         # L0 - L1
    total = filtered + dropped                       # L0
    td = stats.get("task_done_count", 0)
    return {
        "total": total, "L0": total, "L1": filtered, "L1.5": with_eval,
        "L2": ge05, "L3": eq1, "dropped": dropped, "task_done_count": td,
        "ratio": {
            "L1/L0": round(filtered / total, 4) if total else None,
            "L1.5/L1": round(with_eval / filtered, 4) if filtered else None,
            "L2/L1.5": round(ge05 / with_eval, 4) if with_eval else None,
            "L3/L2": round(eq1 / ge05, 4) if ge05 else None,
        },
        "token_stats": stats.get("token_stats"),
        "char_len_stats": stats.get("char_len_stats"),
    }


def _load_per_task_entries(obsutil: str, task_obs: str, origin: str,
                           obs_cred_args: list[str] | None = None) -> list[dict]:
    """单 task 打标 entry：快路径（tsr）优先，stats 陈旧/缺失 → 回退慢路径（assistant 轨迹 + 主 log）。

    返回 per_task entry 列表（结构与 stats_from_per_task 兼容）：
      {task, harness, tool_calls, assistant_rounds, plain_rounds, has_ge3_toolcalls,
       has_plain_round, passed_gate, has_eval, evaluator_completion, verdict_source,
       trajectory, task_done, char_len(慢路径)}
    """
    task_obs = task_obs if task_obs.endswith("/") else task_obs + "/"
    _t0 = time.time()
    entries = _fetch_one_task_stats_valid(obsutil, task_obs, origin, obs_cred_args=obs_cred_args)
    if entries:
        print(f"    [fast] {os.path.basename(task_obs.rstrip('/'))}: 快路径 tsr 命中 "
              f"{len(entries)} 条 tool_calls={entries[0].get('tool_calls')} "
              f"plain_rounds={entries[0].get('plain_rounds')} {time.time()-_t0:.2f}s", flush=True)
        return entries
    print(f"    [slow] {os.path.basename(task_obs.rstrip('/'))}: 快路径 tsr 未命中/陈旧，回退慢路径下载 "
          f"{time.time()-_t0:.2f}s", flush=True)

    # ── 慢路径：下载 assistant 轨迹 + 主 log，本地重算 ──
    _t_dl = time.time()
    leaf = task_obs.rstrip("/").split("/")[-1]
    dest_dir = os.path.join(origin, _cache_subdir_for(task_obs))
    os.makedirs(dest_dir, exist_ok=True)
    # 先 ls 该 task 顶层，判定 harness 布局（不下大文件）
    top = [ln.strip() for ln in run_obsutil_ls(obsutil, task_obs, one_level=False)]
    harness = detect_harness(top)
    # 整任务递归 cp + include/exclude（同 download_task_detail，处理嵌套 user_profile_*/agents 布局）
    if harness == "hermes":
        include = ["*logs/trajectories/*query*.json", "*logs*.log",
                   "*profiles/assistant*/sessions/*.json", "*profiles/main/sessions/*.json",
                   "*profiles/assistant*/state.db*", "*profiles/main/state.db*"]
    else:
        include = ["*assistant*sessions*.jsonl", "*agents/main/sessions/*.jsonl",
                   "*logs*.log", "*evaluator*sessions*.jsonl",
                   "*logs/trajectories/*query*.json",
                   "*profiles/assistant*/sessions/*.json",
                   "*profiles/main/sessions/*.json",
                   "*profiles/assistant*/state.db*", "*profiles/main/state.db*",
                   "*projects/*/*.jsonl"]  # claude-code: projects/<ws>/<session>.jsonl
    exclude = ["*.trajectory.jsonl", "*_use.log", "*profiles/*/logs/*", "*_logs/*.log"]
    run_obsutil_cp(obsutil, task_obs, dest_dir, recursive=True,
                   include=include, exclude=exclude)
    # 主 log（workdir/run.log 优先）
    log_rel = "workdir/run.log"
    log_dest = os.path.join(dest_dir, *log_rel.split("/"))
    os.makedirs(os.path.dirname(log_dest), exist_ok=True)
    try:
        run_obsutil_cp(obsutil, task_obs + log_rel, log_dest)
    except RuntimeError:
        log_dest = None

    # 本地重算
    if harness == "hermes":
        traj = find_primary_assistant_trajectory(dest_dir)
        if not traj:
            return []
        info = analyze_hermes_messages(traj)
        has_eval, score, source = (False, None, None)
        logp = find_primary_log(dest_dir, leaf)
        if logp:
            has_eval, score = extract_first_evaluator_verdict(logp)
            source = "log" if has_eval else None
        entry = {
            "task": leaf, "trajectory": os.path.relpath(traj, origin),
            "tool_calls": info["tool_calls"], "assistant_rounds": info["assistant_rounds"],
            "plain_rounds": info["plain_rounds"],
            "has_ge3_toolcalls": info["tool_calls"] >= 3,
            "has_plain_round": info["plain_rounds"] > 0,
            "passed_gate": info["plain_rounds"] > 0,     # hermes L1 门槛: 有产出即可
            "has_eval": has_eval, "evaluator_completion": score, "verdict_source": source,
            "harness": "hermes", "task_done": bool(logp and has_task_done_marker(logp)),
        }
        print(f"    [slow] {leaf}: 慢路径完成(hermes) tool_calls={info['tool_calls']} "
              f"plain_rounds={info['plain_rounds']} has_eval={has_eval} "
              f"下载+重算 {time.time()-_t_dl:.2f}s", flush=True)
        return [entry]
    else:
        traj = find_primary_assistant_trajectory(dest_dir)
        if not traj:
            return []
        info = analyze_trajectory(traj)
        logp = find_primary_log(dest_dir, leaf)
        has_eval, score, source = (False, None, None)
        if logp:
            has_eval, score = extract_first_evaluator_verdict(logp)
            source = "log" if has_eval else None
        gate = info["tool_calls"] >= 3 and info["plain_rounds"] > 0
        entry = {
            "task": leaf, "trajectory": os.path.relpath(traj, origin),
            "tool_calls": info["tool_calls"], "assistant_rounds": info["assistant_rounds"],
            "plain_rounds": info["plain_rounds"],
            "has_ge3_toolcalls": info["tool_calls"] >= 3,
            "has_plain_round": info["plain_rounds"] > 0,
            "passed_gate": gate,
            "has_eval": has_eval, "evaluator_completion": score, "verdict_source": source,
            "harness": "openclaw", "task_done": bool(logp and has_task_done_marker(logp)),
        }
    # char_len（详情列）
    try:
        entry["char_len"] = len(open(traj, encoding="utf-8", errors="replace").read())
    except OSError:
        entry["char_len"] = 0
    print(f"    [slow] {leaf}: 慢路径完成 tool_calls={info['tool_calls']} "
          f"plain_rounds={info['plain_rounds']} has_eval={has_eval} "
          f"下载+重算 {time.time()-_t_dl:.2f}s", flush=True)
    return [entry]


# ============ 批次级漏斗（快路径优先） ============

def _entry_is_stale(entry: dict) -> bool:
    """判定一条快路径（tsr）entry 是否陈旧、不可信。

    采集侧过期 stats 的形态（已实测 hermes 批次）：task_level "none"、
    agents[0] 的 trajectory=None / has_trajectory=False / 全零计数 → harness_tsr_to_entries
    仍产出 1 条 entry，直接消费会把好轨迹误判成 L0/dropped。必须回退慢路径重算。

    判定：harness 必须有 agents[0] 指向的真实轨迹；tool_calls/plain_rounds 全 0 视为陈旧
    （合格 openclaw 轨迹 tool_calls>=3，hermes 有产出即 >=1，全 0 不可能是有效轨迹）。
    """
    traj = entry.get("trajectory")
    if not traj:
        return True
    if not isinstance(traj, str):
        return True
    if entry.get("tool_calls", 0) <= 0 and entry.get("plain_rounds", 0) <= 0:
        print(f"    [stale] {entry.get('task') or entry.get('trajectory')}: tsr 判陈旧 "
              f"(tool_calls={entry.get('tool_calls')} plain_rounds={entry.get('plain_rounds')})", flush=True)
        return True
    return False


def _fetch_one_task_stats_valid(obsutil: str, task_obs: str, origin: str,
                                obs_cred_args: list[str] | None = None) -> list[dict]:
    """_fetch_one_task_stats + 陈旧过滤：过期 tsr 返回 []，触发上层回退慢路径。"""
    entries = _fetch_one_task_stats(obsutil, task_obs, origin, obs_cred_args=obs_cred_args,
                                    with_task_done=True)
    if not entries:
        return []
    return [e for e in entries if not _entry_is_stale(e)]


def build_batch_summary(obsutil: str, batch_obs: str, origin: str,
                        obs_cred_args: list[str] | None = None,
                        concurrency: int = 8, max_tasks: int = 0,
                        force_slow: bool = False) -> dict:
    """整批 Lx 漏斗：快路径（并发拉 tsr / hermes run.log 重算）优先，无效自动回退慢路径。

    返回 stats_from_per_task 结构（filter_stats.json 兼容，含 per_session/token_stats/char_len_stats）。
    """
    os.makedirs(origin, exist_ok=True)
    if not force_slow:
        per_task = fetch_per_task_stats_files(obsutil, batch_obs, origin,
                                              obs_cred_args=obs_cred_args,
                                              concurrency=concurrency, with_task_done=True)
        if per_task:
            valid = [e for e in per_task if not _entry_is_stale(e)]
            # 过期 tsr 被过滤（hermes 陈旧批次整批全滤 → 回退慢路径重算，见 _load_per_task_entries）
            if valid:
                stats = stats_from_per_task(valid, source_type="workspace")
                stats["fast_path"] = True
                stats["stale_entries_dropped"] = len(per_task) - len(valid)
                return stats
            print(f"      [fast] 整批 {len(per_task)} 条 stats 全部陈旧(如 hermes task_level=none)，回退慢路径", flush=True)

    # 慢路径：逐 task 最小下载 + 本地重算
    tasks = list_task_dirs(obsutil, batch_obs, obs_cred_args=obs_cred_args)
    if max_tasks and max_tasks > 0:
        tasks = tasks[:max_tasks]
    per_task_all = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futs = {ex.submit(_load_per_task_entries, obsutil, t, origin, obs_cred_args): t
                for t in tasks}
        for fut in as_completed(futs):
            es = fut.result()
            if es:
                per_task_all.extend(es)
    stats = stats_from_per_task(per_task_all, source_type="workspace")
    stats["fast_path"] = False
    return stats


# ============ 详情（层级 2，按需懒加载语义） ============

def read_tsr_stats(task_dir: str) -> dict | None:
    """读本地 logs/traj_stats_result.json（快路径产物），返回可展示的 stats 摘要。

    轨迹 jsonl 缺失时（OBS 已清理/仅浅层产物在缓存）用它兜底：
    stats 直接来自 tsr，无轨迹块；verdict 由主 log 判定。
    返回 None 表示 tsr 不存在或不可解析（上层继续回退/报 409）。
    """
    p = os.path.join(task_dir, *TSR_REL.split("/"))
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    ags = data.get("agents") or []
    ag = ags[0] if ags else {}
    return {
        "tool_calls": ag.get("tool_calls"),
        "assistant_rounds": ag.get("assistant_rounds"),
        "plain_rounds": ag.get("plain_rounds"),
        "has_eval": bool(ag.get("has_eval")),
        "evaluator_completion": ag.get("evaluator_completion"),
        "level": data.get("task_level"),
        "task_done": bool(data.get("task_done")) or has_task_done_marker(
            find_primary_log(task_dir, data.get("task")) or ""),
    }


def load_task_detail(task_dir: str, task: str | None = None,
                     known_harness: str | None = None) -> dict:
    """本地缓存 cache/<batch>/<leaf>/ 下加载单任务详情。

    返回 {harness, assistant_trajectory(blocks), assistant_stats, trajectories(list),
          evaluator_trajectory(blocks), log{path, tail}, gateway{path, tail}, eval_use_log, ...}
    轨迹 jsonl 缺失（OBS 已清理/仅 tsr+log 浅层产物）时，assistant_stats/verdict 用
    tsr 回退；轨迹块置空数组。

    known_harness: DB task_traj_records.harness（浅层从 tsr 判定写入）。详情页优先用它，
    而非 detect_harness(本地文件布局)——本地只有浅层产物、无 agents/*.jsonl 轨迹文件时，
    detect_harness 会判 "unknown"，但轨迹其实在 OBS 未下载，harness 应显示 DB 里的真实值。
    known_harness 非空且非 unknown 时优先；否则回退 detect_harness 兜底。
    """
    out: dict = {}
    out["task"] = task or os.path.basename(os.path.abspath(task_dir))
    # harness 判定（本地目录布局）
    top = [os.path.join(task_dir, p) for p in os.listdir(task_dir)] if os.path.isdir(task_dir) else []
    rel_paths = []
    for root, _, files in os.walk(task_dir):
        for fn in files:
            rel_paths.append(os.path.relpath(os.path.join(root, fn), task_dir))
    if known_harness and known_harness != "unknown":
        out["harness"] = known_harness
    else:
        out["harness"] = detect_harness(rel_paths)

    # assistant 轨迹
    traj = find_primary_assistant_trajectory(task_dir)
    out["assistant_trajectory"] = None
    out["assistant_stats"] = None
    tsr_stats = None
    if traj:
        if out["harness"] == "hermes":
            out["assistant_trajectory"] = parse_hermes_messages(traj)
            out["assistant_stats"] = analyze_hermes_messages(traj)
        else:
            out["assistant_trajectory"] = parse_openclaw_trajectory(traj)
            out["assistant_stats"] = analyze_trajectory(traj)
    else:
        # 无轨迹 jsonl：浅层快路径产物兜底（OBS 轨迹已清理时仍可看 stats/log/verdict）
        tsr_stats = read_tsr_stats(task_dir)
        out["assistant_stats"] = tsr_stats

    # evaluator 轨迹（懒加载语义：本地有才读）。
    # 任务目录下可能是嵌套布局 task_dir/<traj_name>/agents/evaluator/sessions/<ws>/<session_id>/session.jsonl
    # （obsutil cp -r 保留 OBS 父子层），不能只 os.listdir 一层，也不能硬编码 top 级路径——
    # 同 find_primary_assistant_trajectory / list_task_trajectories：全树递归扫 evaluator/sessions。
    ev_traj = None
    for root, _dirs, _files in os.walk(task_dir):
        rel = os.path.relpath(root, task_dir)
        base = os.path.basename(root)
        if base not in ("agents", "profiles") or rel.count(os.sep) > 4:
            continue
        sess = os.path.join(root, "evaluator", "sessions")
        if not os.path.isdir(sess):
            continue
        cands = []
        for sroot, _sdirs, sfiles in os.walk(sess):
            for fn in sorted(sfiles):
                if fn.endswith(".jsonl") and "trajectory" not in fn:
                    cands.append(os.path.join(sroot, fn))
                elif fn.endswith(".json"):
                    cands.append(os.path.join(sroot, fn))
        if cands:
            ev_traj = max(cands, key=lambda p: os.path.getsize(p))
            break
    out["evaluator_trajectory"] = parse_openclaw_trajectory(ev_traj) if ev_traj and ev_traj.endswith(".jsonl") \
        else (parse_hermes_messages(ev_traj) if ev_traj else None)

    # 轨迹文件清单
    out["trajectories"] = list_task_trajectories(task_dir)

    # 主 log + gateway + eval_use_log（尾部 2MB）
    logp = find_primary_log(task_dir, task)
    out["log"] = {"path": os.path.relpath(logp, task_dir) if logp else None,
                  "tail": _tail_text(logp, _LOG_MAX_BYTES) if logp else None}
    gw = find_gateway_log(task_dir)
    out["gateway"] = {"path": os.path.relpath(gw, task_dir) if gw else None,
                      "tail": _tail_text(gw, _LOG_MAX_BYTES) if gw else None}
    eu = find_eval_use_log(task_dir)
    out["eval_use_log"] = {"path": os.path.relpath(eu, task_dir) if eu else None,
                           "tail": _tail_text(eu, _LOG_MAX_BYTES) if eu else None}

    # verdict（主 log 首轮；无轨迹且 tsr 兜底时同样用主 log）
    has_eval, score = (False, None)
    if logp:
        has_eval, score = extract_first_evaluator_verdict(logp)
    elif tsr_stats:
        has_eval, score = bool(tsr_stats.get("has_eval")), tsr_stats.get("evaluator_completion")
    out["verdict"] = {"has_eval": has_eval, "completion": score,
                      "verdict_source": "log" if has_eval else None,
                      "task_done": bool(logp and has_task_done_marker(logp))
                      or (bool(tsr_stats and tsr_stats.get("task_done")) if not logp else False)}
    return out


def _tail_text(path: str | None, max_bytes: int) -> str | None:
    if not path or not os.path.isfile(path):
        return None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def download_task_detail(obsutil: str, task_obs: str, origin: str,
                         obs_cred_args: list[str] | None = None) -> str:
    """层级2：按需下载单个 task 详情所需文件到 cache/<task相对路径>/，返回本地 task_dir。

    cache 子目录 = _cache_subdir_for(task_obs)（obs://bucket/<batch>/<task> → cache/<batch>/<task>）。
    整任务递归 cp + include/exclude（模式源自源仓 download_task；include 按完整路径匹配，
    天然覆盖 task 下任意嵌套的 user_profile_*/agents|profiles 布局）。
    """
    task_obs = task_obs if task_obs.endswith("/") else task_obs + "/"
    dest = os.path.join(origin, _cache_subdir_for(task_obs))
    os.makedirs(dest, exist_ok=True)

    top = [ln.strip() for ln in run_obsutil_ls(obsutil, task_obs, one_level=False)]
    harness = detect_harness(top)
    if harness == "hermes":
        include = ["*logs/trajectories/*query*.json", "*logs*.log",
                   "*profiles/assistant*/sessions/*.json", "*profiles/main/sessions/*.json",
                   "*profiles/assistant*/state.db*", "*profiles/main/state.db*"]
        exclude = ["*.trajectory.jsonl", "*_use.log", "*profiles/*/logs/*", "*_logs/*.log"]
    else:
        include = ["*assistant*sessions*.jsonl", "*agents/main/sessions/*.jsonl",
                   "*logs*.log", "*evaluator*sessions*.jsonl",
                   "*logs/trajectories/*query*.json",
                   "*profiles/assistant*/sessions/*.json",
                   "*profiles/main/sessions/*.json",
                   "*profiles/assistant*/state.db*", "*profiles/main/state.db*",
                   "*projects/*/*.jsonl"]  # claude-code: projects/<ws>/<session>.jsonl
        exclude = ["*.trajectory.jsonl", "*_use.log", "*profiles/*/logs/*", "*_logs/*.log"]
    run_obsutil_cp(obsutil, task_obs, dest, recursive=True,
                   include=include, exclude=exclude)
    # 主 log + gateway（openclaw）
    for rel in (LOG_CANDIDATES[0], GATEWAY_LOG_REL, EVAL_USE_LOG_REL):
        dpath = os.path.join(dest, *rel.split("/"))
        os.makedirs(os.path.dirname(dpath), exist_ok=True)
        try:
            run_obsutil_cp(obsutil, task_obs + rel, dpath)
        except RuntimeError:
            continue
    return dest


# ============ 状态表（task_status） ============

def build_task_status_rows(detail: dict, filter_level: str | None = None,
                           sort_by: str = "level") -> list[dict]:
    """单任务状态行：按轨迹逐条给出 {name, level, tool_calls, plain_rounds, completion, has_eval}。

    数据源 = load_task_detail（本地已缓存 cache/<batch>/<leaf>/ 的 assistant/evaluator 轨迹）。
    """
    rows = []
    stats = detail.get("assistant_stats") or {}
    # detail["assistant_trajectory"] 是解析后的 blocks 列表；轨迹路径在 trajectories 里取主 assistant
    name = detail.get("task") or ""
    for t in detail.get("trajectories") or []:
        if t.get("role") == "assistant":
            name = t["path"]
            break
    lvl = stats.get("level") or compute_level(
        detail.get("harness", "openclaw"), stats.get("tool_calls", 0),
        stats.get("plain_rounds", 0), detail.get("verdict", {}).get("completion"))
    rows.append({
        "name": name, "level": lvl,
        "tool_calls": stats.get("tool_calls"), "plain_rounds": stats.get("plain_rounds"),
        "completion": detail.get("verdict", {}).get("completion"),
        "has_eval": detail.get("verdict", {}).get("has_eval"),
    })
    if filter_level:
        level_rank = {"L0": 0, "L1": 1, "L1.5": 2, "L2": 3, "L3": 4}
        rows = [r for r in rows
                if level_rank.get(r.get("level", "L0"), 0) >= level_rank.get(filter_level, 0)]
    if sort_by == "completion":
        rows = sorted(rows, key=lambda r: (r.get("completion") is None, r.get("completion") or 0),
                      reverse=True)
    elif sort_by == "task":
        rows = sorted(rows, key=lambda r: r.get("name", ""))
    else:  # level
        rows = sorted(rows, key=lambda r: level_rank.get(r.get("level", "L0"), 0), reverse=True)
    return rows


def build_task_status_table(obsutil: str, batch_obs: str, origin: str,
                            obs_cred_args: list[str] | None = None,
                            concurrency: int = 8, max_tasks: int = 0,
                            filter_level: str | None = None,
                            sort_by: str = "level") -> list[dict]:
    """整批状态表：task | harness | level | tool_calls | plain_rounds | completion | task_done。

    快路径优先（复用 build_batch_summary 的 per_session），支持按 Lx 过滤 / 排序。
    """
    stats = build_batch_summary(obsutil, batch_obs, origin, obs_cred_args,
                                concurrency=concurrency, max_tasks=max_tasks)
    rows = stats.get("per_session", [])
    if filter_level:
        level_rank = {"L0": 0, "L1": 1, "L1.5": 2, "L2": 3, "L3": 4}
        rows = [r for r in rows if level_rank.get(r.get("level", "L0"), 0) >= level_rank.get(filter_level, 0)]
    if sort_by == "completion":
        rows = sorted(rows, key=lambda r: (r.get("completion") is None, r.get("completion") or 0),
                      reverse=True)
    elif sort_by == "task":
        rows = sorted(rows, key=lambda r: r.get("session", ""))
    else:  # level
        rows = sorted(rows, key=lambda r: level_rank.get(r.get("level", "L0"), 0), reverse=True)
    return rows


# ============ 缓存核查（verify_api_and_cache） ============

def verify_local_cache(task_dir: str, task_obs: str, obsutil: str,
                       obs_cred_args: list[str] | None = None) -> list[dict]:
    """校验本地 cache/<task相对路径>/ 缓存与 OBS 远端一致性。

    返回 [{file, remote_exists, remote_size, local_exists, local_size, match, note}]。
    """
    task_obs = task_obs if task_obs.endswith("/") else task_obs + "/"
    report = []
    # 本地缓存文件（相对 task_dir）
    local_files = []
    if os.path.isdir(task_dir):
        for root, _, files in os.walk(task_dir):
            for fn in files:
                ap = os.path.join(root, fn)
                rel = os.path.relpath(ap, task_dir)
                local_files.append((rel, ap))

    # 关键文件集合（与「加载分层」一致）
    key_rels = [TSR_REL, "workdir/run.log", GATEWAY_LOG_REL, EVAL_USE_LOG_REL,
                "agents/main/sessions/", "profiles/main/sessions/"]
    for rel, ap in local_files:
        if rel.startswith(("agents/", "profiles/")):
            # 轨迹文件: 远端按相对路径查
            remote_url = task_obs + rel
            r_exist = _obs_remote_exists(obsutil, remote_url)
            r_size = _obs_remote_size(obsutil, remote_url)
        else:
            r_exist = _obs_remote_exists(obsutil, task_obs + rel)
            r_size = _obs_remote_size(obsutil, task_obs + rel)
        l_size = os.path.getsize(ap)
        report.append({
            "file": rel,
            "remote_exists": r_exist, "remote_size": r_size,
            "local_exists": True, "local_size": l_size,
            "match": (r_size == l_size) if r_exist else False,
            "note": "" if (r_exist and r_size == l_size) else
                    ("MISSING_REMOTE" if not r_exist else "MISMATCH"),
        })
    return report


def _obs_remote_exists(obsutil: str, obj_url: str) -> bool:
    try:
        lines = run_obsutil_ls(obsutil, obj_url)
        return any(obj_url.rstrip("/") in ln for ln in lines)
    except RuntimeError:
        return False


def _obs_remote_size(obsutil: str, obj_url: str) -> int | None:
    """obsutil ls 单对象行含大小（列: 大小 类型 最后修改 对象名）。解析失败返回 None。"""
    try:
        lines = run_obsutil_ls(obsutil, obj_url)
    except RuntimeError:
        return None
    for ln in lines:
        ln = ln.strip()
        if obj_url.rstrip("/") in ln:
            parts = ln.split()
            # 形如 "2026-08-20 19:52:38 16371B  对象"
            for tok in parts:
                if tok.endswith("B") and tok[:-1].replace(".", "").isdigit():
                    s = tok[:-1]
                    try:
                        return int(float(s)) if "." in s else int(s)
                    except ValueError:
                        return None
    return None
