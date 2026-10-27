# -*- coding: utf-8 -*-
"""对外 API（/openapi/v1）：给外部调用方按 AK 查询任务沙箱输出路径。

与 /api/* 的区别：
  - 鉴权走固定 AK（settings.PUBLIC_API_KEYS，X-API-Key 头），不涉及用户身份/JWT；
  - 不依赖 platform.db（接口要在库还没同步时也能用），直接从实例目录的
    configs/**/*.json 与 config.yaml 推导。

「q1」的定义：任务配置文件里 queries[0].text，即该任务的首条 query 正文。
「沙箱输出路径」：hive.py 上传轨迹时用的 OBS 目录
    os.path.join(obs_config.traj_save_path, Path(config_file).stem)
（hive.py:1106），bucket 取自实例 config.yaml 的 run_config.obs.traj_save_bucket。
"""
from __future__ import annotations

import json
import os
import secrets
import time
import unicodedata
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from ..core.config import settings

router = APIRouter(prefix="/openapi/v1", tags=["public-api"])

# ============ 鉴权 ============


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """固定 AK 校验。逐个常量时间比较，避免长度/前缀泄漏。"""
    configured = settings.public_api_keys
    if not configured:
        raise HTTPException(status_code=503, detail="对外 API 未启用（服务端未配置 PUBLIC_API_KEYS）")
    if not x_api_key:
        raise HTTPException(status_code=401, detail="缺少 X-API-Key 请求头")
    ok = False
    # 不能短路：每个 key 都比较一次，避免按耗时猜中前缀
    for key in configured:
        if secrets.compare_digest(key, x_api_key):
            ok = True
    if not ok:
        raise HTTPException(status_code=401, detail="API key 无效")


# ============ 索引 ============

# text(归一化) -> [(instance_id, config_abs_path), ...]
_index: dict[str, list[tuple[str, str, str]]] = {}
_index_at: float = 0.0
_index_signature: tuple = ()
_INDEX_TTL = 60.0

_TASK_JSON_MARKER = b'"queries"'
# configs/ 下 1.5 万个 json 共约 780MB，其中真正的任务配置只有几十个、且都在 3KB 以内。
# 只读文件头做预筛，把整读量从 780MB 压到几十 MB（实测 12.5s → <1s）。
_HEAD_BYTES = 64 * 1024
_MAX_TASK_CONFIG_BYTES = 1024 * 1024
# 列表接口回显 q1 的上限。真实数据里最长 104 字，但外部语料不可控：
# 一条几百字的 query × 上千个任务会白白吃掉带宽。
Q1_MAX_CHARS = 200


def _normalize(text: str) -> str:
    """NFKC + 统一换行 + 合并连续空白 + 去首尾。"""
    t = unicodedata.normalize("NFKC", text or "")
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    return " ".join(t.split())


def _instance_signature() -> tuple:
    """各实例 configs 目录的 (路径, mtime) 快照，用于判断索引是否要重建。"""
    root = Path(settings.INSTANCE_DIR)
    if not root.is_dir():
        return ()
    sig = []
    for d in root.iterdir():
        cfg = d / "configs"
        if cfg.is_dir():
            try:
                sig.append((d.name, cfg.stat().st_mtime))
            except OSError:
                continue
    return tuple(sorted(sig))


def _build_index() -> tuple[dict[str, list[tuple[str, str, str]]], int]:
    """扫描 instances/*/configs/**/*.json，建「首条 query → 任务配置」索引。

    真实数据里 configs/ 下绝大多数 json 是 eval 中间产物（transcript/result/raw
    request 等），只有任务配置含 queries 数组。先做字节预筛再解析，避免
    对 1.5 万个文件逐个 json.loads。
    """
    root = Path(settings.INSTANCE_DIR)
    index: dict[str, list[tuple[str, str, str]]] = {}
    skipped = 0
    if not root.is_dir():
        return index, skipped

    for path in root.glob("*/configs/**/*.json"):
        try:
            with open(path, "rb") as f:
                head = f.read(_HEAD_BYTES)
                if _TASK_JSON_MARKER not in head:
                    continue
                # 头部命中：任务配置本身只有几 KB；超过 1MB 的一定不是，跳过
                raw = head + f.read(_MAX_TASK_CONFIG_BYTES)
        except OSError:
            continue
        if len(raw) > _MAX_TASK_CONFIG_BYTES:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        queries = data.get("queries") if isinstance(data, dict) else None
        if not isinstance(queries, list) or not queries:
            continue
        first = queries[0]
        if not isinstance(first, dict):
            continue
        text = first.get("text")
        if not isinstance(text, str) or not text.strip():
            skipped += 1
            continue
        instance_id = path.relative_to(root).parts[0]
        # 值里存原文：列表接口要回显用户熟悉的那份文本（归一化会改全角标点）
        index.setdefault(_normalize(text), []).append((instance_id, str(path), text))
    return index, skipped


