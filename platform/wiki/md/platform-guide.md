---
title: 平台指导文档（新手操作手册）
category: 平台使用
tags:
- 新手
- 批跑
- 入门
summary: 从准备 OBS 材料、任务配置格式，到新建实例批跑、查看输出与 Harness 配置的完整上手流程。
is_pinned: true
sort_order: 15
author: admin
created_at: '2026-11-16T20:11:26'
updated_at: '2026-11-16T20:11:26'
---

# OpenClaw Hive 平台新手操作手册

> 平台地址：<http://115.120.48.151:8087/>
> 注册账号请联系管理员：00495253@郑念祖、00945507@曾湘
> 整理日期：2026-11-16

## 1. 准备工作

在上平台批跑任务之前，请提前准备以下材料，上传到蓝区（华东二）的 `obs://s3-asset-b-hd-cce-aifm-nlp-exp/` 桶下，并保存好对应路径。上传方法见参考资料 [1]。

```text
├── task_configs/
    └── task-00000.json               ← 任务配置（必须）
├── skills/                           ← input_dir.skill_dir 引用的完整技能包
    └── serper/                       ← 具体的技能
└── environments/                     ← input_dir.user_dir.path 引用
    └── task-00000/
        ├── user_profile.json         ← 用户画像
        ├── MAP_Linux.json            ← map_file（把 demo.md 映射进 agent workspace）
        └── task-00000/
            └── demo.md               ← 任务源文档
```

### 1.1 任务配置文件格式

每个案例对应一个 JSON 文件，以 `task_configs/task-00000.json` 为例：

```jsonc
{
  "harness_type": "openclaw",
  "gateway_ws_url": "ws://127.0.0.1:18789/gateway",
  "gateway_timeout": 7200,
  "workspace_base": "/home/ma-user/.openclaw",
  "input_dir": {
    "skill_dir": "skills",                  // 替换：skills 文件夹名称
    "user_dir": {
      "path": "environments/task-00000",    // 替换：用户资料等信息的文件夹
      "profile_file": "user_profile.json",
      "map_file": "MAP_Linux.json"
    }
  },
  "agents": [
    {
      "name": "main",
      "config": [],
      "skills": [
        "serper"                            // 替换：任务需要的 skill 技能名
      ]
    }
  ],
  "queries": [                              // query 列表，支持多 query
    {
      "agent_name": "main",
      "session_name": "task-00000",
      "text": "用校服条目做PPT，每页只讲一个点，给高中生讲概念。",
      "use_simulator": true,                // 是否使用用户模拟模型多轮交互，false 则单轮对话
      "timeout": 7200
    }
  ],
  "user_max_turn": 20,
  "simulator_config": "configs/user_proxy_model.json"
}
```

### 1.2 MAP_Linux.json 文件格式

```jsonc
{
  "demo.md": "~/demo.md"    // ~ 会自动解析到对应 harness 的 agent workspace 下
}
```

### 1.3 模型Key准备
（如需）去 AI数据技术创新平台 - 外部API管理 - 数据生产申请里面申请

## 2. 开始批跑

### 2.1 新建任务

- 步骤 1：新建 OBS 配置

![alt text](/api/wiki/assets/dce3c15534514f009410ac1a8c63a39c.webp)

- 步骤 2：Harness 配置

![alt text](/api/wiki/assets/91d32be8da5045fcb5bfb054f3b39f2b.webp)

- 步骤 3：用户模拟配置

![alt text](/api/wiki/assets/3609d1522d6342c5bd75081654f68732.webp)

- 步骤 4：高级配置

![alt text](/api/wiki/assets/36440718d5094bf7887a6872e892dc94.webp)

最后点击 **创建实例**，跳转到主界面，再点击该实例的【更多】→【启动】。

### 2.2 查看任务

#### 2.2.1 任务详情页

![alt text](/api/wiki/assets/f35c9d9572b34050977a94123a09442a.webp)

#### 2.2.2 日志页

![alt text](/api/wiki/assets/c9147faa465a409bae77d0111bc6673e.webp)

#### 2.2.3 输出页

指导文档：<http://115.120.48.151:8087/#/wiki/outputview-usage>

![alt text](/api/wiki/assets/e82e5f9ec73e46cbaacb7cfd56518c0e.webp)

**会话等级**

| 等级 | 含义 |
|---|---|
| L0 | 存在轨迹 |
| L1 | assistant 轨迹的工具调用次数 ≥ 3，且出现过至少一次纯文字回复 |
| L1.5 | 在 L1 基础上，有 evaluator 裁决 |
| L2 | 在 L1.5 基础上，首轮 completion ≥ 0.5 |
| L3 | 在 L1.5 基础上，首轮 completion = 1 |

**模型配置 / 用户模拟配置**

可以直接查看、导出对应轨迹，用于后处理训练。不同 harness 均有对应的导出方式。

**会话列表**

可以查看该实例每个任务的完整数据流链路：

```text
任务名称 → 任务编号 → 任务配置（task.json）→ 等级评估 → 详情（日志）→ 交付物（沙箱保存内容）
```

