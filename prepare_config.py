"""Local agent config preparation, framework-aware but self-contained.

本模块是 harness 配置的单一数据源:``_FRAMEWORK_LAYOUTS`` 在此定义,
hive.py 反向 import 它。新增 harness 只需在本文件修改两处:
  1. ``_FRAMEWORK_LAYOUTS`` 加一个 key;
  2. ``prepare_local_agent_config`` 加对应的 framework 分支(若需要本地 mutate)。
"""
from __future__ import annotations
import copy
import json
import logging
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import List, Optional
import tomllib
import tomli_w

from omegaconf import OmegaConf

logger = logging.getLogger("env_sdk_client")


# 每框架的目录布局 — 用这个 dict 派生所有路径默认值。
# hive.py 的 SandboxConfig / _FW / set_agent_framework 都从这里取数。
_FRAMEWORK_LAYOUTS: dict = {
    "openclaw": {
        "harness_dir":            "/home/ma-user/.openclaw",
        "harness_local_config":   "uploads/openclaw.json",
        "harness_sandbox_config": "/home/ma-user/.openclaw/openclaw.json",
        "upload_paths": [
            "/home/ma-user/.openclaw/agents",
        ],
        "workspace_base": "/home/ma-user/.openclaw/workspace",
        "skill_subdir": "skills",
    },
    # 不支持TOOL.md
    "hermes": {
        "harness_dir":            "/home/ma-user/.hermes",
        "harness_local_config":   "uploads/config.yaml",
        "harness_sandbox_config": "/home/ma-user/.hermes/config.yaml",
        "upload_paths": [
            # 每个 profile (<agent-name>) 只保存 sessions / logs / workspace
            "/home/ma-user/.hermes/profiles/*/sessions",
            "/home/ma-user/.hermes/profiles/*/logs",
            "/home/ma-user/.hermes/profiles/*/workspace",
            "/home/ma-user/.hermes/profiles/*/state.db",
        ],
        # workspace 隔离在 profiles/<name>/ 里
        "workspace_base": None,
        "skill_subdir": "skills",
    },
    # 只支持CLAUDE.md
    "claude-code": {
        "harness_dir":            "/home/ma-user/.claude",
        "harness_local_config":   "uploads/settings.json",
        "harness_sandbox_config": "/home/ma-user/.claude/settings.json",
        "upload_paths": [
            "/home/ma-user/.claude/projects",
            "/home/ma-user/.claude/todos",
            "/home/ma-user/.claude/session-env",
        ],
        "workspace_base": "/home/ma-user/.claude/workspace",
        "skill_subdir": ".claude/skills",
    },
    "openjiuwen": {
        "harness_dir":            "/home/ma-user/.openjiuwen",
        "harness_local_config":   "uploads/openjiuwen.json",
        "harness_sandbox_config": "/home/ma-user/.openjiuwen/openjiuwen.json",
        "upload_paths": [
           "/home/ma-user/.openjiuwen/sessions"
        ],
        "workspace_base": "/home/ma-user/.openjiuwen/workspace",
        "skill_subdir": "skills",
    },
    # agent路径：~/.config/opencode/agents/*.md
    "opencode": {
        "harness_dir":            "/home/ma-user/.config/opencode",
        "harness_local_config":   "uploads/opencode.json",
        "harness_sandbox_config": "/home/ma-user/.config/opencode/opencode.json",
        "upload_paths": [
            "/home/ma-user/.local/share/opencode/opencode.db",
            "/home/ma-user/.local/share/opencode/log"
        ],
        "workspace_base": "/home/ma-user/.config/opencode/workspace",
        "skill_subdir": ".opencode/skills",
    },
    "codex": {
        "harness_dir":            "/home/ma-user/.codex",
        "harness_local_config":   "uploads/config.toml",
        "harness_sandbox_config": "/home/ma-user/.codex/config.toml",
        "upload_paths": [
           "/home/ma-user/.codex/sessions"
        ],
        "workspace_base": "/home/ma-user/.codex/workspace",
        "skill_subdir": ".agents/skills",
    },
    "pi": {
        "harness_dir":            "/home/ma-user/.pi/agent",
        "harness_local_config":   "uploads/models.json",
        "harness_sandbox_config": "/home/ma-user/.pi/agent/models.json",
        "upload_paths": [
           "/home/ma-user/.pi/agent/sessions"
        ],
        "workspace_base": "/home/ma-user/.pi/workspace",
        "skill_subdir": ".agents/skills",
    },
    "grok": {
        "harness_dir":            "/home/ma-user/.grok",
        "harness_local_config":   "uploads/grok/config.toml",
        "harness_sandbox_config": "/home/ma-user/.grok/config.toml",
        "upload_paths": [
           "/home/ma-user/.grok/sessions",
        ],
        "workspace_base": "/home/ma-user/.grok/workspace",
        "skill_subdir": "skills",
    },
    "dsh": {
        "harness_dir":            "/home/ma-user/.dsh",
        "harness_local_config":   "uploads/dsh_settings.yaml",
        "harness_sandbox_config": "/home/ma-user/.dsh/settings.yaml",
        "upload_paths": [
           "/home/ma-user/.dsh/sessions",
        ],
        "workspace_base": "/home/ma-user/.dsh/workspace",
        "skill_subdir": ".agents/skills",
    },

}


