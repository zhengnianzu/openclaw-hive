# -*- coding: utf-8 -*-
"""API Key 等敏感值的统一脱敏。

原先 `mask_secret` 住在 task_export.py（导出 Excel 专用），但实例详情的
`/create-params` 与 `/configs/{filename}` 两个接口也要用同一口径脱敏，
故抽到这里共享。task_export 继续从本模块重新导出，保持既有调用不变。

口径只有一处定义：掩码规则改这里，导出和详情页同时生效。
"""
from __future__ import annotations

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
