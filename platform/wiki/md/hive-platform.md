---
title: Hive Platform新手使用记录
category: 平台使用
summary: 新手从零使用记录
is_pinned: false
sort_order: 130
author: '60043045'
updated_by: admin
created_at: '2026-11-16T16:11:51.785969'
updated_at: '2026-11-23T19:56:47.856408'
tags: []
---

1. 创建任务 - 基于现有任务复制

   1. 基于id搜索找不到任务，基于名字搜索可以找到
   2. 点击列表右侧的 ⌈详情⌋ -> 右上角 ⌈操作⌋ -> ⌈复制⌋ 进入配置页面
   3. 调整obs配置，测试任务只修改了 ⌈轨迹保存路径⌋，实际任务按需修改
   4. 调整Harness配置，⌈新建KEY⌋ 基于邀请码 ⌈Invite Code⌋ ，用户模拟配置user_simulator、evaluator也可以配置api key
   5. 创建实例
2. 运行 - 观察运行状态
3. grok和dsh的glm-5.3模型，需要配置openai格式（针对http\://115.33.106.57:8082\
   default_t60043045_9757_A0JT_FZJk）
