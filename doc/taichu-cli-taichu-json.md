# TaiChu CLI `.taichu.json` 配置手册

本文只说明 **TaiChu CLI 实际读取的 `.taichu.json` 参数**，以当前 `apps/taichu-cli` 和它装配的 Runtime 为准。它不是所有 TaiChu 宿主共用的配置模板。本文尤其区分模型上下文窗口、输入治理上限、输出预算与压缩阈值；这些值不能互换。

## 配置文件在哪里

CLI 默认读取**启动时所在目录**的 `.taichu.json`，不会沿父目录搜索。也可以用 `--config <path>` 指定文件；相对路径按启动目录解析。选定的同一文件会交给 Runtime 读取 `context`，并供内置工具读取各自配置。修改文件后，应重新创建 CLI Runtime 才能按新配置生效。

下面是一个可按实际模型能力调整的示例。`128000` **只是示例**，不表示 CLI 会探测或保证上游模型支持 128K；密钥占位符也必须替换为实际可用的本地配置，不要提交真实密钥。

```json
{
  "providers": [
    {
      "name": "example-provider",
      "base_url": "<模型服务 API 基地址>",
      "api_key": "<本地密钥>",
      "models": [
        {
          "id": "example-model",
          "wire_protocol": "chat-completions",
          "supports_images": false,
          "max_tokens": 16384
        }
      ]
    }
  ],
  "default_provider": "example-provider",
  "default_model": "example-model",
  "default_thinking": "medium",
  "context": {
    "window_tokens": 128000,
    "active_limit_tokens": 110000,
    "static_limit_tokens": 20000,
    "capability_context_weight_percent": 20,
    "reserve_tokens": 8192,
    "compaction": {
      "session_enabled": true,
      "trigger_tokens": 100000,
      "keep_recent_tokens": 16000
    },
    "lcm": {
      "active_context_target_tokens": 48000,
      "leaf_chunk_tokens": 16000
    }
  }
}
```

`providers` 在 CLI 中是**数组**，`default_model` 是**字符串**。不要直接套用其他宿主的 `providers` 对象映射或 `default_model` 对象写法。

## 模型与常规参数

| 路径 | 类型 | 含义及限制 |
|---|---|---|
| `providers` | 数组 | 模型服务列表；进行模型调用时需要有匹配的 provider 和 model。也接受别名 `llm_providers`。 |
| `providers[].name` | 字符串 | provider 名称，用于与默认 provider、模型专用上下文策略匹配。 |
| `providers[].base_url` | 字符串 | 模型服务地址；选中的 provider 不能是空地址。 |
| `providers[].api_key` | 字符串，可选 | 模型服务密钥。不要将真实值提交到仓库，也不要假设此字段支持环境变量占位符展开。 |
| `providers[].models` | 字符串或数组 | 可写成逗号分隔的模型 ID 字符串、模型 ID 字符串数组，或下表的模型对象数组。对象写法可声明额外能力和预算。 |
| `providers[].models[].id` | 字符串 | 模型 ID。 |
| `providers[].models[].supports_images` | 布尔值，可选 | 是否声明支持图像输入；默认 `false`。 |
| `providers[].models[].wire_protocol` | 字符串，可选 | `chat-completions`（默认）或 `responses`；也接受键名 `wireProtocol`。 |
| `providers[].models[].max_tokens` | 正整数，可选 | 模型策略的**输出上限**，也参与上下文输入预算计算；它不是模型总上下文窗口。未填时采用模型策略的默认值。 |
| `default_provider` | 字符串，可选 | 默认 provider。选择模型时，CLI 会先找声明了该模型 ID 的第一个 provider；因此多个 provider 声明同一 ID 时，不应依赖此字段强制选中另一个 provider。 |
| `default_model` | 字符串，可选 | 默认模型 ID。未填时尝试取默认 provider 的首个模型，再取 provider 列表中首个模型。建议明确填写。 |
| `default_thinking` | 字符串，可选 | 默认推理强度：`off`、`minimal`、`low`、`medium`、`high`、`xhigh`；别名为 `thinking`。实际能否使用还取决于模型服务。 |
| `skill_catalog_max_bytes` | 正整数，可选 | Core/ATD Skill 目录容量，单位字节；默认 16 MiB，允许范围为 `1` 至 `1073741824`。 |
| `provider_max_tokens` | 正整数，可选 | **仅 eval-json 路径**的请求级输出上限；命令行同名参数优先。它不改变 `models[].max_tokens`，也不扩大上下文窗口或输入治理预算。普通交互 CLI 不把它作为输出覆盖值。 |

若只需声明模型 ID，可使用 `"models": ["model-a", "model-b"]` 或 `"models": "model-a,model-b"`；需要设置输出上限时使用对象数组。

## 上下文窗口：`context`

`context` 是可选的顶层对象。**不配置**时沿用 Runtime 原有行为；**一旦配置**，默认 profile 中下列字段必须全部提供，不能只写 `window_tokens` 或只写 `active_limit_tokens`。`context` 内的未知字段、非法值或非法大小关系都会使 Runtime 初始化失败。

这里没有名为 `active_context` 的配置键：输入治理上限用 `active_limit_tokens`，LCM 的活跃上下文目标用 `lcm.active_context_target_tokens`。

