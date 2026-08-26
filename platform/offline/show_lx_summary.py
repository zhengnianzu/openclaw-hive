# -*- coding: utf-8 -*-
"""离线工具 1:查看某批次/某任务的 Lx 漏斗与占比(show_lx_summary)。

用法:
  python offline/show_lx_summary.py --batch <批次名> [--bucket obs://s3-asset-b-hd-cce-aifm-nlp-exp]
        [--cache-dir <本地缓存根>] [--obsutil <path>] [--concurrency 8] [--max-tasks N] [--json]
  python offline/show_lx_summary.py --task-obs obs://.../<batch>/<task>/ [--json]

--batch 接受批次目录名或完整 obs 路径; --task-obs 接受单任务 obs 路径。
缓存默认 .env OUTPUT_CACHE(platform/output_cache) 下 <batch>/<task>/ 相对路径目录。
输出: 整批漏斗 total/L0-L3 数量 + 逐级通过率; --json 出机器可读 JSON(含 per_session)。
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


def main() -> None:
    ap = argparse.ArgumentParser(description="查看批次/任务的 Lx 漏斗与占比")
    ap.add_argument("--batch", default=None, help="批次名或批次 obs 路径")
    ap.add_argument("--task-obs", default=None, help="单任务 obs 路径")
    ap.add_argument("--bucket", default=oa.DEFAULT_OBS_BUCKET, help="OBS 桶")
    ap.add_argument("--batch-prefix", default=oa.DEFAULT_BATCH_PREFIX, help="桶内批次前缀目录")
    ap.add_argument("--cache-dir", dest="origin", default=oa.DEFAULT_OUTPUT_CACHE,
                    help=f"本地缓存根(.env OUTPUT_CACHE, 默认 {oa.DEFAULT_OUTPUT_CACHE})")
    ap.add_argument("--obsutil", default=oa.DEFAULT_OBSUTIL)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--max-tasks", type=int, default=0)
    ap.add_argument("--force-slow", action="store_true",
                    help="跳过快路径，强制逐 task 慢路径重算（hermes 陈旧批次建议用）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    a = ap.parse_args()

    def _build(url: str) -> dict:
        return oa.build_batch_summary(a.obsutil, url, a.origin,
                                      concurrency=a.concurrency, max_tasks=a.max_tasks,
                                      force_slow=a.force_slow)

    # 平台 fetch 函数把进度打到 stdout。--json 时重定向 stdout，保证纯 JSON 输出。
    import contextlib, io
    _buf = io.StringIO() if a.json else None
    _stdout_ctx = contextlib.redirect_stdout(_buf) if a.json else contextlib.nullcontext()

    if a.task_obs:
        with _stdout_ctx:
            stats = _build(a.task_obs)
        title = a.task_obs
    elif a.batch:
        batch_url = a.batch if a.batch.startswith("obs://") else \
            oa._batch_url_from_bucket(a.bucket, a.batch_prefix.rstrip("/") + "/" + a.batch.strip("/"))
        with _stdout_ctx:
            stats = _build(batch_url)
        title = a.batch
    else:
        ap.error("需要 --batch 或 --task-obs")

    if a.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        if _buf and _buf.getvalue().strip():
            sys.stderr.write(_buf.getvalue())
        return

    comp = oa.stats_from_per_task_compact(stats)
    print(f"== Lx 漏斗: {title} ==")
    print(f"  总轨迹数(L0)            : {comp['total']}")
    print(f"  合格轨迹 L1 (过门槛)     : {comp['L1']}")
    print(f"  L1.5 (有数值 completion) : {comp['L1.5']}")
    print(f"  L2 (completion>=0.5)     : {comp['L2']}")
    print(f"  L3 (completion==1)       : {comp['L3']}")
    print(f"  dropped (未过门槛)       : {comp['dropped']}")
    print(f"  task_done 标记           : {comp['task_done_count']}")
    print("  逐级通过率:")
    for k, v in comp["ratio"].items():
        print(f"    {k:10s}: {v if v is not None else 'N/A'}")
    if stats.get("token_stats"):
        t0 = stats["token_stats"].get("L1")
        if t0:
            print(f"  token 均值(L1): {t0.get('avg_total_tokens')}")
    if stats.get("char_len_stats"):
        c0 = stats["char_len_stats"].get("L1")
        if c0:
            print(f"  char_len 均值(L1): {c0.get('avg_char_len')}")
    print(f"  快路径: {'是' if stats.get('fast_path') else '否(回退慢路径)'}")


if __name__ == "__main__":
    main()
