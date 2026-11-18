# Base URL 规范化改造

## 1. Excel 原始内容

> 2026-11-18 按当前代码复核重生成。行号以当日 `instances.py` / `prepare_config.py` 为准。
> user_simulator 全 harness 统一收口在 `instances.py:1605-1610`（两种协议都补 `/v1`），不再逐行标注。
> Responses 协议按实现方案约定不在本次统一规范化范围，实现列标 "—"（透传模板默认）。

| harness | 可切协议？ | 协议 | key 值 | 接口值 | Base URL 应为 | 主 Agent | Evaluator | user_simulator |
|---|---|---|---|---|---|---|---|---|
| openclaw | ✅ 界面可选 | anthropic | `api` | `anthropic-messages` | 带 `/v1` | ✅ `instances.py:1435-1444` | ✅ `prepare_config.py:697-700` | `/v1` |
| openclaw | ✅ 界面可选 | Chat Completions | `api` | `openai-completions` | 带 `/v1` | ✅ 同上 | ✅ 同上 | `/v1` |
| openclaw | ✅ 界面可选 | responses | `api` | `openai-responses` | 带 `/v1` | — 无条件补（恰好对） | — 统一补 | `/v1` |
| openclaw-tui | ✅ 界面可选 | anthropic | `api` | `anthropic-messages` | 带 `/v1` | ✅ `instances.py:1435-1444` | ✅ `prepare_config.py:697-700` | `/v1` |
| openclaw-tui | ✅ 界面可选 | Chat Completions | `api` | `openai-completions` | 带 `/v1` | ✅ 同上 | ✅ 同上 | `/v1` |
| openclaw-tui | ✅ 界面可选 | responses | `api` | `openai-responses` | 带 `/v1` | — 无条件补（恰好对） | — 统一补 | `/v1` |
| opencode | ✅ 界面可选 | anthropic | `npm` | `@ai-sdk/anthropic` | 带 `/v1` | ✅ `instances.py:1159-1170` + `1178-1181` | ✅ `prepare_config.py:328-346` | `/v1` |
| opencode | ✅ 界面可选 | Chat Completions | `npm` | `@ai-sdk/openai-compatible` | 带 `/v1` | ✅ 同上 | ✅ 同上 | `/v1` |
| opencode | ✅ 界面可选 | responses | `npm` | — | 带 `/v1` | — 无条件补 | — 统一补 | `/v1` |
| pi | ✅ 界面可选 | anthropic | `api` | `anthropic-messages` | 裸 | ✅ `instances.py:1252-1264` | ✅ `prepare_config.py:397-405` | `/v1` |
| pi | ✅ 界面可选 | Chat Completions | `api` | `openai-completions` | 带 `/v1` | ✅ 同上 | ✅ 同上 | `/v1` |
| pi | ✅ 界面可选 | responses | `api` | `openai-responses` | 带 `/v1` | — 未处理（透传） | — 未处理 | `/v1` |
| dsh | ✅ 界面可选 | anthropic | `api` | `anthropic-messages` | 裸 | ✅ `instances.py:1337-1349` | ✅ `prepare_config.py:555-563` | `/v1` |
| dsh | ✅ 界面可选 | Chat Completions | `api` | `openai-completions` | 带 `/v1` | ✅ 同上 | ✅ 同上 | `/v1` |
| dsh | ✅ 界面可选 | responses | `api` | `openai-responses` | 带 `/v1` | — 未处理（透传） | — 未处理 | `/v1` |
| jiuwenswarm-dev / -xiaoyi | ✅ 界面可选 | anthropic | `client_provider` | `Anthropic` | 带 `/v1` | ✅ `instances.py:1408-1418` | ✅ `prepare_config.py:633-636` | `/v1` |
| jiuwenswarm-dev / -xiaoyi | ✅ 界面可选 | openai | `client_provider` | `OpenAI` | 带 `/v1` | ✅ 同上 | ✅ 同上 | `/v1` |
| claude-code | ❌ 无选择器 | 固定 anthropic | — | — | 裸 | ✅ `instances.py:1113-1116`（去 `/v1`） | — 无 agents 注入 | `/v1` |
| hermes | ✅ 已加选择器 | anthropic | `transport` | `anthropic_messages` | 带 `/v1` | ✅ `instances.py:1091-1099` | — 无 prepare_config 分支 | `/v1` |
| hermes | ✅ 已加选择器 | Chat Completions | `transport` | `chat_completions` | 带 `/v1` | ✅ 同上 | — 同上 | `/v1` |
| hermes | ✅ 已加选择器 | responses | `transport` | `codex_responses` | 带 `/v1` | — 仅两值映射，responses 未处理 | — 同上 | `/v1` |
| codex | ❌ 无选择器 | 固定 responses | — | — | 带 `/v1` | ✅ `instances.py:1221-1224`（有意固定补） | ✅ `prepare_config.py:462-465`（注释：Responses 保留原行为） | `/v1` |
| grok | ✅ 已加选择器 | anthropic | `api_backend` | `messages` | 带 `/v1` | ✅ `instances.py:1298-1307` | ✅ `prepare_config.py:507-516` | `/v1` |
| grok | ✅ 已加选择器 | Chat Completions | `api_backend` | `chat_completions` | 带 `/v1` | ✅ 同上 | ✅ 同上 | `/v1` |
| grok | ✅ 已加选择器 | responses | `api_backend` | `responses` | 带 `/v1` | — 仅两值映射，responses 未处理 | — 仅两值映射 | `/v1` |

