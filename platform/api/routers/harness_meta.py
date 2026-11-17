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


# ---- 模型连通性验证（新建实例页「验证模型」按钮） ----
#
# 新建实例时 base_url / key / model_id 填错不会有任何即时反馈，要等沙箱里 harness 真的
# 去调模型失败才知道，一次无效实例就是一整轮任务白跑。这里用一次最小的真实调用做冒烟探测。
#
# 协议与 /v1 的口径沿用 instances.py 里各 harness 的既有约定：
# openai-completions 带 /v1，anthropic-messages 不带（见 instances.py:1188-1192 等）。

# 探测提示词：只要模型回一个 OK，token 消耗压到最小
_PROBE_PROMPT = "回复 OK 就行"
_PROBE_MAX_TOKENS = 16
# 小于前端 axios 的 30s 超时，保证是后端的报错原文回到页面，而不是 axios 先超时
_PROBE_TIMEOUT = 20
# 上游报错体可能很长（网关爱回整页 HTML），原样返回但做长度上限
_ERROR_MAX_LEN = 4000
_REPLY_MAX_LEN = 200


class ValidateModelRequest(BaseModel):
    model_base_url: str = ""
    model_api_key: str = ""
    model_api_type: str = ""
    model_id: str = ""


class ValidateModelResult(BaseModel):
    ok: bool = False
    tried: list[str] = []
    used: str = ""
    request_url: str = ""
    reply: str = ""
    error: str = ""


def _probe_protocols(api_type: str) -> list[str]:
    """按「API 类型」决定尝试顺序；未指定则先 anthropic 再 openai。

    子串 + 忽略大小写匹配：openjiuwen 的选项值是 Anthropic/OpenAI，其余 harness 是
    anthropic-messages/openai-completions，用相等判断会把 openjiuwen 误判成未指定。
    """
    text = (api_type or "").strip().lower()
    if "anthropic" in text:
        return ["anthropic"]
    if "openai" in text:
        return ["openai"]
    return ["anthropic", "openai"]


def _redact(text: str, secret: str) -> str:
    """上游报错体有时会把 key 原样回显，这里脱敏后再返回给前端。"""
    if not text:
        return ""
    if secret and len(secret) >= 8:
        text = text.replace(secret, secret[:4] + "****" + secret[-4:])
    elif secret:
        text = text.replace(secret, "****")
    if len(text) > _ERROR_MAX_LEN:
        text = text[:_ERROR_MAX_LEN] + f"\n...(已截断，共 {len(text)} 字符)"
    return text


def _probe_payload(proto: str, base: str, model_id: str, api_key: str) -> tuple:
    """返回 (url, headers, json_body)。"""
    body = {
        "model": model_id,
        "max_tokens": _PROBE_MAX_TOKENS,
        "messages": [{"role": "user", "content": _PROBE_PROMPT}],
    }
    if proto == "anthropic":
        return (
            f"{base}/v1/messages",
            {"x-api-key": api_key, "anthropic-version": "2023-06-01",
             "content-type": "application/json"},
            body,
        )
    return (
        f"{base}/v1/chat/completions",
        {"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
        body,
    )


def _extract_reply(proto: str, data: dict) -> str:
    """从响应里取出模型回复文本；取不到就回整个 JSON 的简短形态。"""
    try:
        if proto == "anthropic":
            blocks = data.get("content") or []
            texts = [b.get("text", "") for b in blocks if isinstance(b, dict)]
            text = "".join(t for t in texts if t)
        else:
            text = (((data.get("choices") or [{}])[0].get("message") or {})
                    .get("content") or "")
    except Exception:
        text = ""
    if not text:
        text = json.dumps(data, ensure_ascii=False)
    text = str(text).strip()
    return text[:_REPLY_MAX_LEN] if len(text) > _REPLY_MAX_LEN else text


@router.post("/validate-model", response_model=ValidateModelResult)
async def validate_model(req: ValidateModelRequest,
                         user: dict = Depends(get_current_user)):
    """用表单里的 base_url/key/model_id 实打一次模型，验证是否可用。

    一律返回 200：前端 axios 对非 2xx 会弹全局 toast（api/index.js），而报错需要内联
    原样展示，所以成败放在 body 的 ok 字段里。
    """
    import httpx

    from ..services.proxy import normalize_base_url

    base_url = (req.model_base_url or "").strip()
    api_key = (req.model_api_key or "").strip()
    model_id = (req.model_id or "").strip()

    missing = []
    if not base_url:
        missing.append("模型 Base URL")
    if not api_key:
        missing.append("模型 API Key")
    if not model_id:
        missing.append("模型 ID")
    if missing:
        return ValidateModelResult(
            ok=False,
            error="请先填写：" + "、".join(missing)
                  + "\n（套用模板后 API Key 会被清空，需要重新填写或点「新建KEY」）",
        )

    base = normalize_base_url(base_url)
    protocols = _probe_protocols(req.model_api_type)

    tried: list[str] = []
    errors: list[str] = []
    last_url = ""

    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
        for proto in protocols:
            tried.append(proto)
            url, headers, body = _probe_payload(proto, base, model_id, api_key)
            last_url = url
            try:
                resp = await client.post(url, headers=headers, json=body)
            except Exception as e:
                errors.append(f"[{proto}] {url}\n{type(e).__name__}: {e}")
                continue

            if resp.status_code // 100 != 2:
                errors.append(f"[{proto}] {url}\nHTTP {resp.status_code}\n{resp.text}")
                continue

            try:
                data = resp.json()
            except Exception as e:
                errors.append(f"[{proto}] {url}\nHTTP {resp.status_code} "
                              f"响应不是合法 JSON：{type(e).__name__}: {e}\n{resp.text}")
                continue

            return ValidateModelResult(
                ok=True, tried=tried, used=proto, request_url=url,
                reply=_extract_reply(proto, data),
            )

    return ValidateModelResult(
        ok=False, tried=tried, request_url=last_url,
        error=_redact("\n\n".join(errors), api_key),
    )
