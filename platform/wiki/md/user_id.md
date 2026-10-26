---
title: user_id配置说明
category: 平台使用
tags:
- 通知
- wiki
summary: 自部署模型（网关按照user_id计费）需要，上传前给模型key追加_<沙箱标识>，第三方模型请关闭，否则鉴权失败
linked_entity_type: harness_config
linked_entity_key: ppt_256k
is_pinned: true
sort_order: 30
author: admin
created_at: '2026-10-20T14:48:52.535204'
updated_at: '2026-10-20T14:48:52.535204'
---

1. 请新建任务/复制任务时，检查【高级配置】中“key追加沙箱后缀”开启，**使用自部署模型时，必须强制开启**，使用第三方模型则请关闭；
2. harness配置的suffix_api_keys字段值由【高级配置】控制；
3. 任务生成后，请检查任务名称是否带【红色*】号，带上则表示user_id配置成功，没带上则请检查任务详情页的高级配置选项-Key沙箱后缀是否是【已开启】状态
