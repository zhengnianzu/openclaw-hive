"""Harness 元配置加载器 — 从 platform/settings/harness_config.json 派生所有机械映射。

schema 字段: id / name / hive_config / user_config / agent_config / ui_color。
新增 harness 只需在 JSON 加一条, 前后端自动获取:
  - 后端: EXPECTED_FILES / ALLOWED_CONFIG_FILES / traj_prefixes / agent_config 路径
  - 前端: 下拉选项 / 颜色 / 标签 / tagType / fileLabel

traj_prefix 由代码派生(claude-code→cc_trajs 特例, 其余 {id}_trajs);
harness_local_config_file 直接用 agent_config 拼路径, 不再需要路径变量名映射。
不可机械映射的逻辑(各 harness 配置文件生成, 格式各异)仍保留在 instances.py。
"""
from __future__ import annotations

import json
import os
from functools import lru_cache

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..core.config import settings
from ..core.security import get_current_user

router = APIRouter(prefix="/api/harness-meta", tags=["harness-meta"])

_CONFIG_PATH = os.path.join(settings.SETTINGS_DIR, "harness_config.json")

# traj_prefix 代码派生: claude-code 用 cc_trajs, 其余 {id}_trajs
_TRAJ_SPECIAL = {"claude-code": "cc_trajs"}
# md 模板目录默认与 harness id 同名（settings/harness_mds/<id>/）。
# 这里只登记「目录名 != id」的例外；旧 jiuwenswarm 标识复用 jiuwenswarm 目录。
_MD_TEMPLATE_ALIASES: dict[str, str] = {}


@lru_cache(maxsize=1)
def _load() -> dict:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _entries() -> dict:
    return {k: v for k, v in _load().items()
            if not k.startswith("_") and isinstance(v, dict)}


def _listable() -> dict:
    meta = _load().get("_meta", {})
    excluded = set(meta.get("exclude_from_lists", []))
    return {k: v for k, v in _entries().items() if k not in excluded}


def listable_types() -> list[str]:
    return list(_listable().keys())


def get(harness_type: str) -> dict:
    return _entries().get(harness_type, {})


# ---- 后端派生 ----

def agent_config(harness_type: str) -> str | None:
    return get(harness_type).get("agent_config")


def EXPECTED_FILES() -> dict:
    """每个 harness 期望的配置文件: [agent_config?, hive_config, user_config]。"""
    result = {}
    for k, v in _entries().items():
        files = []
        if v.get("agent_config"):
            files.append(v["agent_config"])
        files.append(v["hive_config"])
        files.append(v["user_config"])
        result[k] = files
    return result


def ALLOWED_CONFIG_FILES() -> set:
    """所有 agent_config 去重 + hive_config + user_config。"""
    files = set()
    for v in _entries().values():
        for f in (v.get("hive_config"), v.get("user_config"), v.get("agent_config")):
            if f:
                files.add(f)
    return files


# ---- harness md 模板 ----

HARNESS_MDS_DIR = os.path.join(settings.SETTINGS_DIR, "harness_mds")


def md_templates(harness_type: str) -> list[str]:
    """该 harness 可用的 md 模板文件名。

    只取顶层 *.md，不递归：jiuwenswarm/memory/MEMORY.md 这类子目录模板
    本期不支持（历史上递归扫描曾把 settings/ 整个卷进去，产生过 404 层嵌套目录）。
    """
    d = os.path.join(HARNESS_MDS_DIR, _MD_TEMPLATE_ALIASES.get(harness_type, harness_type))
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d)
                  if f.lower().endswith(".md") and os.path.isfile(os.path.join(d, f)))


def md_template_path(harness_type: str, filename: str) -> str | None:
    """模板绝对路径；文件名不在白名单内返回 None（防穿越）。"""
    if filename not in md_templates(harness_type):
        return None
    template_type = _MD_TEMPLATE_ALIASES.get(harness_type, harness_type)
    return os.path.join(HARNESS_MDS_DIR, template_type, filename)


def traj_prefix(harness_type: str) -> str:
    """代码派生: claude-code→cc_trajs, 其余 {id}_trajs, 兜底 openclaw_trajs。"""
    if harness_type in _TRAJ_SPECIAL:
        return _TRAJ_SPECIAL[harness_type]
    return f"{harness_type}_trajs"


# ---- 前端 meta 端点 ----

class HarnessMeta(BaseModel):
    types: list[str]
    colors: dict[str, str]
    labels: dict[str, str]
    fileLabels: dict[str, str]
    agentConfig: dict[str, str | None]
    # 每个 harness 可用的 md 模板文件名（来自 settings/harness_mds/<type>/）
    mdFiles: dict[str, list[str]] = {}


@router.get("", response_model=HarnessMeta)
def get_harness_meta(user: dict = Depends(get_current_user)):
    entries = _listable()
    return HarnessMeta(
        types=list(entries.keys()),
        colors={k: v.get("ui_color", "#909399") for k, v in entries.items()},
        labels={k: v.get("name", k) for k, v in entries.items()},
        fileLabels={k: f"Harness ({v.get('agent_config', '')})" for k, v in entries.items()},
        agentConfig={k: v.get("agent_config") for k, v in entries.items()},
        mdFiles={k: md_templates(k) for k in entries},
    )
