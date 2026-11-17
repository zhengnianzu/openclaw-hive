"""Helpers for the optional per-sandbox LLM proxy integration.

Upstream URLs come from explicit platform inputs rather than config discovery:
the main harness uses ``model_base_url`` and auxiliary agents use
``user_proxy_model.json``.  This keeps routing deterministic across harnesses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROXY_HOST = "127.0.0.1"
PROXY_START_PORT = 5001
PROXY_DIR_NAME = "llm_proxy_lite"
PROXY_CONFIG_NAME = "proxy_config.json"

class ProxyConfigError(ValueError):
    """The enabled proxy cannot derive a usable upstream/listener map."""


def _is_url(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith(("http://", "https://"))


def _normalize_target(url: str) -> str:
    """Return the target prefix used by the path-preserving proxy gateway.

    Clients with a ``.../v1`` base URL send requests whose path already starts
    with ``/v1``.  Removing that suffix here avoids forwarding to ``/v1/v1/...``.
    Other path prefixes are retained (``https://host/api/v1`` -> ``.../api``).
    """
    root = url.strip().rstrip("/")
    if root.lower().endswith("/v1"):
        root = root[:-3].rstrip("/")
    return root


def _rewrite_config_urls(path: Path, replacements: dict[str, str]) -> None:
    """Replace every mapped target string with its local proxy URL."""
    content = path.read_text(encoding="utf-8")
    for target_ip, local_url in sorted(
        replacements.items(), key=lambda item: len(item[0]), reverse=True
    ):
        content = content.replace(target_ip, local_url)
    path.write_text(content, encoding="utf-8")


def build_proxy_config_and_rewrite(
    harness_config_file: str,
    model_config_file: str,
    proxy_config_file: str,
    *,
    main_base_url: str,
    start_port: int = PROXY_START_PORT,
) -> dict[str, Any]:
    """Build routing from explicit URL sources and rewrite task-local copies.

    ``main_base_url`` is the front-end form value. Auxiliary upstreams come from
    ``user_proxy_model.json``. The result is a role-independent, deduplicated
    ``target_ip -> local_ip`` map named ``ip_map1``, ``ip_map2``, ...
    """
    paths = [Path(harness_config_file), Path(model_config_file)]
    if not _is_url(main_base_url):
        raise ProxyConfigError("proxy_enable=true requires run_config.task.model_base_url")

    model_data = json.loads(paths[1].read_text(encoding="utf-8"))
    proxy_config: dict[str, dict[str, str]] = {}
    target_listeners: dict[str, str] = {}

    def listener_for(target_ip: str) -> str:
        local_ip = target_listeners.get(target_ip)
        if local_ip is None:
            local_ip = f"{PROXY_HOST}:{start_port + len(target_listeners)}"
            target_listeners[target_ip] = local_ip
            index = len(target_listeners)
            proxy_config[f"ip_map{index}"] = {
                "target_ip": target_ip,
                "local_ip": local_ip,
            }
        return local_ip

    # Main harness has one explicit role and is rewritten before auxiliary
    # providers are injected from user_proxy_model.json.
    main_url = main_base_url.strip()
    main_target = _normalize_target(main_url)
    listener_for(main_target)

    # Agent names do not participate in routing; only unique upstream mappings
    # are retained in proxy_config.json.
    for agent in model_data.values():
        if not isinstance(agent, dict) or not _is_url(agent.get("base_url")):
            continue
        original_url = str(agent["base_url"]).strip()
        target = _normalize_target(original_url)
        listener_for(target)

    replacements = {
        target_ip: f"http://{local_ip}"
        for target_ip, local_ip in target_listeners.items()
    }
    for path in paths:
        _rewrite_config_urls(path, replacements)

    encoded = json.dumps(proxy_config, indent=2, ensure_ascii=False) + "\n"
    target = Path(proxy_config_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(encoded, encoding="utf-8")
    return proxy_config