def _reset_caches() -> None:
    """清空索引与路径前缀缓存（测试用；换 INSTANCE_DIR 后必须调）。"""
    global _index, _index_at, _index_signature
    _index = {}
    _index_at = 0.0
    _index_signature = ()
    _obs_prefix_cache.clear()


def _get_index() -> dict[str, list[tuple[str, str, str]]]:
    """带 TTL + 目录 mtime 签名的进程内缓存。"""
    global _index, _index_at, _index_signature
    now = time.time()
    sig = _instance_signature()
    fresh = (now - _index_at) < _INDEX_TTL and sig == _index_signature and _index_at > 0
    if fresh:
        return _index
    _index, _ = _build_index()
    _obs_prefix_cache.clear()  # 索引重建时 config.yaml 可能已变，路径前缀不能沿用
    _index_at = now
    _index_signature = sig
    return _index


# ============ 路径推导 ============


def _instance_dir_of(config_abs_path: str) -> Path:
    """configs 下任意深度 → 实例根目录 instances/<id>。

    注意不能用 resolve()：instances/<id>/configs 常常是指向
    platform/downloads/configs/... 的符号链接，resolve 后就不在 instances 下了。
    这里只做字面路径切分。
    """
    root = Path(settings.INSTANCE_DIR).absolute()
    rel = Path(config_abs_path).absolute().relative_to(root)
    return root / rel.parts[0]


def _obs_output_path(config_abs_path: str) -> str:
    """按 hive.py 的规则拼该任务的 OBS 轨迹目录。"""
    instance_dir = _instance_dir_of(config_abs_path)
    bucket, traj_path = _obs_prefix(instance_dir, config_abs_path)
    stem = Path(config_abs_path).stem
    # 与 hive.py:1106 的 os.path.join(traj_save_path, stem) 同构；
    # os.path.join 只做字符串拼接，不会动 bucket 里的 "obs://" 前缀。
    return os.path.join(str(bucket), str(traj_path).strip("/"), stem)


# 实例根目录 -> (bucket, traj_path)。列表接口一次要算几十个任务，
# 同一实例会被反复用到，而每个都要读一遍 config.yaml。
_obs_prefix_cache: dict[str, tuple[str, str]] = {}


def _obs_prefix(instance_dir: Path, config_abs_path: str) -> tuple[str, str]:
    cache_key = str(instance_dir)
    cached = _obs_prefix_cache.get(cache_key)
    if cached is not None:
        return cached
    config_yaml = instance_dir / "config.yaml"
    if not config_yaml.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"实例配置文件不存在：{config_yaml.name}（实例 {instance_dir.name}）",
        )
    from omegaconf import OmegaConf

    cfg = OmegaConf.load(str(config_yaml))
    bucket = OmegaConf.select(cfg, "run_config.obs.traj_save_bucket") or ""
    traj_path = OmegaConf.select(cfg, "run_config.obs.traj_save_path") or ""
    if not bucket or not traj_path:
        raise HTTPException(
            status_code=502,
            detail=(
                f"实例 {instance_dir.name} 的 config.yaml 缺少 "
                "run_config.obs.traj_save_bucket / traj_save_path，无法确定输出路径"
            ),
        )
    value = (str(bucket), str(traj_path))
    _obs_prefix_cache[cache_key] = value
    return value


# ============ 定位 ============