#### 2.2.4 沙箱交付物

![alt text](/api/wiki/assets/c930f0a5c5584f16a02b220a02f65843.webp)

## 3. 配置 Harness

### 3.1 三层关系

```text
Harness 类型
  └─ 配置方案（可改名）
       └─ 版本（不可变快照）
```

**工作台是可编辑副本，保存之后，点击发布，【已发布】的版本，才可以被选中。**

常见 Runtime 文件包括：

- `config.yaml`
- `openclaw.json`
- `user_proxy_model.json`

可选的 Harness Markdown 包括：

- `AGENTS.md`
- `SOUL.md`
- `IDENTITY.md`
- 其他该类型模板提供的 Markdown 文件

不需要的文件可以保持“未初始化”状态。

### 3.2 修改已有方案

方式一（复制后修改）：

```text
选择 Harness 类型 → 选择方案 → 复制方案（重命名）→ 进入工作台 → 修改文件 → 保存 → 创建新版本 → 发布
```

方式二（直接修改）：

```text
选择 Harness 类型 → 选择方案 → 进入工作台 → 修改文件 → 保存 → 创建新版本 → 发布
```

## 4. 队列管理

> 指导文档：<http://115.120.48.151:8087/#/wiki/queue-usage>

### 4.1 新建队列
在高级配置中，可以选择 **任务队列**，用来让多个任务按顺序串行执行：一个跑完，下一个自动开始，不用自己盯着。

![alt text](/api/wiki/assets/7268ad0971944b6db79dd72d9c61d641.webp)

### 4.2 插队
![alt text](/api/wiki/assets/357f0486084b4af2b35ef1a6181c4312.webp)
队列里如果有任务想插队到最优先的放行位置，可以点击插队，操作入口如下：

| 位置 | 操作 |
|---|---|
| **任务管理**（首页） | 该任务的「更多」→ **插队**；已插队的显示 **取消插队** |
| **任务详情 → 队列信息** | 顶部「插队」/「取消插队」按钮，下方队列任务列表每行也有 |

- 取消插队后，任务回到它原本的名次（插队只改优先级，从来没动过入队序号）。
- 任务被重新「启动」时，插队标记会被清空，按正常流程排到队尾。

## 5. Harness 配置管理



## 6. 镜像管理
![alt text](/api/wiki/assets/a5e309761f224a7aa80281cf358cc0c1.webp)

| harness类型 | 镜像 | 描述 | 
|---|---|---|
| openclaw2026.6.6、hermes0.18.2、claude-code2.1.22、opencode1.18.18、codex0.147.0、pi0.84.3、grok1.0.5、dsh0.1.1-rc.2 | multiharness:26.11.4 （**multiharness:后跟日期的，基础镜像版本**）| 默认包含serper技能 |
| multiharness-no-serper:26.xx.x | 移除所有harness的默认serper技能，skill泛化专用 | linux | 
| openclaw26.10.6版本 | openclaw:26.10.6-20261102 | linux | 
| jiuwenswarm:dev-stable-2026.10.23v1 | jiuwenswarm-dev专属镜像，默认带serper | 
| jiuwenswarm:xiaoyi-2026.11.2 | jiuwenswarm-xiaoyi专属镜像，默认带serper | 
| lego-preset-v2 | lego专属镜像，万轶维护 | linux |
| xiaoyiclaw:0.0.x | xiaoyiclaw 专属镜像， 曹子轩维护 | linux | 
| windows-xxx | windows镜像 | windows | 


## 7. 代码管理
![alt text](/api/wiki/assets/5842ae4be0da4df5b94236ac4a76db43.webp)
| 代码仓 | 描述 |
|---|---|
| openclaw-task/v1117 | **主代码仓版本**：选择最新日期的 | 
| openclaw-task/test-jiuwenswarm-serper | test代码仓，测试用 | 
| openclaw-task/xiaoyiclaw-pangu-v4 | xiaoyiclaw专属代码仓，曹子轩维护 | 
| openclaw-task/trg0249_261112_fix2 | trg专属代码仓，万轶维护 | 

## 参考资料

### [1] obsutil 连接配置脚本（以 Linux 为例）

```bash
# 下载
cd /home/ma-user/ && wget https://obs-community.obs.cn-north-1.myhuaweicloud.com/obsutil/current/obsutil_linux_amd64.tar.gz
# 解压
tar -xzvf obsutil_linux_amd64.tar.gz
# 移动
mv obsutil_linux_amd64_5.8.3 obsutil
# 配置
./obsutil/obsutil config -i=HPUABVUGOTP2OPODPEKP -k=1lNcDkVMBV7tVQq3aBWXirTWClbpiWJ1r08zbkSD -e=obs.cn-southwest-2.myhuaweicloud.com
# 验证
./obsutil/obsutil ls -d obs://s3-asset-b-hd-cce-aifm-nlp-exp/
# 双向复制
./obsutil/obsutil cp -r -f <local_dir> <obs_dir>
```
