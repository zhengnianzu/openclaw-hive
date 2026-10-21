# Harness 配置管理 — 使用文档

> 面向使用者：如何在平台上创建、编辑、版本化、发布 harness 配置，以及创建任务时它如何生效。

---

## 一、核心概念

三层结构，从上到下：

```
Harness 类型（openclaw / hermes / claude-code / dsh / codex / pi / grok / opencode / openjiuwen）
    └── 配置方案（scheme）   例如「默认配置」「研究版本」「编码助手」
            └── 版本（version）  例如 1.0、1.1、2.0
```

| 概念 | 说明 | 能否改名 |
|---|---|---|
| **Harness 类型** | 由 `settings/harness_config.json` 定义，新增类型只需改这个 JSON | 否 |
| **配置方案** | 一套配置的业务分组。**创建后名称可改**，磁盘目录不受影响 | 可以随时改 |
| **版本** | 方案在某个时间点的**不可变快照**，有状态（草稿 / 测试中 / 已发布） | 否，版本号创建后不可改 |

### 工作台（原「草稿」）

每个方案有一份**可编辑的工作副本**，叫「工作台」。它不是一个版本，而是「正在改的内容」。

- 改工作台 → 不影响任何已有版本和已建实例
- 「创建版本」→ 把工作台冻结成不可变快照
- 「发布」→ 该版本才能被创建任务时选用

---

## 二、目录结构

### 全局根

```bash
# .env
HARNESS_DIR=/home/.../platform/settings
```

每个方案在自己的数据库行里记着**相对路径**（`harness_configs.harness_dir`），实际路径 = `HARNESS_DIR + harness_dir`。换机器或整体搬配置目录时，只改 `.env` 这一行。

### 单个方案

```
$HARNESS_DIR/harness/<type>/harness-<id>/
    ├── draft/                     ← 工作台（可编辑）
    │     ├── config.yaml
    │     ├── openclaw.json
    │     ├── user_proxy_model.json
    │     └── md/                  ← harness markdown（可选）
    │           ├── AGENTS.md
    │           └── SOUL.md
    ├── 1.0/                       ← 版本快照（只读）
    │     ├── config.yaml
    │     ├── openclaw.json
    │     ├── user_proxy_model.json
    │     └── md/
    └── 1.1/
```

`<id>` 是数据库自增主键，创建后永不变，所以方案名称怎么改都不影响磁盘。

### 三类文件

| 文件 | 来源 | 能否从模板初始化 |
|---|---|---|
| Runtime 配置（`config.yaml` / `openclaw.json` / `user_proxy_model.json`） | `settings/` 根目录的同名文件 | ✅ |
| Harness md（`AGENTS.md` / `SOUL.md` / `IDENTITY.md` …） | `settings/harness_mds/<type>/` | ✅ |
| 版本快照 | 由工作台冻结而来 | ❌（只读） |

---

## 三、界面操作

### 3.1 列表页（Harness 配置管理）

顶部概览：配置方案总数、版本总数、已发布版本数、各类型方案/版本分布。

列：**方案名称 → 类型 → 当前版本 → 版本数 → 描述 → 文件 → 更新时间 → 操作**

- **点任意行** → 进入该方案的工作台
- **更多菜单**：编辑 / 重命名 / 复制 / 设为默认 / 删除

### 3.2 新建方案

1. 列表页右上「新建配置」
2. 选择 Harness 类型
3. 填「方案名称」（如「研究版本」）
4. 保存

新方案的目录是 `harness/<type>/harness-<id>/draft/`，初始**为空**——需要手动初始化文件（见 3.3）。

### 3.3 初始化文件（最重要的一步）

进入工作台后，左侧是文件目录树，分两组：

```
▼ Workspace   0/7      ← harness md
    AGENTS.md      未初始化
    SOUL.md        未初始化
    ...
▼ Runtime     0/3      ← 配置文件
    openclaw.json  未初始化
    config.yaml    未初始化
    user_proxy_model.json  未初始化
```

**灰色的「未初始化」文件点一下**，会弹确认框，确认后从模板复制一份：

| 文件类型 | 模板来源 |
|---|---|
| md | `settings/harness_mds/<type>/<文件名>` |
| Runtime | `settings/<文件名>` |

不需要的文件不用管——未初始化就是不加载，不会报错。

### 3.4 编辑文件

1. 左侧点文件名 → 中栏打开
2. 顶部「编辑 / 预览」切换：`.md` 文件预览走 Markdown 渲染，其他显示原文
3. 改完后点右上「保存」

**保存前会校验**：`.json` 文件会先 `JSON.parse`，格式错误直接拦下不写盘。工具栏还有「格式化」按钮。

**未保存提示**：切换文件、切换版本、离开页面时，如果有未保存改动会弹确认框。

### 3.5 创建版本

1. 改完工作台后，点右上「创建版本」
2. 版本号会**预填下一个建议值**（如已有 1.0、1.1 → 预填 1.2），可改
3. 填「说明」（可选，如「优化角色定位」）
4. 确认

创建后：

- 工作台的全部文件（含 `md/`）被复制到 `<方案目录>/<版本号>/`
- 新版本状态为**草稿**
- 版本历史只追加，不可修改

### 3.6 发布版本

版本行的 `⋯` → **发布**。

只有 `已发布` 的版本才会出现在**创建任务**的版本下拉里。未发布的版本建任务时会返回 400。

> 版本状态：`草稿` → `已发布`。中间的 `测试中` 状态后端已支持，界面暂未开放流转。