def _resolve_hits(q1: str | None, task: str | None, instance_id: str | None):
    """把查询条件收敛成命中的 [(实例, 路径, 原文)]，条件不成立时抛 400。"""
    if not q1 and not task:
        raise HTTPException(status_code=400, detail="需要提供 q1 或 task 之一")

    if task:
        # 按 task 名查：走全索引扫一遍（只有几十到几千条，够用），
        # 不另建反向索引以免多一份要失效的缓存。
        wanted = task.strip()
        hits = [
            h for hits in _get_index().values() for h in hits
            if Path(h[1]).stem == wanted
        ]
        if instance_id:
            hits = [h for h in hits if h[0] == instance_id]
        if not hits:
            raise HTTPException(status_code=404, detail=f"task={wanted} 未匹配到任何任务")
        return hits

    key = _normalize(q1)
    if not key:
        raise HTTPException(status_code=400, detail="q1 不能为空")
    hits = _get_index().get(key) or []
    if instance_id:
        hits = [h for h in hits if h[0] == instance_id]
    if not hits:
        raise HTTPException(status_code=404, detail="q1 未匹配到任何任务")
    return hits


# ============ 端点 ============


@router.get("/task-output-path")
def task_output_path(
    q1: str | None = Query(default=None, description="任务首条 query 的完整正文（config json 里 queries[0].text）"),
    task: str | None = Query(default=None, description="config 文件名去后缀，如 001（与 q1 二选一）"),
    instance_id: str | None = Query(default=None, description="同一 q1 命中多条时用它收敛"),
    _: None = Depends(require_api_key),
):
    """按 q1（或 task + instance_id）返回该任务的沙箱输出路径（OBS 轨迹目录）。

    成功：{"task": "<config 文件名去后缀>", "output_path": "obs://.../<task>"}

    列表接口的 q1 默认截断到 200 字，被截断的文本在这里匹配不上；
    那种情况下用列表返回的 task + instance_id 来查。
    """
    hits = _resolve_hits(q1=q1, task=task, instance_id=instance_id)

    if len(hits) > 1:
        # 同一道题会在多个实例里重复出现（同一批数据跑了多次），不能替调用方挑一个
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"q1 匹配到 {len(hits)} 个任务，请加 instance_id 参数收敛",
                "candidates": [
                    {"task": Path(p).stem, "instance_id": iid} for iid, p, _t in hits
                ],
            },
        )

    instance_id, config_abs_path, _raw_text = hits[0]
    return {
        "task": Path(config_abs_path).stem,
        "output_path": _obs_output_path(config_abs_path),
    }


@router.get("/list_key_tasks")
@router.get("/list_key_tasks/", include_in_schema=False)
def list_key_tasks(
    instance_id: str | None = Query(default=None, description="只看某个实例下的任务"),
    q1_max_chars: int = Query(
        default=Q1_MAX_CHARS, ge=0, le=10000,
        description="q1 回显截断长度（0 = 不截断）。只影响展示，不影响查询",
    ),
    _: None = Depends(require_api_key),
):
    """列出所有任务的首条 query（q1）及其沙箱输出路径。

    同一 q1 会在多个实例重复出现（同一批语料跑了多次），所以按 (实例, 任务) 逐个返回，
    不合并。q1 默认截断到 200 字并带 q1_truncated 标记——被截断的文本不能拿去
    /task-output-path 匹配，用返回里的 task + instance_id 查即可。
    """
    index = _get_index()
    items = []
    for _norm_text, hits in index.items():
        for iid, config_abs_path, raw_text in hits:
            if instance_id and iid != instance_id:
                continue
            # 回显原文而不是归一化文本：归一化会把全角 ？／， 改成半角，用户对照不上
            shown = raw_text
            truncated = False
            if q1_max_chars and len(raw_text) > q1_max_chars:
                shown = raw_text[:q1_max_chars] + "…"
                truncated = True
            item = {
                "task": Path(config_abs_path).stem,
                "instance_id": iid,
                "q1": shown,
                "q1_truncated": truncated,
            }
            try:
                item["output_path"] = _obs_output_path(config_abs_path)
            except HTTPException as e:
                # 单个实例的 config.yaml 有问题不该让整个列表 500
                item["output_path"] = None
                item["error"] = e.detail
            items.append(item)

    items.sort(key=lambda d: (d["instance_id"], d["task"]))
    return {"total": len(items), "items": items}