def adapt_layout_for_windows(layout: dict, win_home: str) -> dict:
    """把 Linux 风格 layout 的 /home/ma-user/... 路径适配为 Windows 路径。"""
    out = copy.deepcopy(layout)
    for key in ("harness_dir", "harness_sandbox_config", "workspace_base"):
        p = layout.get(key, "")
        if p and p.startswith("/home/ma-user"):
            out[key] = str(PureWindowsPath(win_home).joinpath(
                PurePosixPath(p).relative_to("/home/ma-user")))
    out["upload_paths"] = [
        str(PureWindowsPath(win_home).joinpath(
            PurePosixPath(p).relative_to("/home/ma-user")))
        if p and p.startswith("/home/ma-user") else p
        for p in layout.get("upload_paths", [])
    ]
    return out


def normalize_base_url(base_url: str, api_kind: str = "openai-completions") -> str:
    """按 api_kind 归一化 base_url 的 /v1 后缀。"""
    base_url = base_url.rstrip("/")
    if api_kind == "openai-completions" and not base_url.endswith("/v1"):
        base_url += "/v1"
    if api_kind == "anthropic-messages" and base_url.endswith("/v1"):
        base_url = base_url.removesuffix("/v1")
    return base_url


def load_yaml_config(config_file: str):
    """Load and return an OmegaConf config object."""
    return OmegaConf.load(config_file)


def _read_agent_system_prompts(data_config_file: Optional[str]) -> dict:
    """从 data_config JSON 里抽 {agent.name: agent.system_prompt}。

    opencode 分支需要 per-task 的 agent system_prompt,以前依赖 hive 的
    parse_data_config/DataConfig;这里只读 JSON 的 agents 字段,无 hive 依赖。
    """
    if not data_config_file:
        return {}
    try:
        with open(data_config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"read agent system_prompt failed: {e}")
        return {}
    return {
        agent.get("name", ""): agent.get("system_prompt", "")
        for agent in (data.get("agents") or [])
    }