## 2. 实现方案

### 目标

根据 `doc/baseurl规范化.xlsx` 规范模型 Base URL 的末尾 `/v1` 处理。主 Agent 与
`user_simulator` 在 `instances.py` 处理，evaluator 在 `prepare_config.py` 处理。

本次只处理以下两种协议：

- `openai-completions`（以及 harness 内部的 Chat Completions 别名）
- `anthropic-messages`（以及 harness 内部的 Anthropic Messages 别名）

前端表单只提供以上两种协议，本次实现不增加 Responses 分支。

### 两个处理阶段

不抽取公共 Base URL 规范化函数。不同 harness 的配置结构、字段名和协议字段并不一致，
因此规则直接写在对应 harness 分支中：

- `platform/api/routers/instances.py`：在各 `req.harness_type` 分支中处理主 Agent；
- `prepare_config.py`：在各 `framework` 分支中处理 evaluator 和其他辅助 Agent；
- `instances.py` 写入 `user_proxy_model.json` 前：统一处理所有 harness 共用的
  `user_simulator.base_url` 字段。

每个分支都先去掉末尾斜杠，再按本 harness 的协议要求补充或移除 `/v1`，所以重复执行
不会生成 `/v1/v1`。

### 规则矩阵

| 使用位置 | Anthropic Messages | Chat Completions |
|---|---|---|
| `user_simulator`（所有 harness） | 补 `/v1` | 补 `/v1` |
| opencode | 补 `/v1` | 补 `/v1` |
| grok | 补 `/v1` | 补 `/v1` |
| claude-code | 去 `/v1` | 不适用 |
| pi、dsh | 去 `/v1` | 补 `/v1` |
| openclaw、openclaw-tui | 补 `/v1` | 补 `/v1` |
| hermes | 补 `/v1` | 补 `/v1` |
| jiuwenswarm 系列 | 补 `/v1` | 补 `/v1` |

### Hermes 与 Grok 协议字段映射

前端统一提交标准协议值，后端在生成 harness 配置时转换为各自的内部字段：

| Harness | 前端值 | 配置字段 | 写入值 |
|---|---|---|---|
| hermes | `anthropic-messages` | `providers.<model.provider>.transport` | `anthropic_messages` |
| hermes | `openai-completions` | `providers.<model.provider>.transport` | `chat_completions` |
| grok | `anthropic-messages` | `model.default-model.api_backend` | `messages` |
| grok | `openai-completions` | `model.default-model.api_backend` | `chat_completions` |

#### Hermes 配置示例

Hermes 与其他 harness 的主要差异是：`model.provider` 只保存当前 provider 名称，
Base URL、Key、协议类型和 provider 默认模型都写入 `providers.<name>`。例如模板中
`model.provider: custom`，前端选择 `Anthropic Messages` 后，生成的相关配置为：

```yaml
model:
  default: claude-opus-4-7-thinking
  provider: custom

providers:
  custom:
    api: http://gateway.example:8082/v1
    api_key: sk-example
    transport: anthropic_messages
    default_model: claude-opus-4-7-thinking
```

如果前端选择 `OpenAI Completions`，模型部分变为：

```yaml
model:
  default: claude-opus-4-7-thinking
  provider: custom

providers:
  custom:
    api: http://gateway.example:8082/v1
    api_key: sk-example
    transport: chat_completions
    default_model: claude-opus-4-7-thinking
```

选择器留空时不新增或覆盖当前 provider 的 `transport`，继续使用所选 Hermes 配置模板
的默认行为。自动申请的新 Key 也写回同一个 `providers.<model.provider>.api_key`。

### 调用链

#### 主 Agent

实例创建时，`platform/api/routers/instances.py` 的每个 harness 配置分支根据自己的字段
结构直接处理 URL。这次补齐了 openclaw/openclaw-tui、opencode、pi、dsh、
jiuwenswarm、hermes、claude-code 与 grok，不经过公共函数或通用规则表。

Codex 使用自身固定协议，保留原有行为，不纳入本次改造范围。

#### user_simulator

`instances.py` 在 `user_proxy_model.json` 写盘前统一处理 `user_simulator`：

1. 支持 `agents[]` 新格式；
2. 支持 `user_proxy_*` 旧字段；
3. 支持直接沿用模板默认配置；
4. 最终 Base URL 保证带 `/v1`。

规范化只执行在统一收口处，不需要在每个 harness 分支重复处理。

#### evaluator

`prepare_config.py` 读取每个沙箱自己的 `user_proxy_model.json` 副本后，由当前 framework
分支在写入具体配置字段时规范化 evaluator（以及其他非 `user_simulator` 的辅助 agent）。
因此：

- 不会修改实例级原始模板；
- 每个沙箱仍使用独立配置副本；
- evaluator 使用当前 framework 对应的 URL 规则；
- 原始 `model_cfg` 不进行提前、跨 framework 的统一改写。

### 示例

```text
输入:  http://gateway:8082
协议:  openai-completions
结果:  http://gateway:8082/v1

输入:  http://gateway:8082/v1/
协议:  anthropic-messages
harness: pi
结果:  http://gateway:8082

输入:  http://gateway:8082
协议:  anthropic-messages
harness: opencode
结果:  http://gateway:8082/v1
```
