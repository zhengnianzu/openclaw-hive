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
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import List, Optional

import yaml
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
    # OpenClaw TUI 复用原生 ~/.openclaw 配置和会话目录，但不启动 Gateway。
    # 本地模板使用独立文件名，避免平台配置方案与 gateway 型 openclaw 互相覆盖。
    "openclaw-tui": {
        "harness_dir":            "/home/ma-user/.openclaw",
        "harness_local_config":   "uploads/openclaw_tui.json",
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
    # 保留旧标识，仅用于兼容历史 config / 仍在用的旧实例；新任务用下面两个明确版本。
    "jiuwenswarm": {
        "harness_dir":            "/home/ma-user/.jiuwenswarm",
        "harness_local_config":   "uploads/jw_config.yaml",
        "harness_sandbox_config": "/home/ma-user/.jiuwenswarm/config/config.yaml",
        "upload_paths": [
            "/home/ma-user/.jiuwenswarm",
        ],
        "workspace_base": "/home/ma-user/.jiuwenswarm/agent/jiuwenclaw_workspace",
        "skill_subdir": "skills",
    },
    "jiuwenswarm-dev": {
        "harness_dir":            "/home/ma-user/.jiuwenswarm",
        "harness_local_config":   "uploads/jw_dev_config.yaml",
        "harness_sandbox_config": "/home/ma-user/.jiuwenswarm/config/config.yaml",
        "upload_paths": [
            "/home/ma-user/.jiuwenswarm/service_default/.logs",
            "/home/ma-user/.jiuwenswarm/service_default/agent_default/agent/sessions"
        ],
        "workspace_base": "/home/ma-user/.jiuwenswarm/service_default/agent_default/agent/jiuwenclaw_workspace",
        "skill_subdir": "skills",
    },
    "jiuwenswarm-xiaoyi": {
        "harness_dir":            "/home/ma-user/.jiuwenswarm",
        "harness_local_config":   "uploads/jw_xiaoyi_config.yaml",
        "harness_sandbox_config": "/home/ma-user/.jiuwenswarm/config/config.yaml",
        "upload_paths": [
            "/home/ma-user/.jiuwenswarm/logs",
            "/home/ma-user/.jiuwenswarm/agent/.logs",
            "/home/ma-user/.jiuwenswarm/agent/sessions",
        ],
        "workspace_base": "/home/ma-user/.jiuwenswarm/agent/workspace",
        "skill_subdir": "skills",
    },

}

def adapt_layout_for_windows(layout: dict, win_home: str) -> dict:
    """把 Linux 风格 layout 的 /home/ma-user/... 路径适配为 Windows 路径。

    用 PurePosixPath 拆出相对部分, 再用 PureWindowsPath 拼回去。
    win_home 是沙箱 Windows home (config 的 run_config.sandbox.home),
    不再硬编码, 换镜像/换用户时只改配置。
    harness_local_config 是本地侧路径, skill_subdir 是相对路径, 均不变。
    """
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
        logging.warning(f"read agent system_prompt failed: {e}")
        return {}
    return {
        agent.get("name", ""): agent.get("system_prompt", "")
        for agent in (data.get("agents") or [])
    }