| 路径 | 类型 | 实际作用 |
|---|---|---|
| `context.window_tokens` | 正整数 | 当前 profile 声明的**模型总上下文窗口**。应按所选上游模型真实能力设置；这不是 CLI 自动测得的数值。 |
| `context.active_limit_tokens` | 正整数 | 输入治理上限，**不是保证每次请求都可使用的输入 token 数**；还要为保留量和模型输出留下空间。 |
| `context.static_limit_tokens` | 正整数 | Static 层的硬上限。 |
| `context.capability_context_weight_percent` | `1`～`100` | Skill 与 Provider tools 共用的动态能力上下文软额度占比。 |
| `context.reserve_tokens` | 正整数 | 从总窗口中保留、不分配给输入或输出的 token 量。 |
| `context.compaction.session_enabled` | 布尔值 | 是否允许 Session owner 自动压缩；设为 `false` 不会关闭 Jiuwen LCM。 |
| `context.compaction.trigger_tokens` | 正整数 | 压缩触发上限；实际生效值还会受到请求级输入预算约束。 |
| `context.compaction.keep_recent_tokens` | 正整数 | Session 压缩时保留的近期 token 量。 |
| `context.lcm.active_context_target_tokens` | 正整数 | LCM 活跃上下文目标，实际投影还会扣除请求内其他占用。 |
| `context.lcm.leaf_chunk_tokens` | 正整数 | LCM leaf chunk 的 token 量。 |
| `context.models` | 数组，可选 | 按 `provider` + `model` **精确匹配**的模型专用 profile；未匹配时使用顶层默认 profile。每个条目必须重复提供上述完整 profile，不从默认 profile 继承缺失字段。 |

### 四种容易混淆的“窗口”

假设 `window_tokens = 128000`、`reserve_tokens = 8192`、`active_limit_tokens = 110000`，且所选模型的 `models[].max_tokens = 16384`：

1. 配置声明的总窗口为 **128000**。
2. 扣除保留量后，输入与输出合计最多占 **119808**。
3. 为模型策略输出上限预留 **16384** 时，期望输入上限是 `min(110000, 119808 - 16384) = 103424`，**不是 110000**。
4. 为自动缩减输出保留的最低生成空间是 `min(16384, 1024) = 1024`，因此输入硬上限是 `min(110000, 119808 - 1024) = 110000`。这并不保证在这个输入长度下还能生成 16384 token。

`compaction.trigger_tokens` 再与**期望输入上限**取较小值；Static、能力上下文和非历史输入的占用还会减少实际可给历史及 LCM 使用的预算。故 `window_tokens`、`active_limit_tokens`、`models[].max_tokens`、`compaction.trigger_tokens` 分别控制不同边界，不能用一个参数替代另一个。请求级 `provider_max_tokens` 覆盖也**不改变**上述用模型策略输出上限计算的输入治理预算。

### 必须满足的关系

所有 token 数都必须大于零，且：

```text
reserve_tokens < window_tokens
static_limit_tokens <= active_limit_tokens <= window_tokens - reserve_tokens
compaction.keep_recent_tokens < compaction.trigger_tokens <= active_limit_tokens
static_limit_tokens + lcm.leaf_chunk_tokens <= compaction.trigger_tokens
lcm.leaf_chunk_tokens <= lcm.active_context_target_tokens <= active_limit_tokens
1 <= capability_context_weight_percent <= 100
```

如果需要针对某个模型设置不同窗口，可在 `context.models` 中增加条目，例如下例是**完整条目**，不是只覆盖 `window_tokens`：

```json
{
  "provider": "example-provider",
  "model": "small-model",
  "window_tokens": 32768,
  "active_limit_tokens": 24000,
  "static_limit_tokens": 6000,
  "capability_context_weight_percent": 20,
  "reserve_tokens": 2048,
  "compaction": {
    "session_enabled": true,
    "trigger_tokens": 20000,
    "keep_recent_tokens": 5000
  },
  "lcm": {
    "active_context_target_tokens": 12000,
    "leaf_chunk_tokens": 4000
  }
}
```

把该对象放入顶层 `context.models` 数组即可；`provider` 和 `model` 必须与实际选择结果一致，同一组身份不能重复。

## 可选工具及插件配置

以下是同一文件中由 CLI 装配的其他消费者读取的可选顶层参数。未使用相应能力时可以不写。

| 路径 | 配置含义 |
|---|---|
| `search_providers` | Web 搜索 provider 数组；每项含 `name`、`kind`、可选 `base_url` 和 `api_key`。当前仅支持 `kind: "tavily"`，且 Tavily 必须提供 `api_key`。 |
| `default_search_provider` | 选择 `search_providers` 中的名称；配置了搜索 provider 时必填且必须匹配。 |
| `image_generation` | 图像生成配置：`default_provider` 和 `providers` 数组。provider 含 `name`、`kind`、`api_key`、`secret_key`，以及可选的 `base_url`、`model`、`size`；当前 `kind` 为 `volcengine_jimeng`。图像生成的两个密钥字段支持 `${ENV_VAR}` 形式的环境变量引用。 |
| `extended_builtin_tools` | 扩展内置工具声明数组。每项需提供 `name`、`label`、`description`、`parameters`；可选 `order`、`override_policy`、`available_hosts`。声明工具不等于自动赋予宿主执行权限。 |
| `plugins` | 插槽到插件别名的映射，例如 `{"context_engine":"jiuwen"}`。非空时，所选配置文件旁必须有对应的 `plugin.json` 安装清单；仅填别名不足以加载插件。 |
| `decision` | 直连 Decision provider：`endpoint`、`api_key`、`deadline_ms`；endpoint 必须是受支持的 `/v1/systemone?backend=jev` 路径，`deadline_ms` 为 `1`～`60000`。配置会校验，但是否发起请求还受独立的 feature 开关控制。 |

CLI 的 feature 开关不写在此文件中；不要把 `.feature.json` 的字段混入 `.taichu.json`。例如 `decision.enabled` 属于 feature 配置，而 `decision.endpoint`、`decision.api_key`、`decision.deadline_ms` 属于本文件。
