# -*- coding: utf-8 -*-
"""轻量 traj_stats：从源仓 /root/traj_output/traj_stats.py 迁来的最小函数集。

当前 output_pipeline 只用到 has_task_done_marker（扫描主 log 的「【Task_Done】」标记）。
后续如需轨迹正文解析（assistant jsonl → 消息流）、verdict 抽取（extract_first_evaluator_verdict）
等详情能力，再按需从源仓补迁，保持本模块只含当前 pipeline 实际依赖的函数。
"""
from __future__ import annotations

import os


def has_task_done_marker(log_path):
    """检查 task 主 log 是否包含「【Task_Done】」标记(assistant 回答末尾输出的任务完成信号)。
    log 不存在时视为 False。"""
    if not os.path.isfile(log_path):
        return False
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            return "【Task_Done】" in f.read()
    except OSError:
        return False
