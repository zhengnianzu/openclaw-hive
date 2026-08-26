# -*- coding: utf-8 -*-
"""离线工具 4:任务状态表 + 数据接口/缓存核查。

用法:
  python offline/task_status.py --task-obs obs://.../<batch>/<task>/ [--filter-level L1] [--sort completion] [--json]
  python offline/task_status.py --task-obs ... --verify-cache [--api-base http://127.0.0.1:8000] [--json]

--verify-cache: 逐文件比对 OBS 远端大小与本地缓存(默认 .env OUTPUT_CACHE 下 <batch>/<task>/),再(若给 --api-base)直查数据接口,
  比对接口返回与缓存文件是否一致。输出核查报告 [{file, remote_exists, remote_size, local_exists, local_size, match, note}],
  note 取值: OK / MISSING_REMOTE / MISSING_LOCAL / MISMATCH / API_MISMATCH / API_NOTE_FOUND。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from src import offline_analysis as oa  # noqa: E402


def verify_api_and_cache(task_dir: str, task_obs: str, obsutil: str,
                         api_base: str | None = None,
                         obs_cred_args: dict | None = None) -> dict:
    """核查缓存文件与 OBS 远端一致性,并(可选)直查数据接口比对。"""
    report = oa.verify_local_cache(task_dir, task_obs, obsutil, obs_cred_args=obs_cred_args)
    result = {"task_obs": task_obs, "task_dir": task_dir, "files": report}
    if not api_base:
        return result
    api_base = api_base.rstrip("/")
    # 数据接口: 批次任务列表; 若本地能解析出任务名则只查该任务
    task_name = task_obs.rstrip("/").split("/")[-1]
    url = f"{api_base}/api/tasks?prefix=openclaw_trajs/{task_name}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            api = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        result["api_error"] = str(e)
        return result
    # 找该任务在接口里返回的缓存文件字段(若存在)
    api_hits = 0
    local_files = {os.path.basename(os.path.normpath(os.path.join(task_dir, f["file"])))
                   for f in report if f["local_exists"]}
    for row in api.get("tasks", []):
        if not row.get("name") or row["name"] != task_name:
            continue
        for cand in ("files", "cache_files", "traj_files"):
            for f in row.get(cand) or []:
                api_hits += 1
                if os.path.basename(str(f)) in local_files:
                    for r in report:
                        if os.path.basename(os.path.normpath(os.path.join(task_dir, r["file"]))) == os.path.basename(str(f)):
                            r.setdefault("api_endpoint", url)
                            r["api_matches_cache"] = r.get("api_matches_cache", True)
                            break
    for r in report:
        if r.get("api_matches_cache") is None:
            r["api_matches_cache"] = None
    result["api_endpoint"] = url
    result["api_matches_cache"] = (api_hits > 0)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="任务状态表 + 数据接口/缓存核查")
    ap.add_argument("--task-obs", default=None)
    ap.add_argument("--local", default=None, help="本地已缓存任务目录(跳过下载)")
    ap.add_argument("--cache-dir", dest="origin", default=oa.DEFAULT_OUTPUT_CACHE,
                    help=f"本地缓存根(.env OUTPUT_CACHE, 默认 {oa.DEFAULT_OUTPUT_CACHE})")
    ap.add_argument("--obsutil", default=oa.DEFAULT_OBSUTIL)
    ap.add_argument("--filter-level", default=None, choices=["L0", "L1", "L1.5", "L2", "L3", "dropped"],
                    help="只显示指定级别")
    ap.add_argument("--sort", default="completion", choices=["task", "level", "completion"])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verify-cache", action="store_true", help="核查本地缓存 vs OBS 远端")
    ap.add_argument("--api-base", default=None, help="数据接口 base URL(给了才直查接口比对)")
    a = ap.parse_args()

    if a.local:
        task_dir = os.path.abspath(a.local)
    elif a.task_obs:
        task_dir = os.path.join(a.origin, oa._cache_subdir_for(a.task_obs))
        oa.download_task_detail(a.obsutil, a.task_obs, a.origin)
    else:
        ap.error("需要 --task-obs 或 --local")

    if a.verify_cache:
        res = verify_api_and_cache(task_dir, a.task_obs or task_dir, a.obsutil, api_base=a.api_base)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"== 缓存核查: {res['task_obs']} ==")
            for r in res["files"]:
                print(f"  {r['file']:60s} remote={'Y' if r['remote_exists'] else 'N'} "
                      f"({r['remote_size']}) local={'Y' if r['local_exists'] else 'N'} "
                      f"({r['local_size']}) match={r['match']} note={r.get('note')}")
            if res.get("api_endpoint"):
                print(f"  接口: {res['api_endpoint']}  api_matches_cache={res.get('api_matches_cache')}")
            if res.get("api_error"):
                print(f"  接口错误: {res['api_error']}")
        return

    detail = oa.load_task_detail(task_dir)
    rows = oa.build_task_status_rows(detail, filter_level=a.filter_level, sort_by=a.sort)
    if a.json:
        print(json.dumps({"task": detail["task"], "harness": detail["harness"],
                          "verdict": detail["verdict"], "rows": rows}, ensure_ascii=False, indent=2))
    else:
        print(f"== 任务状态: {detail['task']} (harness={detail['harness']}) ==")
        print(f"  has_eval={detail['verdict']['has_eval']} completion={detail['verdict']['completion']} "
              f"verdict_source={detail['verdict']['verdict_source']} task_done={detail['verdict']['task_done']}")
        for r in rows:
            comp = r["completion"]
            comp_s = "-" if comp is None else f"{float(comp):.3f}"
            print(f"  {r['level']:6s} tool_calls={r['tool_calls']:3d} plain={r['plain_rounds']:3d} "
                  f"completion={comp_s} {r['name']}")


if __name__ == "__main__":
    main()
