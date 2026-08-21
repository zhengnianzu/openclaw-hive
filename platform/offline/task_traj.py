# -*- coding: utf-8 -*-
"""离线工具 2/3:列任务轨迹 + 按块打印 assistant 轨迹(逐块 JSON)。

用法:
  python offline/task_traj.py --task-obs obs://.../<batch>/<task>/    # 列轨迹
  python offline/task_traj.py --task-obs obs://.../<batch>/<task>/ --print-trajectory [--path <相对路径|'primary'>] [--json]
  python offline/task_traj.py --local <本地任务目录> [--print-trajectory ...]

缓存落 .env OUTPUT_CACHE（默认 platform/output_cache）下的 <batch>/<task>/ 相对路径目录。
--print-trajectory 默认打印最可能的主 assistant 轨迹(max by size)。
--json: 整份输出为 JSON,块形如 {i, role, part_type, content, tool_name, args, isError, exitCode, ...}。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from src import offline_analysis as oa  # noqa: E402


def _pick_trajectory(task_dir: str, want: str | None):
    """解析 --path: None→primary; 名字匹配 task_dir 下的相对路径(可直接给文件名)。"""
    if not want or want == "primary":
        return oa.find_primary_assistant_trajectory(task_dir)
    want = want.replace("\\", "/")
    hits = [p for p in oa.list_task_trajectories(task_dir) if want in p["path"] or p["path"].endswith(want)]
    if not hits:
        raise SystemExit(f"找不到轨迹 {want!r}; 可用: {[p['path'] for p in oa.list_task_trajectories(task_dir)]}")
    if len(hits) > 1:
        hits.sort(key=lambda p: p["size"], reverse=True)
    return hits[0]["path"]


def main() -> None:
    ap = argparse.ArgumentParser(description="列出/打印任务轨迹")
    ap.add_argument("--task-obs", default=None, help="任务 obs 路径")
    ap.add_argument("--local", default=None, help="本地已缓存任务目录(跳过下载)")
    ap.add_argument("--cache-dir", dest="origin", default=oa.DEFAULT_OUTPUT_CACHE,
                    help=f"本地缓存根(.env OUTPUT_CACHE, 默认 {oa.DEFAULT_OUTPUT_CACHE})")
    ap.add_argument("--obsutil", default=oa.DEFAULT_OBSUTIL)
    ap.add_argument("--print-trajectory", action="store_true", help="按块打印主 assistant 轨迹")
    ap.add_argument("--path", default=None, help="指定轨迹相对路径或关键字; 默认 primary")
    ap.add_argument("--json", action="store_true", help="整份输出为 JSON")
    ap.add_argument("--max-lines", type=int, default=oa._MAX_TRAJ_LINES)
    ap.add_argument("--max-bytes", type=int, default=oa._MAX_TRAJ_BYTES)
    a = ap.parse_args()

    if a.local:
        task_dir = os.path.abspath(a.local)
    elif a.task_obs:
        task_dir = os.path.join(a.origin, oa._cache_subdir_for(a.task_obs))
        oa.download_task_detail(a.obsutil, a.task_obs, a.origin)
    else:
        ap.error("需要 --task-obs 或 --local")

    trajs = oa.list_task_trajectories(task_dir)

    if not a.print_trajectory:
        out = [{"path": p["path"], "role": p["role"], "kind": p["kind"],
                "size": p["size"], "mtime": p["mtime"], "note": p.get("note")} for p in trajs]
        print(json.dumps(out, ensure_ascii=False, indent=2) if a.json
              else "".join(f"{p['path']}  role={p['role']} kind={p['kind']} size={p['size']}\n" for p in trajs))
        return

    path = _pick_trajectory(task_dir, a.path)
    # harness 由文件格式判定: *.json = hermes messages[]（OpenAI chat 式）; *.jsonl = openclaw event stream
    harness = "hermes" if path.endswith(".json") else "openclaw"
    if harness == "hermes":
        blocks = oa.parse_hermes_messages(path)
    else:
        blocks = oa.parse_openclaw_trajectory(path, max_lines=a.max_lines, max_bytes=a.max_bytes)

    if a.json:
        out = [{"i": i, **{k: b.get(k) for k in ("role", "part_type", "content", "tool_name",
                                                  "args", "isError", "exitCode")}}
               for i, b in enumerate(blocks)]
        print(json.dumps({"trajectory": path, "harness": harness, "count": len(blocks), "blocks": out},
                         ensure_ascii=False, indent=2))
        return

    print(f"== {path}  (harness={harness}) ==")
    for i, b in enumerate(blocks):
        head = f"[{i}] {b.get('role')}/{b.get('part_type')}"
        if b.get("tool_name"):
            head += f" {b['tool_name']}"
        if b.get("isError"):
            head += " [ERROR]"
        if b.get("exitCode") is not None:
            head += f" exit={b['exitCode']}"
        print(head)
        content = b.get("content")
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False)
        content = content or ""
        for line in content.splitlines()[:30]:
            print("    " + line)
        if b.get("args") is not None:
            print("    args: " + json.dumps(b["args"], ensure_ascii=False)[:300])
        print()


if __name__ == "__main__":
    main()