### 3.7 对比版本

版本行 `⋯` → **与工作台对比**。

差异**直接显示在中栏**（不再弹抽屉），按文件分 tab，`+` 绿色为新增行、`-` 红色为删除行。

### 3.8 覆盖到工作台

版本行 `⋯` → **覆盖到工作台**。

把该版本的**全部文件**覆盖到工作台，用于「基于旧版本继续改」：

```
工作台乱掉了 → 覆盖到版本 1.1 → 工作台内容 == 1.1 → 继续改 → 创建版本 1.2
```

> 覆盖只改工作副本，**不影响任何版本历史**，也不会产生新版本。

### 3.9 复制方案

列表页 `⋯` → **复制**，输入新方案名称即可。

- 复制的是**当前工作台**的文件，不是某个版本
- 不复制版本历史（新方案版本数为 0）

### 3.10 重命名方案

列表页 `⋯` → **重命名**，或编辑弹窗里改名称。

目录是 `harness-<id>`，改名**不碰磁盘**，历史版本也不受影响。

---

## 四、配置如何生效

### 4.1 创建任务时的解析规则

```
用户选择「配置方案」+「配置版本」
        │
        ▼
后端校验：版本必须存在、必须属于该方案、harness_type 必须一致、状态必须为 published
        │
        ▼
从 <方案目录>/<版本号>/ 读取模板文件
        │
        ├── config.yaml            → 应用表单字段（并发数、OBS 路径、镜像…）
        ├── openclaw.json 等        → 注入 model / baseUrl / apiKey
        └── md/*.md                → 复制到 instances/<id>/workspace/
        │
        ▼
写入 instances/<id>/config.yaml、openclaw.json …
```

### 4.2 字段覆盖规则

实例的 `config.yaml` 里，以下字段**总是被表单覆盖**，改模板没用：

| 界面字段 | 写入路径 |
|---|---|
| 并发数 | `run_config.concurrent_num` |
| 起始/总任务数 | `run_config.start_index` / `total_num` |
| 模型 API Key / Base URL / Model ID | 各 harness 自己的配置路径 |
| 任务标识 | `sandbox_id_prefix` |
| md 文件路径 | `run_config.harness_md`（绝对路径列表） |

### 4.3 harness md 的注入

如果版本目录下有 `md/`：

```yaml
# instances/<id>/config.yaml
run_config:
  harness_md:
    - /…/instances/<id>/workspace/AGENTS.md
    - /…/instances/<id>/workspace/SOUL.md
```

文件被**复制**到实例目录，不是引用源路径——实例之间互不影响。

没有 md 的版本会写 `harness_md: []`，不报错。

### 4.4 快照语义

**实例创建后与版本彻底解耦**。之后改工作台、新建版本、甚至删掉该方案，都不影响已建实例——它的配置在创建时已写进 `instances/<id>/`。

---

## 五、常见流程速查

### 从零做一个方案

```
新建方案 → 进工作台 → 逐个点灰文件初始化 → 编辑 → 保存
  → 创建版本 1.0 → 发布 → 建任务时选用
```

### 改现有方案的配置

```
进工作台 → 改文件 → 保存 → 创建版本 1.1 → 发布
（工作台直接改，不需要先复制版本）
```

### 基于旧版本重做

```
⋯ → 覆盖到工作台 → 改 → 创建版本 1.2 → 发布
```

### 新开一套平行配置

```
列表页 ⋯ → 复制 → 填新名称 → 进新方案改 → 创建版本 → 发布
```

---

## 六、运维

### 换机器 / 迁移配置目录

```bash
# .env
HARNESS_DIR=/new/path/to/settings
```

数据库里的 `harness_dir` 是相对路径，只要 `<HARNESS_DIR>/harness/...` 结构搬过去就能直接用。

### 首次启动

`ensure_defaults()` 会：

1. 为每个 harness 类型建一个默认方案，写入 `harness_dir`
2. 从 `settings/` 根目录的模板复制文件到各方案的工作台
3. 每个方案建一个 `published` 版本（版本号沿用旧记录）

全新机器只要 `settings/` 下有模板文件和 `harness_mds/`，启动即可用。

### 数据库字段

```sql
-- 方案
harness_configs(
  id, name,                      -- 名称（可改）
  harness_type,
  harness_dir,                   -- 相对 HARNESS_DIR，如 harness/openclaw/harness-5
  is_default, description,
  obs_harness_path, obs_task_path, obs_proxy_path,
  created_by, created_at, updated_at
)

-- 版本
harness_config_versions(
  id, config_id, version,        -- 版本号（不可改）
  status,                        -- draft / testing / published
  description, files_json,
  created_by, created_at, published_at, published_by
)
```

目录里的版本路径**不入库**，固定按 `<方案目录>/<version>/` 推导。

---

## 七、排错

| 现象 | 原因 / 处理 |
|---|---|
| 版本下拉里没有选项 | 该方案没有 `已发布` 版本 → 去工作台创建版本并发布 |
| 建任务报「版本 X 尚未发布」 | 同上 |
| 建任务报「快照目录缺失」 | 磁盘上的版本目录被手工删了 → 重新创建同名版本 |
| 工作台改了但实例没变 | 正常。实例用的是创建时选定的版本快照 |
| 文件显示「未初始化」点不动 | 只有 operator 角色能初始化；只读用户只能查看已有的 |
| 改了 `settings/` 下的模板但新实例没变 | 各方案有独立工作台，模板只在「初始化」时复制一次 |
