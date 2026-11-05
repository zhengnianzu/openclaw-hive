# -*- coding: utf-8 -*-
"""API Key 等敏感值的统一脱敏。

原先 `mask_secret` 住在 task_export.py（导出 Excel 专用），但实例详情的
`/create-params` 与 `/configs/{filename}` 两个接口也要用同一口径脱敏，
故抽到这里共享。task_export 继续从本模块重新导出，保持既有调用不变。

口径只有一处定义：掩码规则改这里，导出和详情页同时生效。
"""
from __future__ import annotations

import json

MASK_PLACEHOLDER = "****"

# create_params 里承载 key 的字段。顶层两个 + agents[] 里的 api_key。
# 抽样 400 个实例遍历全部字段路径确认：除这三处外没有别的 key 字段。
CREATE_PARAM_KEY_FIELDS = ("model_api_key", "user_proxy_api_key")
AGENT_KEY_FIELD = "api_key"


def mask_secret(value) -> str:
    """API Key 等敏感值的统一掩码。

    与前端 maskKey（InstanceDetail.vue）的展示效果一致，但更严格：
    前端对长度 <= 8 的值原样返回，这里一律整体打码，不泄漏短 key。

    幂等：已经是掩码形态的值再传进来不会被二次打码（前端兜底会再调一次）。
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    if s == MASK_PLACEHOLDER:
        return s
    if len(s) <= 8:
        return MASK_PLACEHOLDER
    return s[:4] + "***" + s[-4:]


def mask_create_params(params: dict) -> dict:
    """返回 params 的副本，三处 key 字段换成掩码值。

    不改入参：调用方（task_export / 复制实例接口）仍需要原文，
    而 agents 是 json.loads 出来的嵌套 list，浅拷贝会让下面改 agent dict
    时污染到同一份对象，故 agents 逐项重建。
    """
    if not isinstance(params, dict):
        return params
    masked = dict(params)
    for field in CREATE_PARAM_KEY_FIELDS:
        if masked.get(field):
            masked[field] = mask_secret(masked[field])
    agents = masked.get("agents")
    if isinstance(agents, list):
        new_agents = []
        for a in agents:
            if isinstance(a, dict) and a.get(AGENT_KEY_FIELD):
                a = {**a, AGENT_KEY_FIELD: mask_secret(a[AGENT_KEY_FIELD])}
            new_agents.append(a)
        masked["agents"] = new_agents
    return masked


def collect_secret_values(params: dict) -> list[str]:
    """抽出该实例全部非空 key 原文，按长度降序去重返回。

    给「配置文件」tab 做原文替换用：这些文件本来就是后端拿这几个 key 填出来的，
    原文在手上，不必去 13 种 harness 格式里猜字段名在哪
    （同 hive.py 给 key 打沙箱后缀时的做法：直接替换文件里出现的原始 key）。

    降序是必须的：若某个 key 恰好是另一个的前缀，先替短的会把长的替成
    「掩码+残留尾巴」，残留部分仍是原文片段。
    """
    if not isinstance(params, dict):
        return []
    values = [params.get(f) for f in CREATE_PARAM_KEY_FIELDS]
    for a in (params.get("agents") or []):
        if isinstance(a, dict):
            values.append(a.get(AGENT_KEY_FIELD))
    seen = set()
    out = []
    for v in values:
        if not isinstance(v, str):
            continue
        v = v.strip()
        # 太短的值当不了可靠的替换目标：会在正常内容里误命中（比如 api 类型名）。
        if len(v) <= 8 or v in seen:
            continue
        seen.add(v)
        out.append(v)
    out.sort(key=len, reverse=True)
    return out


def redact_text(content: str, params: dict) -> str:
    """把配置文件全文里出现的原始 key 替换成掩码值。

    params 为空 / 非法时原样返回 content（历史实例可能没有 create_params，
    不该因此 500 —— 与 batch_output 的静默降级口径一致）。
    """
    if not content:
        return content
    for raw in collect_secret_values(params):
        content = content.replace(raw, mask_secret(raw))
    return content


# ---------------------------------------------------------------------------
# 沙箱配置（sandbox_configs）的脱敏
#
# 与 create_params 不同：这里的 key 不来自实例入参，而是沙箱 yaml 自身的
# global.api_key 与 sandbox[].sandbox.env[]（SERPER_API_KEY / SEARCH_API_KEY /
# VL_API_KEY 等）。字段名无法穷举，故按「名字里带 KEY/TOKEN/SECRET」判定。
# ---------------------------------------------------------------------------

# env 变量名命中即视为敏感。PASSWORD 一并纳入：同类凭据，漏判的代价不对称。
_SENSITIVE_ENV_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def is_sensitive_env_name(name) -> bool:
    """env 变量名是否该脱敏。大小写无关，子串命中即算。"""
    if not name:
        return False
    upper = str(name).upper()
    return any(h in upper for h in _SENSITIVE_ENV_HINTS)


def mask_env_list(env) -> list:
    """沙箱 env 列表（[{name, value}, ...]）里敏感项的 value 换成掩码。

    不改入参：逐项重建 dict。非 list / 非 dict 项原样保留，
    让界面仍能看到结构（与 mask_create_params 的降级口径一致）。
    """
    if not isinstance(env, list):
        return env
    out = []
    for item in env:
        if isinstance(item, dict) and is_sensitive_env_name(item.get("name")) and item.get("value"):
            item = {**item, "value": mask_secret(item["value"])}
        out.append(item)
    return out


def collect_sandbox_secret_values(cfg: dict) -> list[str]:
    """抽出沙箱 yaml 里全部非空 key 原文，按长度降序去重返回。

    给 /file 接口做整份 yaml 的原文替换用 —— yaml 结构多变（resources 两种
    写法、boot_source 可选），逐字段遍历容易漏；拿到原文直接全文替换更稳。
    降序理由同 collect_secret_values：短 key 若是长 key 的前缀，先替短的会留下原文尾巴。
    """
    if not isinstance(cfg, dict):
        return []
    gl = cfg.get("global")
    values = [gl.get("api_key") if isinstance(gl, dict) else None]
    sandboxes = cfg.get("sandbox")
    for sb in (sandboxes.values() if isinstance(sandboxes, dict) else ()):
        if not isinstance(sb, dict):
            continue
        inner = sb.get("sandbox")
        if not isinstance(inner, dict):
            continue
        env = inner.get("env")
        for item in (env if isinstance(env, list) else ()):
            if isinstance(item, dict) and is_sensitive_env_name(item.get("name")):
                values.append(item.get("value"))
    seen = set()
    out = []
    for v in values:
        if not isinstance(v, str):
            continue
        v = v.strip()
        # <=8 的值不参与全文替换：沙箱 env 里有 "on"/"off"/"sk-xxx" 这类短值，
        # 拿它们当替换目标会命中正文里的正常内容（把 "off" 替成掩码）。
        # 代价是摘要接口按字段打码（不看长度）、本函数按原文替换（跳过短值），
        # 对长度<=8 的值两边不一致。可接受：这种长度的不是真凭据，是占位符。
        if len(v) <= 8 or v in seen:
            continue
        seen.add(v)
        out.append(v)
    out.sort(key=len, reverse=True)
    return out


def redact_sandbox_yaml(content: str, cfg: dict) -> str:
    """把沙箱 yaml 全文里出现的 key 原文替换成掩码值。

    短值（<=8 字符）不参与替换，理由同 collect_secret_values：
    会在正常内容里误命中。这类值本就不是真 key（占位符 sk-xxx 之类）。
    """
    if not content:
        return content
    for raw in collect_sandbox_secret_values(cfg):
        content = content.replace(raw, mask_secret(raw))
    return content


def instance_create_params(instance_id: str) -> dict:
    """该实例的 create_params（原始 dict）；缺失或非法 JSON 返回空 dict。

    只给脱敏用：拿不到就是没 key 可遮，不该让调用方 500
    （与 batch_output 对非法 create_params 静默降级的口径一致）。

    放在本模块而不是某个 router：instances.py 的「配置文件」tab 与
    logs.py 的 obs-view 都要用同一份 key 清单来脱敏，
    从 router 互相 import 会让两个路由模块耦合。
    """
    from ..core.database import get_connection  # 延迟导入：避免模块级循环依赖

    with get_connection() as conn:
        row = conn.execute(
            "SELECT create_params FROM task_instances WHERE id=?", (instance_id,)
        ).fetchone()
    if not row or not row["create_params"]:
        return {}
    try:
        params = json.loads(row["create_params"])
    except (ValueError, TypeError):
        return {}
    return params if isinstance(params, dict) else {}
