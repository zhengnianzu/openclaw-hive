---
title: jiuwenswarm-xiaoyi批跑和轨迹聚合说明
summary: jiuwenswarm-xiaoyi批跑和轨迹聚合说明
is_pinned: false
sort_order: 140
author: c00845018
created_at: '2026-11-12T16:14:25.611245'
updated_at: '2026-11-13T17:38:27.645569'
tags: []
---

### 批跑要求

jiuwenswarm-xiaoyi批跑任务

Windows 配置可以参考 <http://115.120.48.151:8087/#/instance/2611101950-c008450182611101949-9405>

Linux配置可以参考 <http://115.120.48.151:8087/#/instance/2611012200-c008450182611012200-a798>

<br />

批跑需要注意三点

<br />

(1) **所有的key都需要经过new api中转**，中转地址包括 115.120.113.66 115.120.62.121  115.120.48.151 115.33.106.57 115.120.50.8 ，否则轨迹无法被捕获。

(2) 每次批跑新任务的时候，**需要在平台上生成一个新key**，方便后续聚合的时候进行任务对应

(3) 轨迹和workspace的对应**依赖于user id**，因此应当将 Key追加沙箱后缀开启(外部模型开启此参数不会影响模型的正常调用)

<br />

![1.00](/api/wiki/assets/217448c9e0964c41ad7b42b63f7a91f6.webp)

## 轨迹聚合

<br />

![1.00](/api/wiki/assets/df0ecc1ff19548cf93f44bdf53ba02ea.webp)

轨迹聚合需要输入两个参数，一个是 轨迹的路径，采用上图的方式导出，一个是workspace的路径，来自批跑任务的配置。

<br />

聚合代码在: <https://codehub-g.huawei.com/l00981388/jiuwenswarm_traj_agg/files?ref=master>

<br />

补充: 

已经下载到本地的轨迹数据和workspace数据可以通过指定本地cache路径避免重复下载

对于已导出的数据，可以参考 [jiuwenswarm_traj_agg - 文件](https://codehub-g.huawei.com/l00981388/jiuwenswarm_traj_agg/files?ref=master\&filePath=delivery_filter%2Fdelivery_filter.py\&isFile=true)来过滤其中的压缩轨迹和evaluator轨迹，这种方式会将压缩轨迹误删除。