def prepare_local_agent_config(
    framework: str,
    data_config_file: Optional[str] = None,
    local_path: Optional[str] = None,
    local_user_proxy_model_path: Optional[str] = None,
    home: str = "/home/ma-user",
    sandbox_type: str = "x86_cpu",
    oc_agent_names: Optional[list] = [],
) -> None:
    """在 spawn worker 之前把本地 agent 配置文件按 framework 修改到位。

    Args:
        framework: 当前 agent 框架名 (openclaw/openclaw-tui/hermes/dsh/...)
        data_config_file: opencode/openclaw/openclaw-tui 分支需要读 data_config 里的 agents
        home: 沙箱内的 home 根
        sandbox_type: 沙箱类型 ("windows" | "x86_cpu"), 决定 home 路径归一化形态
    """
    model_cfg = json.loads(Path(local_user_proxy_model_path).read_text(encoding="utf-8"))

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
        Path(local_user_proxy_model_path).write_text(
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
        logging.info(f"Updated opencode.json with provider/agent from {local_user_proxy_model_path}")

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
        logging.info(f"Updated models.json with provider from {local_user_proxy_model_path}")

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
                f"Updated config.toml with {len(new_blocks)} provider(s) from {local_user_proxy_model_path}"
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
                f"Updated config.toml with {len(new_blocks)} provider(s) from {local_user_proxy_model_path}"
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
        logging.info(f"Updated settings.yaml with provider from {local_user_proxy_model_path}")

    elif framework in {"jiuwenswarm", "jiuwenswarm-dev", "jiuwenswarm-xiaoyi"}:
        # jw_config.yaml: 把 user_proxy_model.json 的
        # 每个模型追加成 models.defaults 的一条, 用 alias 区分; 默认模型靠
        # is_default: true 标记, 其余段(permissions 等)原样保留。
        data = yaml.safe_load(Path(local_path).read_text(encoding="utf-8")) or {}
        models = data.setdefault("models", {})
        defaults = models.setdefault("defaults", [])
        entries = [e for e in defaults if isinstance(e, dict)]

        default_entry = next((e for e in entries if e.get("is_default")), None)

        # 新条目沿用默认模型条目的可选字段, 不硬编码。
        # 注意别往 model_config_obj 塞 context_window: 它会被并进 extra_params,
        # 再传给 openai SDK 的 chat.completions.create(), 后者不认这个键, 直接 TypeError。
        proto_mcc = (default_entry or {}).get("model_client_config") or {}
        proto_mcc_obj = (default_entry or {}).get("model_config_obj") or {}

        # jiuwenswarm 拒绝 alias 与任何条目的 model_name 撞名, 一并纳入校验。
        taken_names = {
            n for e in entries
            for n in (str(e.get("alias") or "").strip(),
                      str((e.get("model_client_config") or {}).get("model_name") or "").strip())
            if n
        }

        for agent_name, cfg in model_cfg.items():
            if agent_name == "user_simulator":
                continue
            if not (cfg.get("model") and cfg.get("base_url") and cfg.get("api_key")):
                logging.warning(
                    f"agent {agent_name} missing model/base_url/api_key, skip"
                )
                continue
            if agent_name in taken_names:
                logging.info(
                    f"jiuwenswarm model alias={agent_name} already exists, skip"
                )
                continue

            api_kind = cfg.get("api", "openai-completions")
            client_provider = (
                "Anthropic" if "anthropic" in api_kind.lower() else "OpenAI"
            )

            defaults.append({
                "model_client_config": {
                    "api_base": normalize_base_url(cfg["base_url"], api_kind),
                    "api_key": cfg["api_key"],
                    "model_name": cfg["model"],
                    "client_provider": client_provider,
                    "timeout": proto_mcc.get("timeout", 1800),
                    "verify_ssl": proto_mcc.get("verify_ssl", True),
                    "stream_first_chunk_timeout":
                        proto_mcc.get("stream_first_chunk_timeout", 300),
                    "stream_idle_timeout":
                        proto_mcc.get("stream_idle_timeout", 120),
                    "custom_headers": copy.deepcopy(proto_mcc.get("custom_headers", {})),
                },
                "model_config_obj": copy.deepcopy(proto_mcc_obj),
                "is_default": False,
                "alias": agent_name,
            })
            taken_names.add(agent_name)
            logging.info(
                f"Added jiuwenswarm model: alias={agent_name} "
                f"model={cfg['model']} provider={client_provider}"
            )

        Path(local_path).write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        logging.info(
            f"Updated jw_config.yaml with models from {local_user_proxy_model_path}"
        )

    elif framework in {"openclaw", "openclaw-tui"}:
        data = json.loads(Path(local_path).read_text(encoding="utf-8"))
        data.setdefault("models", {}).setdefault("mode", "merge")
        providers = data["models"].setdefault("providers", {})
        agents_section = data.setdefault("agents", {})

        # 两代 agent schema, 按模板里有没有 entries 键判定(不依赖版本号文本):
        #   - 10.6: agents.entries 按 agent 名键控的对象;
        #   - 6.6:  agents.list 数组, 条目带 id 字段。
        entries_mode = isinstance(agents_section.get("entries"), dict)
        agents_entries = agents_section.setdefault("entries", {}) if entries_mode else None
        agents_list = (
            agents_section.setdefault("list", [])
            if not entries_mode else None
        )

        # 按 sandbox_type 归一化 home 路径: windows 用 PureWindowsPath(反斜杠), 其余用 PurePosixPath
        path_cls = PureWindowsPath if sandbox_type == "windows" else PurePosixPath

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

            workspace = str(path_cls(home, ".openclaw", f"workspace-{agent_name}"))

            if entries_mode:
                # entries 的键就是 agent 名, 条目里不再写 id/name。
                # agentDir 由下方统一按 10.6 的 agents/<id>/agent 补齐。
                agents_entries[agent_name] = {
                    **agents_entries.get(agent_name, {}),
                    "workspace": workspace,
                    "model": {"primary": model_ref},
                    "models": {model_ref: {}},
                }
            else:
                agent_dir = str(path_cls(home, ".openclaw", "agents", agent_name))
                entry = {
                    "id": agent_name,
                    "name": agent_name,
                    "workspace": workspace,
                    "agentDir": agent_dir,
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

        # 补注册 oc_agent_names 里传入但配置中还没有的 agent。
        # workspace 仍按 <base>-<name> 派生, 与 harness 的命名规则一致。
        added: List[str] = []
        for name in (oc_agent_names or []):
            name = str(name).strip()
            if not name:
                continue
            if entries_mode:
                if name in agents_entries:
                    continue
                agents_entries[name] = {
                    "workspace": str(path_cls(home, ".openclaw", f"workspace-{name}")),
                }
            else:
                if any(isinstance(e, dict) and e.get("id") == name
                       for e in agents_list):
                    continue
                agents_list.append({
                    "id": name,
                    "workspace": str(path_cls(home, ".openclaw", f"workspace-{name}")),
                })
            added.append(name)
        if added:
            logging.info(f"Registered agents from oc_agent_names: {added}")

        # 归一化 agentDir: 每个条目都要有 agentDir。缺了它, gateway 会用自己进程的
        # home 派生默认路径(Windows 上 gateway 以 SYSTEM 跑, home=systemprofile)落
        # session 状态, 而不是 home 派生的路径 → upload_paths 指向的
        # ~/.openclaw/agents 一直是空的, 且跨 query 续聊(session 复用)会报
        # "Session history or replay state is invalid"。统一按 home 派生补齐。
        # entries 模式的键即名字; list 模式条目自带 id/name。
        if entries_mode:
            # 10.6 多 agent 名册必须显式声明归属, 否则整份配置被判非法:
            # "multi-agent rosters require agents.ownership='explicit' or one legacy
            #  default=true marker"。单条目时加也无害, 故 entries 模式恒写。
            agents_section["ownership"] = "explicit"
            for name, e in agents_entries.items():
                if isinstance(e, dict) and not e.get("agentDir"):
                    # 10.6 的 agentDir 默认是 <home>/.openclaw/agents/<id>/agent
                    # (模板里 test/test1/test2 也是这个形态), 与本分支保持一致;
                    # 显式写成 agents/<id> 会让 agent 状态库落到父目录, 与
                    # openclaw 自己的解析结果不一致。
                    e["agentDir"] = str(
                        path_cls(home, ".openclaw", "agents", name, "agent"))
        else:
            for e in agents_list:
                if not isinstance(e, dict):
                    continue
                name = (e.get("id") or e.get("name") or "").strip()
                if name and not e.get("agentDir"):
                    e["agentDir"] = str(path_cls(home, ".openclaw", "agents", name))

        Path(local_path).write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

# ============================================================================
# Error catalog / classification / log formatting (shared with utils/win_util.py)
# ============================================================================

# ============================================================================
# Error Catalog
# ============================================================================
# 错误码前缀 = 错误来源, 一眼可辨:
#   C0xx  Client 侧    — hive.py 自身流程 / 本地文件 / 配置解析
#   S0xx  Server 侧    — 沙箱远端执行失败 / 网络 / OBS / gateway
#   T0xx  Task/业务侧  — 主脚本自报失败
# ============================================================================

ERROR_CATALOG: dict = {
    # -- Client-side (C0xx) --
    "C001": "extract code failed",
    "C002": "upload file failed",
    "C003": "environment creation failed",
    "C004": "agent config write failed",
    # -- Server / sandbox-side (S0xx) --
    "S001": "gateway start failed",
    "S002": "gateway startup timeout",
    "S003": "gateway unexpected output",
    "S004": "sandbox port-update failed",
    "S005": "skill download failed",
    "S006": "user profile download failed",
    "S007": "agents download failed",
    "S008": "script execution failed",
    "S009": "upload traj to OBS failed",
    # -- Task / business-side (T0xx) --
    "T001": "【Task_Failed】",
    "T002": "达到最大轮次",
    "T003": "连续3次未收到回复",
    "T004": "AgentExecutionError",
    "T005": "AssertionError",
    "T006": "API call failed",
    "T007": "TimeoutError",
    "T008": "HarnessError",
    "T009": "RuntimeError",
    "T010": "Uncategorized Traceback",
    # -- Unclassified --
    "X999": "unclassified exception",
}


def _strip_api_use_lines(text: str) -> str:
    """去掉日志里的 'INFO:api_use:' 行 (api 用量记录, 与任务成败无关, 干扰判定/查看)。"""
    if not text:
        return text
    return "\n".join(line for line in text.splitlines() if "INFO:api_use:" not in line)


def _classify_task_stdout(stdout: str) -> tuple[str, str]:
    """
    从主脚本 stdout 里推断更具体的错误码（优先匹配最后出现的错误信息）。
    """
    stdout = _strip_api_use_lines(stdout)
    if not stdout or "【Task_Done】" in stdout:
        return "", ""

    best_pos = -1
    best_code = ""
    best_phrase = ""

    # 遍历错误目录，但仅处理以 'T' 开头且不等于 'T010' 的条目
    for code, phrase in ERROR_CATALOG.items():
        if not code.startswith("T") or code == "T010":
            continue
        if phrase:
            pos = stdout.rfind(phrase)          # 从后往前查找该短语的最后出现位置
            if pos > best_pos:                  # 取位置最靠后的（即最后出现的）
                best_pos = pos
                best_code = code
                best_phrase = phrase

    if best_pos != -1:
        return best_code, best_phrase

    # 若未匹配到任何 T 类错误，再检查是否含有 Traceback
    if "Traceback" in stdout:
        return "T010", "Traceback"

    return "", ""

class HiveError(Exception):
    """
    带错误码的流水线异常。
    code:      C001 / S001 / T001 ...
    detail:    简短单行原因
    sandbox_result: 若为 sandbox 端失败, 附上原始 Result 便于详情落盘
    """

    def __init__(self, code: str, detail: str = "", sandbox_result=None):
        self.code = code
        self.short = ERROR_CATALOG.get(code, ERROR_CATALOG["X999"])
        self.detail = detail or ""
        self.sandbox_result = sandbox_result
        super().__init__(f"[{code}] {self.short}"
                         + (f": {self.detail}" if self.detail else ""))



def _fmt_sandbox_block(title: str, result, extra_stdout: str = "",
                       extra_label: str = "") -> str:
    """
    把 sandbox 返回的 Result 对象格式化成美观的多行块, 供 task-<idx>.log 展示。
    extra_stdout: 额外附带的文件内容 (如 gateway.log), 由 extra_label 标注来源。
    只格式化, 不打印。返回值末尾无换行。
    """
    def _boxed(text: str) -> str:
        bar = "─" * 78
        return f"┌{bar}┐\n{text}\n└{bar}┘"

    def _dedupe_pip_noise(lines: list[str]) -> list[str]:
        """折叠 'Requirement already satisfied:' 行, 保留其它内容原样。"""
        out: list[str] = []
        skipped: int = 0
        for ln in lines:
            #   "Requirement already satisfied: openclaw-sdk==2.1.0 in /home/ma-user/..."
            if ln.lstrip().startswith("Requirement already satisfied:"):
                skipped += 1
                continue
            out.append(ln)
        if skipped:
            out.append(f"[… {skipped} 'Requirement already satisfied' lines suppressed]")
        return out

    try:
        code = getattr(result, "code", None)
        msg  = getattr(result, "msg", "") or ""
        data = getattr(result, "data", None) or {}
        exit_code = data.get("exit_code") if isinstance(data, dict) else None
        stdout    = (data.get("stdout") if isinstance(data, dict) else "") or ""
        stderr    = (data.get("stderr") if isinstance(data, dict) else "") or ""
    except Exception as e:
        return _boxed(f" ▎{title}\n ▎<failed to format Result: {e}>")

    header = (
        f" ▎{title}\n"
        f" ▎ code={code}  exit_code={exit_code}  msg={msg!r}"
    )
    parts = [header]
    if stdout:
        parts.append(" ▎── stdout ────────────────────────────────────")
        for line in _dedupe_pip_noise(stdout.rstrip().splitlines()):
            parts.append(f" │ {line}")
    if stderr:
        parts.append(" ▎── stderr ────────────────────────────────────")
        for line in _dedupe_pip_noise(stderr.rstrip().splitlines()):
            parts.append(f" │ {line}")
    if extra_stdout:
        label = (extra_label or "file").strip()
        # 让标签行宽度与 78 列边框对齐: " ▎── <label> " + 补足横线
        prefix = f" ▎── {label} "
        pad = max(3, 86 - len(prefix))
        parts.append(prefix + "─" * pad)
        for line in extra_stdout.rstrip().splitlines():
            parts.append(f" │ {line}")
    if not stdout and not stderr and not extra_stdout:
        parts.append(" ▎ <no stdout/stderr captured>")
    return _boxed("\n".join(parts))


def _log_sandbox_detail(log: logging.Logger, title: str, result, level: int = logging.INFO,
                        extra_stdout: str = "", extra_label: str = "") -> None:
    """
    将 sandbox Result 的 stdout/stderr 详情 **只写入 task-<idx>.log**,
    main.log 通过 DropSandboxDetailFilter 过滤掉。
    extra_stdout / extra_label: 额外附带沙箱内文件内容 (如 gateway.log)。
    """
    block = _fmt_sandbox_block(title, result, extra_stdout=extra_stdout,
                               extra_label=extra_label)
    log.log(level, "sandbox_detail:\n" + block, extra={"sandbox_detail": True})