def prepare_local_agent_config(
    sandbox_config,
    framework: str,
    data_config_file: Optional[str] = None,
    local_config: Optional[str] = None,
    local_model_config: Optional[str] = None,
    suffix: str = "",
    apply_suffix: bool = False,
) -> None:
    """在 spawn worker 之前把本地 agent 配置文件按 framework 修改到位。

    Args:
        sandbox_config: SandboxConfig 实例 (harness_local_config_file /
            user_proxy_model_local_file)
        framework: 当前 agent 框架名 (openclaw/hermes/dsh/...)
        data_config_file: opencode 分支需要 data_config 里的 agent system_prompt
        local_config: 本次任务的 openclaw.json 独立副本 (空则回落 sandbox_config 里的共享路径)
        local_model_config: 本次任务的 user_proxy_model.json 独立副本
        suffix: 沙箱标识后缀 (env_id 的 uuid 段), apply_suffix 为真时追加到模型 key
        apply_suffix: 是否给模型 key 追加后缀 (对应 task.suffix_api_keys)
    """
    local_path = local_config or sandbox_config.harness_local_config_file
    local_model_path = local_model_config or sandbox_config.user_proxy_model_local_file
    model_cfg = json.loads(Path(local_model_path).read_text(encoding="utf-8"))

    # 先改本地 user_proxy_model.json，api-key补全后缀
    if apply_suffix:
        for agent_name, agent in model_cfg.items():
            agent["api_key"] += f"_{suffix}"
            logger.info(
                f"user_proxy_model.json#{agent_name} api_key changed to {agent['api_key']}"
            )
        Path(local_model_path).write_text(
            json.dumps(model_cfg, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        
    if framework == "openjiuwen":
        # 1) openjiuwen.json: 归一化 default.provider 里的 anthropic -> Anthropic
        data = json.loads(Path(local_path).read_text(encoding="utf-8"))
        if "anthropic" in data["default"]["provider"]:
            data["default"]["provider"] = "Anthropic"
        # 模型 key 追加沙箱标识后缀
        if apply_suffix:
            data["default"]["api_key"] += f"_{suffix}"
            logger.info(f"openjiuwen default api_key changed to {data["default"]["api_key"]}")
        Path(local_path).write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        # 2) 修改 user_proxy_model_local_file
        for agent in model_cfg.values():
            if "anthropic" in agent.get("api", ""):
                agent["api"] = "Anthropic"
        Path(local_model_path).write_text(
            json.dumps(model_cfg, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    elif framework == "opencode":
        # opencode.json: agents.system_prompt 和 agent:evaluator 注入
        data = json.loads(Path(local_path).read_text(encoding="utf-8"))
        providers = data.setdefault("provider", {})
        agents = data.setdefault("agent", {})

        # opencode 需要把 data_config 里 agent.system_prompt 注入到 agent.prompt
        agent_sp = _read_agent_system_prompts(data_config_file)

        # 更新主agent的api-key
        if apply_suffix:
            main_provider_key = "local-provider"
            providers[main_provider_key]["options"]["apiKey"] += f"_{suffix}"
            logger.info(f"opencode local-provider api_key changed to {providers[main_provider_key]["options"]["apiKey"]}")
            Path(local_path).write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

        # 更新 provider 和 agent
        for agent_name, cfg in model_cfg.items():
            if agent_name == "user_simulator":
                continue
            if not (cfg.get("model") and cfg.get("base_url")
                    and cfg.get("api_key") and cfg.get("provider")):
                logger.warning(
                    f"agent {agent_name} missing model/base_url/api_key/provider, skip"
                )
                continue

            provider_key = cfg["provider"]
            model_name = cfg["model"]
            api_key = cfg["api_key"]
            api_kind = cfg.get("api", "openai-completions")
            base_url = cfg["base_url"].rstrip('/')
            if not base_url.endswith('/v1'):
                base_url = base_url + "/v1"

            if provider_key not in providers:
                npm_pkg = ("@ai-sdk/openai-compatible"
                           if "openai" in api_kind.lower()
                           else "@ai-sdk/anthropic")
                providers[provider_key] = {
                    "name": provider_key,
                    "npm": npm_pkg,
                    "options": {"baseURL": base_url, "apiKey": api_key},
                    "models": {model_name: {"name": model_name}},
                }
                logger.info(f"Added provider: {provider_key}")
            else:
                existing_models = providers[provider_key].setdefault("models", {})
                if model_name not in existing_models:
                    existing_models[model_name] = {"name": model_name}
                    logger.info(f"Added model {model_name} to existing provider {provider_key}")

            model_ref = f"{provider_key}/{model_name}"
            if agent_name in agents:
                agents[agent_name]["model"] = model_ref
                if agent_name in agent_sp:
                    agents[agent_name]["prompt"] = agent_sp.get(agent_name) or ""
                logger.info(f"Updated agent {agent_name} with model {model_ref}")
            else:
                agents[agent_name] = {
                    "model": model_ref,
                    "prompt": agent_sp.get(agent_name) or ""
                }
                logger.info(f"Added new agent {agent_name} with model {model_ref}")

        Path(local_path).write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logger.info(f"Updated opencode.json with provider/agent from {local_model_path}")

    elif framework == "pi":
        # models.json: 注入 provider
        data = json.loads(Path(local_path).read_text(encoding="utf-8"))
        providers = data.setdefault("providers", {})

        for agent_name, cfg in model_cfg.items():
            if agent_name == "user_simulator":
                continue
            if not (cfg.get("model") and cfg.get("base_url")
                    and cfg.get("api_key") and cfg.get("provider")):
                logger.warning(
                    f"agent {agent_name} missing model/base_url/api_key/provider, skip"
                )
                continue

            provider_key = cfg["provider"]
            model_name = cfg["model"]
            api_kind = cfg.get("api", "openai-completions")
            base_url = normalize_base_url(cfg["base_url"], api_kind)

            if provider_key not in providers:
                providers[provider_key] = {
                    "baseUrl": base_url,
                    "api": api_kind,
                    "apiKey": cfg["api_key"],
                    "models": [
                        {
                            "id": model_name,
                            "name": model_name,
                            "reasoning": False,
                            "input": ["text"],
                            "contextWindow": 200000,
                            "maxTokens": 327680,
                            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                        }
                    ],
                }
                logger.info(f"Added pi provider: {provider_key} ({model_name})")
            else:
                existing_models = providers[provider_key].setdefault("models", [])
                existing_ids = {m.get("id") for m in existing_models if isinstance(m, dict)}
                if model_name not in existing_ids:
                    existing_models.append({"id": model_name})
                    logger.info(
                        f"Added model {model_name} to existing pi provider {provider_key}"
                    )
        # 配置主agent
        if apply_suffix:
            main_provider_key = "custom"
            providers[main_provider_key]["apiKey"] += f"_{suffix}"
            logger.info(f"pi custom api_key changed to {providers[main_provider_key]["apiKey"]}")
            
        Path(local_path).write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logger.info(f"Updated models.json with provider from {local_model_path}")

    elif framework == "codex":
        # [model_providers.<key>] 追加到 config.toml, 已存在则跳过。
        # 走 tomllib 解析 + tomli_w 回写: 通过 dict 判断 provider 是否已存在,
        data = tomllib.loads(Path(local_path).read_text(encoding="utf-8"))
        providers = data.setdefault("model_providers", {})
        new_providers: List[str] = []

        for agent_name, cfg in model_cfg.items():
            if agent_name == "user_simulator":
                continue
            if not (cfg.get("model") and cfg.get("base_url")
                    and cfg.get("api_key") and cfg.get("provider")):
                logger.warning(
                    f"agent {agent_name} missing model/base_url/api_key/provider, skip"
                )
                continue

            provider_key = cfg["provider"]
            if provider_key in providers:
                logger.info(
                    f"codex provider {provider_key} already exists in config.toml, skip"
                )
                continue

            base_url = cfg["base_url"].rstrip("/")
            if not base_url.endswith("/v1"):
                base_url = base_url + "/v1"

            providers[provider_key] = {
                "name": provider_key,
                "base_url": base_url,
                "experimental_bearer_token": cfg["api_key"],
                "wire_api": "responses",
            }
            new_providers.append(provider_key)
            logger.info(f"Added codex provider: {provider_key} (agent={agent_name})")

        # 配置主agent
        if apply_suffix:
            main_provider_key = "custom"
            providers[main_provider_key]["experimental_bearer_token"] += f"_{suffix}"
            logger.info(f"codex custom api_key changed to {providers[main_provider_key]["experimental_bearer_token"]}")
        
        if new_providers or apply_suffix:
            with open(local_path, "wb") as f:
                tomli_w.dump(data, f)

    elif framework == "grok":
        # [model.<provider>] 追加到 config.toml, 已存在则跳过。
        data = tomllib.loads(Path(local_path).read_text(encoding="utf-8"))
        models = data.setdefault("model", {})
        new_providers: List[str] = []

        for agent_name, cfg in model_cfg.items():
            if agent_name == "user_simulator":
                continue
            if not (cfg.get("model") and cfg.get("base_url")
                    and cfg.get("api_key") and cfg.get("provider")):
                logger.warning(
                    f"agent {agent_name} missing model/base_url/api_key/provider, skip"
                )
                continue

            provider_key = cfg["provider"]
            if provider_key in models:
                logger.info(
                    f"grok provider {provider_key} already exists in config.toml, skip"
                )
                continue

            base_url = cfg["base_url"].rstrip("/")
            if not base_url.endswith("/v1"):
                base_url = base_url + "/v1"

            model_name = cfg["model"]
            models[provider_key] = {
                "model": model_name,
                "name": model_name,
                "base_url": base_url,
                "api_key": cfg["api_key"],
            }
            new_providers.append(provider_key)
            logger.info(f"Added grok provider: {provider_key} (agent={agent_name})")

        # 配置主agent
        if apply_suffix:
            model_section = models["default-model"]
            model_section["api_key"] += f"_{suffix}"
            logger.info(f"grok default-model api_key changed to {model_section["api_key"]}")
        
        if new_providers or apply_suffix:
            with open(local_path, "wb") as f:
                tomli_w.dump(data, f)

    elif framework == "dsh":
        # llm-pi-ai.providers 追加 provider 到 settings.yaml, 已存在则跳过
        data = load_yaml_config(local_path)
        # 循环体里赋值会让它在循环外变成局部未绑定(agent 全被 skip 时直接 UnboundLocalError),
        # 且 apply_suffix 段要用同一份引用, 因此提到循环外只取一次。
        providers = (data.get("llm-pi-ai") or {}).get("providers") or {}
        credentials = data.get("credentials") or {}

        for agent_name, cfg in model_cfg.items():
            if agent_name == "user_simulator":
                continue
            if not (cfg.get("model") and cfg.get("base_url")
                    and cfg.get("api_key") and cfg.get("provider")):
                logger.warning(
                    f"agent {agent_name} missing model/base_url/api_key/provider, skip"
                )
                continue

            provider_key = cfg["provider"]
            api_kind = cfg.get("api", "openai-completions")
            base_url = normalize_base_url(cfg["base_url"], api_kind)
            api_key = cfg["api_key"]
            model_name = cfg["model"]

            if provider_key not in providers:
                api_key_env = provider_key.replace('-', '_').upper() + "_API_KEY"
                providers[provider_key] = {
                    "displayName": provider_key,
                    "apiKeyEnv": api_key_env,
                    "api": api_kind,
                    "baseURL": base_url,
                    "models": [
                        {
                            "id": model_name,
                        }
                    ],
                }
                credentials[api_key_env] = api_key
                logger.info(f"Added dsh provider: {provider_key} ({model_name})")
            else:
                existing_models = providers[provider_key].setdefault("models", [])
                existing_ids = {m.get("id") for m in existing_models if hasattr(m, "get")}
                if model_name not in existing_ids:
                    existing_models.append({"id": model_name})
                    logger.info(
                        f"Added model {model_name} to existing dsh provider {provider_key}"
                    )

        if apply_suffix:
            main_provider_key = "custom"
            api_key_env = providers[main_provider_key]["apiKeyEnv"]
            credentials[api_key_env] += f"_{suffix}"
            logger.info(f"dsh {api_key_env} changed to {credentials[api_key_env]}")

        OmegaConf.save(data, local_path)
        logger.info(f"Updated settings.yaml with provider from {local_model_path}")

    elif framework == "openclaw":
        data = json.loads(Path(local_path).read_text(encoding="utf-8"))
        data.setdefault("models", {}).setdefault("mode", "merge")
        providers = data["models"].setdefault("providers", {})
        agents_section = data.setdefault("agents", {})
        agents_list = agents_section.setdefault("list", [])

        for agent_name, cfg in model_cfg.items():
            if agent_name == "user_simulator":
                continue
            if not (cfg.get("model") and cfg.get("base_url")
                    and cfg.get("api_key") and cfg.get("provider")):
                logger.warning(
                    f"agent {agent_name} missing model/base_url/api_key/provider, skip"
                )
                continue

            api_kind = cfg.get("api") or "anthropic-messages"
            provider_key = cfg["provider"]
            model_ref = f"{provider_key}/{cfg['model']}"

            providers[provider_key] = {
                "baseUrl": cfg["base_url"],
                "apiKey": cfg["api_key"],
                "api": api_kind,
                "timeoutSeconds": 300000,
                "models": [
                    {
                        "id": cfg["model"],
                        "name": cfg["model"],
                        "api": api_kind,
                        "reasoning": True,
                        "input": ["text"],
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                        "contextWindow": 200000,
                        "maxTokens": 327680,
                        "compat": {"maxTokensField": "max_tokens"},
                    }
                ],
            }

            entry = {
                "id": agent_name,
                "name": agent_name,
                "workspace": f"/home/ma-user/.openclaw/workspace-{agent_name}",
                "agentDir": f"/home/ma-user/.openclaw/agents/{agent_name}",
                "model": {"primary": model_ref},
                "models": {model_ref: {}},
            }
            idx = next(
                (i for i, e in enumerate(agents_list)
                 if isinstance(e, dict) and e.get("id") == agent_name),
                None,
            )
            if idx is None:
                agents_list.append(entry)
            else:
                agents_list[idx] = {**agents_list[idx], **entry}

            logger.info(
                f"Injected provider={provider_key} model={model_ref} for agent {agent_name}"
            )

        if apply_suffix:
            data["models"]["providers"]["local"]["apiKey"] += f"_{suffix}"
            logger.info(f"openclaw local provider apiKey changed to {data["models"]["providers"]["local"]["apiKey"]}")

        Path(local_path).write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    elif framework == "hermes":
        if apply_suffix:
            data = load_yaml_config(local_path)
            data.model.api_key += f"_{suffix}"
            logger.info(f"hermes api_key changed to {data.model.api_key}")
            OmegaConf.save(data, local_path)
    
    elif framework == "claude-code":
        if apply_suffix:
            data = json.loads(Path(local_path).read_text(encoding="utf-8"))
            data["env"]["ANTHROPIC_AUTH_TOKEN"] += f"_{suffix}"
            logger.info(f"claude-code ANTHROPIC_AUTH_TOKEN changed to {data["env"]["ANTHROPIC_AUTH_TOKEN"]}")
            Path(local_path).write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )