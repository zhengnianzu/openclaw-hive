"""Local agent config preparation, framework-aware but self-contained.

本模块是 harness 配置的单一数据源:``_FRAMEWORK_LAYOUTS`` 在此定义,
hive.py 反向 import 它。新增 harness 只需在本文件修改两处:
  1. ``_FRAMEWORK_LAYOUTS`` 加一个 key;
  2. ``prepare_local_agent_config`` 加对应的 framework 分支(若需要本地 mutate)。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Optional

from omegaconf import OmegaConf


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
           "/home/ma-user/.local/share/opencode"
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
        logging.warning(f"read agent system_prompt failed: {e}")
        return {}
    return {
        agent.get("name", ""): agent.get("system_prompt", "")
        for agent in (data.get("agents") or [])
    }


def prepare_local_agent_config(
    sandbox_config,
    framework: str,
    data_config_file: Optional[str] = None,
) -> None:
    """在 spawn worker 之前把本地 agent 配置文件按 framework 修改到位。

    Args:
        sandbox_config: SandboxConfig 实例 (harness_local_config_file /
            user_proxy_model_local_file)
        framework: 当前 agent 框架名 (openclaw/hermes/dsh/...)
        data_config_file: opencode 分支需要 data_config 里的 agent system_prompt
    """
    local_path = sandbox_config.harness_local_config_file
    local_model_path = sandbox_config.user_proxy_model_local_file
    model_cfg = json.loads(Path(local_model_path).read_text(encoding="utf-8"))

    if framework == "openjiuwen":
        # 1) openjiuwen.json: 归一化 default.provider 里的 anthropic -> Anthropic
        data = json.loads(Path(local_path).read_text(encoding="utf-8"))
        if "anthropic" in data["default"]["provider"]:
            data["default"]["provider"] = "Anthropic"
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

        # 更新 provider 和 agent
        for agent_name, cfg in model_cfg.items():
            if agent_name == "user_simulator":
                continue
            if not (cfg.get("model") and cfg.get("base_url")
                    and cfg.get("api_key") and cfg.get("provider")):
                logging.warning(
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
                logging.info(f"Added provider: {provider_key}")
            else:
                existing_models = providers[provider_key].setdefault("models", {})
                if model_name not in existing_models:
                    existing_models[model_name] = {"name": model_name}
                    logging.info(f"Added model {model_name} to existing provider {provider_key}")

            model_ref = f"{provider_key}/{model_name}"
            if agent_name in agents:
                agents[agent_name]["model"] = model_ref
                if agent_name in agent_sp:
                    agents[agent_name]["prompt"] = agent_sp.get(agent_name) or ""
                logging.info(f"Updated agent {agent_name} with model {model_ref}")
            else:
                agents[agent_name] = {
                    "model": model_ref,
                    "prompt": agent_sp.get(agent_name) or ""
                }
                logging.info(f"Added new agent {agent_name} with model {model_ref}")

        Path(local_path).write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logging.info(f"Updated opencode.json with provider/agent from {local_model_path}")

    elif framework == "pi":
        # models.json: 注入 provider
        data = json.loads(Path(local_path).read_text(encoding="utf-8"))
        providers = data.setdefault("providers", {})

        for agent_name, cfg in model_cfg.items():
            if agent_name == "user_simulator":
                continue
            if not (cfg.get("model") and cfg.get("base_url")
                    and cfg.get("api_key") and cfg.get("provider")):
                logging.warning(
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
                logging.info(f"Added pi provider: {provider_key} ({model_name})")
            else:
                existing_models = providers[provider_key].setdefault("models", [])
                existing_ids = {m.get("id") for m in existing_models if isinstance(m, dict)}
                if model_name not in existing_ids:
                    existing_models.append({"id": model_name})
                    logging.info(
                        f"Added model {model_name} to existing pi provider {provider_key}"
                    )

        Path(local_path).write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logging.info(f"Updated models.json with provider from {local_model_path}")

    elif framework == "codex":
        # [model_providers.<key>] 追加到 config.toml, 已存在则跳过
        toml_text = Path(local_path).read_text(encoding="utf-8")
        new_blocks: List[str] = []

        for agent_name, cfg in model_cfg.items():
            if agent_name == "user_simulator":
                continue
            if not (cfg.get("model") and cfg.get("base_url")
                    and cfg.get("api_key") and cfg.get("provider")):
                logging.warning(
                    f"agent {agent_name} missing model/base_url/api_key/provider, skip"
                )
                continue

            provider_key = cfg["provider"]
            if f"[model_providers.{provider_key}]" in toml_text:
                logging.info(
                    f"codex provider {provider_key} already exists in config.toml, skip"
                )
                continue

            base_url = cfg["base_url"].rstrip("/")
            if not base_url.endswith("/v1"):
                base_url = base_url + "/v1"
            api_key = cfg["api_key"]

            block = (
                f"[model_providers.{provider_key}]\n"
                f'name     = "{provider_key}"\n'
                f'base_url = "{base_url}"\n'
                f'experimental_bearer_token = "{api_key}"\n'
                f'wire_api = "responses"\n'
            )
            new_blocks.append(block)
            logging.info(f"Added codex provider: {provider_key} (agent={agent_name})")

        if new_blocks:
            toml_text = toml_text.rstrip() + "\n\n" + "\n".join(new_blocks)
            Path(local_path).write_text(toml_text, encoding="utf-8")
            logging.info(
                f"Updated config.toml with {len(new_blocks)} provider(s) from {local_model_path}"
            )

    elif framework == "grok":
        # [model.<provider>] 追加到 config.toml, 已存在则跳过
        toml_text = Path(local_path).read_text(encoding="utf-8")
        new_blocks: List[str] = []

        for agent_name, cfg in model_cfg.items():
            if agent_name == "user_simulator":
                continue
            if not (cfg.get("model") and cfg.get("base_url")
                    and cfg.get("api_key") and cfg.get("provider")):
                logging.warning(
                    f"agent {agent_name} missing model/base_url/api_key/provider, skip"
                )
                continue

            provider_key = cfg["provider"]
            if f"[model.{provider_key}]" in toml_text:
                logging.info(
                    f"grok provider {provider_key} already exists in config.toml, skip"
                )
                continue

            base_url = cfg["base_url"].rstrip("/")
            if not base_url.endswith("/v1"):
                base_url = base_url + "/v1"
            api_key = cfg["api_key"]

            block = (
                f"[model.{provider_key}]\n"
                f'model     = "{cfg.get("model")}"\n'
                f'name      = "{cfg.get("model")}"\n'
                f'base_url  = "{base_url}"\n'
                f'api_key   = "{api_key}"\n'
            )
            new_blocks.append(block)
            logging.info(f"Added grok provider: {provider_key} (agent={agent_name})")

        if new_blocks:
            toml_text = toml_text.rstrip() + "\n\n" + "\n".join(new_blocks)
            Path(local_path).write_text(toml_text, encoding="utf-8")
            logging.info(
                f"Updated config.toml with {len(new_blocks)} provider(s) from {local_model_path}"
            )

    elif framework == "dsh":
        # llm-pi-ai.providers 追加 provider 到 settings.yaml, 已存在则跳过
        data = load_yaml_config(local_path)

        for agent_name, cfg in model_cfg.items():
            if agent_name == "user_simulator":
                continue
            if not (cfg.get("model") and cfg.get("base_url")
                    and cfg.get("api_key") and cfg.get("provider")):
                logging.warning(
                    f"agent {agent_name} missing model/base_url/api_key/provider, skip"
                )
                continue

            provider_key = cfg["provider"]
            providers = (data.get("llm-pi-ai") or {}).get("providers") or {}
            api_kind = cfg.get("api", "openai-completions")
            base_url = normalize_base_url(cfg["base_url"], api_kind)
            api_key = cfg["api_key"]
            model_name = cfg["model"]
            credentials = data.get("credentials") or {}

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
                logging.info(f"Added dsh provider: {provider_key} ({model_name})")
            else:
                existing_models = providers[provider_key].setdefault("models", [])
                existing_ids = {m.get("id") for m in existing_models if hasattr(m, "get")}
                if model_name not in existing_ids:
                    existing_models.append({"id": model_name})
                    logging.info(
                        f"Added model {model_name} to existing dsh provider {provider_key}"
                    )
        OmegaConf.save(data, local_path)
        logging.info(f"Updated settings.yaml with provider from {local_model_path}")

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
                logging.warning(
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

            logging.info(
                f"Injected provider={provider_key} model={model_ref} for agent {agent_name}"
            )

        Path(local_path).write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
