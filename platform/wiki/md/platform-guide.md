---
title: 平台指导文档（新手操作手册）
category: 平台使用
tags:
- 新手
- 批跑
- 入门
summary: 从准备 OBS 材料、任务配置格式，到新建实例批跑、查看输出与 Harness 配置的完整上手流程。
is_pinned: true
sort_order: 10
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
        ├── simple_eval_rubrics.json  ← rubric文件（见章节#7）
        ├── MAP_Linux.json            ← map_file（把 demo.md 映射进 agent workspace）
        └── task-00000/
            └── demo.md               ← 任务源文档
```

### 1.1 任务配置文件格式

每个案例对应一个 JSON 文件，以 `task_configs/task-00000.json` 为例：

```jsonc
{
  "system": {
    "platform": ["linux"],
    "python": "3.12",
    "tools": []
  },
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
  "queries": [                              // 【核心】query 列表，支持多 query 串行执行
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

### 1.3 生产Key平台申请
> 完整说明：<http://115.120.48.151:8087/#/wiki/Application_Process_for_the_Production_Key_Platform.md>

1. AI数据技术创新平台 - 外部API管理 - 数据生产申请里面申请。
2. 如果自备模型url和key，请先转发到蓝区网络，本平台生产环境为蓝区。

## 2. 开始批跑

### 2.1 新建任务

- 步骤 1：新建 OBS 配置

![alt text](/api/wiki/assets/dce3c15534514f009410ac1a8c63a39c.webp)

- 步骤 2：Harness 配置

![alt text](/api/wiki/assets/3711345fff3b485a945ac88d7c147a1d.webp)

- 步骤 3：用户模拟配置

![alt text](/api/wiki/assets/3609d1522d6342c5bd75081654f68732.webp)

- 步骤 4：高级配置

![alt text](/api/wiki/assets/8170177a288d42748ecae27ca9b28233.webp)
1. 使用自部署模型时，必须强制开启【Key追加沙箱后缀】，使用第三方模型则请关闭；
2. 任务生成后，请检查任务名称是否带【红色*】号，带上则表示user_id配置成功
3. 最后点击 **创建实例**，跳转到主界面，再点击该实例的【更多】→【启动】

### 2.2 查看任务

#### 2.2.1 任务详情页

![alt text](/api/wiki/assets/f35c9d9572b34050977a94123a09442a.webp)

#### 2.2.2 日志页

![alt text](/api/wiki/assets/c9147faa465a409bae77d0111bc6673e.webp)

#### 2.2.3 输出页

> 完整说明：<http://115.120.48.151:8087/#/wiki/outputview-usage>

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

可以直接查看、导出对应的代理轨迹，用于后处理训练。不同 harness 均有对应的导出方式。

**会话列表**

可以查看该实例每个任务的完整数据流链路：

```text
任务名称 → 任务编号 → 任务配置（task.json）→ 等级评估 → 详情（日志）→ 交付物（沙箱保存内容）
```

#### 2.2.4 沙箱交付物

![alt text](/api/wiki/assets/c930f0a5c5584f16a02b220a02f65843.webp)
可以选择性下载对应的沙箱报错的session轨迹，用于后续导出轨迹；

#### 2.2.5 任务导出
![alt text](/api/wiki/assets/3c223c3dd47d4eca97da27cb25cc29db.webp)

## 3. Harness 配置管理

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
- `Claude.md`
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

### 3.3 现有配置说明
配置的三个文件：
1. config.yaml，任务实例的配置文件
```yaml
# ==================== 全局配置固定模板 ====================
略
# ==================== 沙箱环境配置 ====================
# 定义不同架构/加速器类型的沙箱配置，每种配置对应一种运行环境:x86 CPU\ARM CPU\NPU\windows
sandbox:
  x86_cpu:
    sandbox:
      image: swr.cn-east-4.myhuaweicloud.com/rl_team/multiharness:26.8.3  # 镜像选择
      # 【重要关注】环境变量配置，根据需求修改
      env:
        - name: PIP_INDEX_URL
          value: "https://mirrors.huaweicloud.com/repository/pypi/simple"
        - name: PIP_TRUSTED_HOST
          value: "mirrors.huaweicloud.com"
        - name: NODE_TLS_REJECT_UNAUTHORIZED
          value: "0"
        # 1. serper 默认搜索工具配置，（请使用自己团队的key）
        - name: SERPER_API_URL
          value: https://yibuapi.com/serper/
        - name: SERPER_API_KEY
          value: sk-key

        # 2. image-reading工具配置（请使用自己团队的key）
        - name: IMAGE_READING   # 是否启用image-reading工具，默认off
          value: 'off'
        - name: IMAGE_READING_PATH   # image-reading工具位置，linux沙箱专用
          value: /home/ma-user/claude_code_image_reading_tool/mcp-image-reading/server.js
        - name: VL_API_URL      
          value: http://115.33.106.57:8082/v1
        - name: VL_API_KEY
          value: sk-vl-key
        - name: VL_MODEL_ID
          value: gemini-3.7-flash

        # 3. harness 专用
        - name: MCP_PLAYWRIGHT  # 是否启用PLAYWRIGHT工具。默认off
          value: 'off'
        
      # 资源请求与限制
      resources:
        cpu_request: "1"          # CPU 请求：500m（代表0.5核）
        cpu_limit: "2"            # CPU 上限
        memory_request: "4Gi"     # 内存请求量
        memory_limit: "6Gi"       # 内存上限
sandbox_namespace: omni-env-default-worker
s3:
  s3_uploader_script: /home/ma-user/obsutil/obsutil
  s3_download_script: /home/ma-user/obsutil/obsutil
  bucket_name: obs://s3-asset-b-hd-cce-aifm-nlp-exp
```

## 4. 队列管理

> 完整说明：<http://115.120.48.151:8087/#/wiki/queue-usage>

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


## 5. 镜像管理
![alt text](/api/wiki/assets/7e8841936bf7459dab13b60ce3a9b00a.webp)
- **一个镜像可以映射多个 Harness**：注册/编辑镜像时可多选 Harness 类型
- **默认镜像（每个 Harness 唯一）**：注册或编辑时打开「设为默认镜像」开关，它就成为其映射的每个 Harness 的默认镜像；镜像列表的 Harness 映射列中，默认项以 + ★ 标识。
- **启用开关**：停用的镜像不再出现在创建页下拉，不影响已运行实例。
- **与创建页联动**：高级配置中选好 Harness 类型后，「镜像名称」会自动填入该 Harness 的默认镜像，并带【推荐】标记；仍可手动改选其他。镜像下拉还会按所选沙箱配置的操作系统（Linux/Windows）过滤。


## 6. 代码管理
![alt text](/api/wiki/assets/b8905809b62f48e0bb7ce3a9ac35ab1c.webp)

### 6.1 当前常用代码仓

| 代码仓 | 描述 |
|---|---|
| openclaw-task/v1117 | **主代码仓版本**（建议设为默认） |
| openclaw-task/test-jiuwenswarm | test 代码仓，测试用 |
| openclaw-task/xiaoyiclaw-pangu-v4 | xiaoyiclaw 专属代码仓，曹子轩维护 |
| openclaw-task/trg0249_261112_fix2 | trg 专属代码仓，万轶维护 |

## 7. 批量实验
> 完整说明：<http://115.120.48.151:8087/#/wiki/batch-usage>

```text
Harness 列表 × 模型列表 × 重复次数 → 子任务数；每个子任务内部跑完整测试集
```

### 7.1 快速上手

1. **准备数据集**：在「数据集管理」登记测试集。用户 Config 目录必填；
2. **新建实验**：「批量实验 → 新建批量实验」，依次配置五个 Tab：
   ![alt text](/api/wiki/assets/df83f47e647e42e898754c87db0e42a7.webp)
3. **启动**：创建只写配置、秒级返回；进入实验详情，「更多 → 启动队列」后开始执行。
4. **结果对比**：执行完成之后，可以横向多方面对比批量实验的执行结果，后续可以导出画图，形成最佳推荐


## 8. Evaluator配置
### 8.1 理解三个角色

| 角色 | 做什么 | 是否决定任务结束 |
|---|---|---|
| 执行 Agent | 真正执行 Query、调用工具、生成文件或回答问题 | 否 |
| User Simulator | 模拟用户追问，并输出 `【Task_Done】` 或 `【Task_Failed】` | 是 |
| Evaluator | 读取原始任务、最近几轮回复、工具调用和磁盘文件，根据 Rubric 逐项检查，再把建议选择性反馈给 User Simulator | 否，只是顾问 |


### 8.2 启用Evaluator
在task-00000.json中添加如下内容：
```json
{
"agents": [
    {
      "name": "main",
      "config": ["SOUL.md", "AGENT.md", "IDENTITY.md", "USER.md"],
      "skills": [],
      "system_prompt": "You are a helpful AI assistant. Answer questions clearly and concisely in Chinese."
    },
    {
      "name": "evaluator",   // 顶层 agents 声明独立的 evaluator Agent
      "config": [],
      "skills": [],
      "system_prompt": null
    }
  ],
  "queries": [
    {
      "agent_name": "main",
      "text": "请解释什么是快速排序算法，给出时间复杂度分析，并用Python写一个实现。",
      "session_name": "main",
      "timeout": 3600,
      "use_simulator": true,        // 是否开启多轮对话
      "evaluate": {                 // 包含 evaluate 块，并引用该 evaluator Agent；
        "agent_name": "evaluator",  
        "session_name": "eval",
        "eval_step": 1,             // 每 N 轮评估一次，同时只投喂最近 N 轮窗口
        "to_simulator": true,       // use_simulator是否利用evaluator的结果
        "rubrics_ref": "simple_eval_rubrics.json#/rubrics", // rubrics门禁引用
        "scoring_ref": "simple_eval_rubrics.json#/scoring",  // 权重分数引用
        "oracle_ref": null          // Ground Truth JSON 文件路径
      }
    }
  ]
}

``` 
### 8.3 定义Rubric
#### 简单任务：内联 `rubrics`

直接写字符串列表即可。系统会自动生成 R1、R2 等 ID，所有项按普通最终评分项等权处理。

```json
"rubrics": [
  "回答包含明确结论",
  "关键数字有来源或计算过程",
  "最终文件已经真实写入工作区"
]
```

#### 正式评测：`simple_eval_rubrics.json`

```json
{
  "rubrics": [
    {
      "id": "G1",
      "when": "gate",
      "evaluator": "llm_judge",
      "text": "回答中包含快速排序的核心思想（分治、选取pivot、分区）"
    },
    {
      "id": "G2",
      "when": "gate",
      "evaluator": "llm_judge",
      "text": "没有将快速排序与其他排序算法混淆（如归并排序、堆排序）"
    },
    {
      "id": "C1",
      "when": "final",
      "evaluator": "llm_judge",
      "text": "正确给出平均时间复杂度 O(n log n) 和最坏时间复杂度 O(n²)"
    },
    {
      "id": "C2",
      "when": "final",
      "evaluator": "llm_judge",
      "text": "给出了可运行的 Python 代码实现"
    },
    {
      "id": "C3",
      "when": "final",
      "evaluator": "llm_judge",
      "text": "代码逻辑正确（包含 partition 步骤和递归调用）"
    },
    {
      "id": "PT1",
      "when": "per_turn",
      "evaluator": "llm_judge",
      "text": "回答使用中文，表述清晰易懂"
    }
  ],
  "scoring": {
    "gate_zero": true,
    "weights": {
      "correctness": 0.6,
      "presentation": 0.2,
      "process": 0.2
    },
    "bucket_map": {
      "correctness": [
        "C1",
        "C2",
        "C3"
      ],
      "presentation": [
        "PT1"
      ],
      "process": []
    }
  }
}
```
#### 8.4 评估结果
![alt text](/api/wiki/assets/6717c5af184e48f6b00669eee5fce0b8.webp)
如果在高级配置中，开启了本地代理，则可以捕获更多模型调用详情如`输入TOKENS`、`输出TOKENS`、`次数`、`成功率`

## 9. 资源看板
> 完整说明：<http://115.120.48.151:8087/#/wiki/dashboard-usage>

平台有两个查看资源的入口，二者统计口径一致：

| 入口 | 适合什么时候看 | 能看到什么 |
|---|---|---|
| 首页顶部「当前资源使用情况」 | 创建、启动任务前快速确认 | 当前 CPU、内存、沙箱并发和任务状态 |
| 侧栏「资源看板」 | 任务运行中排查资源占用 | 资源池、用户排行、实例分布、规格和历史趋势 |

### 9.1 指标说明
判断资源是否紧张时：（资源查看）CPU、内存或沙箱容量中的任意一项接近上限，都可能导致新实例长时间准备或排队。
![alt text](/api/wiki/assets/e59c8210d6314b1ab0f862e97ec1e050.webp)


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
