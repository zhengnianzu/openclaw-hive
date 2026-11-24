# -*- coding: utf-8 -*-
"""从 llm_proxy_lite 的用量摘要里解析 Token 统计。

**数据来源**：开了 `proxy_enable` 的任务，沙箱里 llm_proxy_lite 会在每个任务目录下
写 `logs_proxy/proxy_summary.txt`（由 `_gen_proxy_summary` 在主脚本跑完后生成），
随轨迹整目录上传 OBS，并被 offline output_worker 下载到本地 OUTPUT_CACHE：

    <OUTPUT_CACHE>/<traj_save_path 的缓存子目录>/config_<name>/logs_proxy/proxy_summary.txt

文件内容（print_stats_summary.py 生成）形如：

    成功调用次数: 1
    失败调用次数: 0
    总输入 Tokens: 26954
    总输出 Tokens: 89
    总 Tokens: 27043

    按模型统计:
    glm-5.3: 调用 1 次, 输入 26954 , 输出 89, 总计 27043 tokens
    glm-x: 失败 1 次, 输入 100 tokens

为什么不读 `traj/index.jsonl`：那个是逐请求明细，单文件可达百 KB 且按小时分片；
`proxy_summary.txt` 是同一套聚合口径的成品，读它更省。需要逐请求粒度时再解析 index。

**口径**：只统计成功调用（失败调用的输出 token 恒为 0，但输入仍会产生费用，
所以失败次数与失败输入单独返回，便于判断损耗）。
"""
from __future__ import annotations

import glob
import json
import os
import re

_OK = re.compile(r"成功调用次数:\s*(\d+)")
_FAIL = re.compile(r"失败调用次数:\s*(\d+)")
_IN = re.compile(r"总输入 Tokens:\s*(\d+)")
_OUT = re.compile(r"总输出 Tokens:\s*(\d+)")
_PER_MODEL_OK = re.compile(
    r"^(.+?):\s*调用\s*(\d+)\s*次,\s*输入\s*(\d+)\s*,\s*输出\s*(\d+),\s*总计\s*(\d+)\s*tokens")
_PER_MODEL_FAIL = re.compile(r"^(.+?):\s*失败\s*(\d+)\s*次,\s*输入\s*(\d+)\s*tokens")


def parse_proxy_summary(text: str) -> dict:
    """解析一份 proxy_summary.txt。字段缺失时按 0 处理，不抛错。

    返回：
      {ok_calls, fail_calls, input_tokens, output_tokens, total_tokens,
       models: [{model, ok, fail, input_tokens, output_tokens, total_tokens}]}
    """
    out = {
        "ok_calls": 0, "fail_calls": 0,
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
        "models": [],
    }
    if not text:
        return out

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if (m := _OK.search(line)):
            out["ok_calls"] = int(m.group(1))
        elif (m := _FAIL.search(line)):
            out["fail_calls"] = int(m.group(1))
        elif (m := _IN.search(line)):
            out["input_tokens"] = int(m.group(1))
        elif (m := _OUT.search(line)):
            out["output_tokens"] = int(m.group(1))
        elif (m := _PER_MODEL_OK.match(line)):
            out["models"].append({
                "model": m.group(1).strip(),
                "ok": int(m.group(2)), "fail": 0,
                "input_tokens": int(m.group(3)),
                "output_tokens": int(m.group(4)),
                "total_tokens": int(m.group(5)),
            })
        elif (m := _PER_MODEL_FAIL.match(line)):
            out["models"].append({
                "model": m.group(1).strip(),
                "ok": 0, "fail": int(m.group(2)),
                "input_tokens": int(m.group(3)),
                "output_tokens": 0,
                "total_tokens": int(m.group(3)),
            })
    if not out["total_tokens"]:
        out["total_tokens"] = out["input_tokens"] + out["output_tokens"]
    return out


def _cache_dir_for_instance(instance_dir_or_params: dict) -> str | None:
    """由 create_params 算出该实例的本地轨迹缓存目录。

    与 src.offline_analysis._cache_subdir_for 同规则：OUTPUT_CACHE/<traj 缓存子目录>。
    取不到 traj_save_path（老实例/未配）时返回 None。
    """
    traj_path = (instance_dir_or_params.get("traj_save_path") or "").strip().strip("/")
    if not traj_path:
        return None
    bucket = (instance_dir_or_params.get("traj_save_bucket") or "").strip()
    bucket = bucket[len("obs://"):] if bucket.startswith("obs://") else bucket
    try:
        from src import offline_analysis as oa
        if bucket:
            obs = f"obs://{bucket}/{traj_path}/"
        else:
            obs = f"obs://{traj_path}/"
        sub = oa._cache_subdir_for(obs)
        return os.path.join(oa.DEFAULT_OUTPUT_CACHE, sub)
    except Exception:
        # 离线模块不可用（如测试环境）→ 不阻塞调用方
        return None


def collect_instance_tokens(create_params: dict | str | None) -> dict | None:
    """汇总一个实例所有任务目录的 proxy_summary.txt。

    返回 None 表示「没有可统计的数据」（未开网关 / 缓存没下载），
    调用方应把它渲染成 `—` 而不是 0 —— 0 会被误读成「真的没消耗」。
    """
    if isinstance(create_params, str):
        try:
            create_params = json.loads(create_params)
        except (ValueError, TypeError):
            return None
    if not isinstance(create_params, dict):
        return None
    if not create_params.get("proxy_enable"):
        return None

    cache_dir = _cache_dir_for_instance(create_params)
    if not cache_dir or not os.path.isdir(cache_dir):
        return None

    files = sorted(glob.glob(os.path.join(cache_dir, "*", "logs_proxy", "proxy_summary.txt")))
    if not files:
        return None

    total = {"ok": 0, "fail": 0, "input": 0, "output": 0, "total": 0}
    by_model: dict[str, dict] = {}
    per_config: list[dict] = []

    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                parsed = parse_proxy_summary(f.read())
        except OSError:
            continue
        config_name = os.path.basename(os.path.dirname(os.path.dirname(path)))
        total["ok"] += parsed["ok_calls"]
        total["fail"] += parsed["fail_calls"]
        total["input"] += parsed["input_tokens"]
        total["output"] += parsed["output_tokens"]
        total["total"] += parsed["total_tokens"]
        per_config.append({
            "config_name": config_name,
            "ok": parsed["ok_calls"], "fail": parsed["fail_calls"],
            "input_tokens": parsed["input_tokens"],
            "output_tokens": parsed["output_tokens"],
            "total_tokens": parsed["total_tokens"],
        })
        for m in parsed["models"]:
            slot = by_model.setdefault(m["model"], {
                "model": m["model"], "ok": 0, "fail": 0,
                "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            })
            slot["ok"] += m["ok"]
            slot["fail"] += m["fail"]
            slot["input_tokens"] += m["input_tokens"]
            slot["output_tokens"] += m["output_tokens"]
            slot["total_tokens"] += m["total_tokens"]

    if not total["ok"] and not total["fail"]:
        return None

    return {
        "ok_calls": total["ok"],
        "fail_calls": total["fail"],
        "input_tokens": total["input"],
        "output_tokens": total["output"],
        "total_tokens": total["total"],
        "models": sorted(by_model.values(), key=lambda x: -x["total_tokens"]),
        "configs": per_config,
    }
