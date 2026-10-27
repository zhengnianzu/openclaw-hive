---
title: 新增openclaw-tui&&jiuwenswarm生产管线
category: 平台使用
tags:
- wiki
- 通知
- Harness
summary: 新增openclaw-tui&&jiuwenswarm生产管线说明
linked_entity_type: code_repo
linked_entity_key: '137'
is_pinned: false
sort_order: 70
author: admin
created_at: '2026-10-23T18:04:28.982764'
updated_at: '2026-10-24T16:19:16.608473'
---

1. 发布最新版代码仓：V1024版本：新增了openclaw-tui（2026.6.6版本）和jiuwenswarm（dev-stable版本）
2. openclaw-tui可复用现在的multiharness:26.9.24v1 镜像；jiuwenswarm需要指定镜像如【jiuwenswarm:dev-stable-2026.10.23v1】
3. 【multiharness:26.10.24】修复了oc的超长不压缩，强制截断问题，修改源码调整数值
