# 沙箱配置管理（Sandbox Config）— 实现文档

> 状态：**已实现**（2026-10-16），架构为**文件权威、DB 存索引**。
> 目标：把"沙箱运行环境"从散落的 YAML 文件变成平台里可管理、可选择的一等实体。

## 架构（文件权威）

```
platform/configs/config_linux.yaml ─┐  抽出沙箱相关段
platform/configs/config_win.yaml   ─┘
        │
        ▼
platform/settings/sandbox/
  ├── linux.yaml            ← 模板（global + sandbox + sandbox_namespace + s3）
  ├── windows.yaml          ← 模板
  ├── linux_1.yaml          ← 派生文件（每条「沙箱配置」一个）
  └── windows_2.yaml        ← 派生文件
        │  ① 界面保存时按字段改写 ② 也可直接编辑原文（源文件面板）
        ▼
创建任务时：把派生文件的 global/sandbox/sandbox_namespace/s3
           **整段替换**进实例 config.yaml
        │
        ▼
instances/<id>/config.yaml
   remote_server: ...   ← 来自 harness 模板（保留不动）
   env_make: ...        ← 来自 harness 模板（保留不动）
   run_config: ...      ← 来自 harness 模板 + 平台改写
   global/sandbox/sandbox_namespace/s3  ← 来自派生文件（整段替换）
```

**DB 只存索引**（`sandbox_configs` 表）：`id / name / sandbox_type / sandbox_key / file / is_default / enabled / description`。
资源、环境变量、控制面、S3 等业务字段**不进 DB** —— 以文件为准，避免两处真相。列表接口的返回对象是「索引 + 文件解析结果」的合成。

## 实际落地的文件

| 位置 | 内容 |
|---|---|
| `settings/sandbox/{linux,windows}.yaml` | **模板**：从 `configs/*.yaml` 抽出的沙箱段 |
| `settings/sandbox/<type>_<id>.yaml` | **派生文件**：每条配置一个，权威内容 |
| [api/core/database.py](api/core/database.py) | `sandbox_configs` 索引表（只 9 列） |
| [api/models/sandbox_config.py](api/models/sandbox_config.py) | `Create` / `Update` / `Info` / `FileContent` |
| [api/routers/sandbox_configs.py](api/routers/sandbox_configs.py) | CRUD + `apply_sandbox_to_config()` + 文件读写 + `ensure_defaults()` |
| [api/routers/instances.py](api/routers/instances.py) | `create_instance` 调 `apply_sandbox_to_config()` 整段替换 |
| [../hive.py](../hive.py) | 解除 `build_make_config("x86_cpu")` 硬编码，改读 `sandbox_active_key` |
| [frontend/src/views/SandboxConfigs.vue](frontend/src/views/SandboxConfigs.vue) | 列表页 + 五面板编辑弹窗 + 源文件面板 |
| [frontend/src/views/CreateInstance.vue](frontend/src/views/CreateInstance.vue) | 「沙箱配置」下拉 |

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/sandbox-configs` | 列表（`?sandbox_type=` `?enabled_only=`） |
| POST | `/api/sandbox-configs` | 新建 → 复制模板为 `settings/sandbox/<type>_<id>.yaml` 并改写 |
| PUT | `/api/sandbox-configs/{id}` | 编辑 → 字段写进派生文件；name/description 写 DB |
| DELETE | `/api/sandbox-configs/{id}` | 删除（含删除派生文件）；默认/被引用时拒绝 |
| POST | `/api/sandbox-configs/{id}/copy` | 连文件内容一起复制 |
| PUT | `/api/sandbox-configs/{id}/set-default` | 同类唯一 |
| PUT | `/api/sandbox-configs/{id}/enabled` | 默认配置不可停用 |
| GET | `/api/sandbox-configs/{id}/file` | 读派生文件原文 |
| PUT | `/api/sandbox-configs/{id}/file` | 写派生文件原文（校验 YAML 可解析） |
| POST | `/api/sandbox-configs/{id}/reset` | 从模板重新派生（丢弃手改） |

## 已确认的产品决策

- **镜像不放进沙箱配置** —— 新建任务页复用 `images` 表的下拉；派生文件里的 `image` 仅作默认值。
- **「资源池」= 控制面集群**（`global.controlplane_base_url` + `api_key`）；`sandbox_namespace` 是独立字段。
- **资源规格按 YAML 文件实际值**（Windows 16核/32Gi），不用设计图 mock 里的 2/3、6/8Gi。
- **沙箱段 = global + sandbox + sandbox_namespace + s3**；`run_config` 归 harness 模板，不在沙箱范围。

## 种子数据

首次启动（索引表为空）时按模板派生两条，幂等：

| name | type | key | 派生文件 | 资源 | 默认 |
|---|---|---|---|---|---|
| `default-linux` | linux | `x86_cpu` | `linux_1.yaml` | 2/3核 · 6/8Gi | ✅ |
| `default-windows` | windows | `windows` | `windows_2.yaml` | 16/16核 · 32/32Gi | ✅ |

## 顺带修掉的既有 bug

[instances.py](../platform/api/routers/instances.py) 原先的 `base.sandbox.x86_cpu.sandbox.image = ...` 在任何模板上都会抛 `ConfigAttributeError` —— 模板 `settings/config.yaml` 没有顶层 `sandbox:` 段，所以「镜像名称」下拉一直是坏的。现改为先整段替换派生文件的沙箱段、再按 `sandbox_active_key` 定位写镜像，缺失时跳过。

---

## 以下为设计阶段的原始调研记录（保留备查）

### 现状梳理

### 1.1 已有的沙箱定义（物理形态：YAML 文件）

| 文件 | 沙箱 key | 类型 | 特点 |
|---|---|---|---|
| `platform/configs/confg_linux.yaml` | `sandbox.x86_cpu` | Linux | 平铺 `resources.cpu_request/cpu_limit`；`architecture: x86` |
| `platform/configs/config_win.yaml` | `sandbox.windows` | Windows | 嵌套 `resources.requests/limits`；额外 `boot_source` / `pod_spec` / `volumes` / `security_context` / `runtime_type` |
| `platform/settings/config.yaml` | `env_make.*` | 通用模板 | 实例创建的 base 模板，结构与上面两者都不同 |

> ⚠️ `confg_linux.yaml` 文件名少了个 `i`，建议改名为 `config_linux.yaml`。

**两类沙箱的结构差异是本次设计最大的约束**：

```
linux:                             windows:
  sandbox:                           sandbox:
    x86_cpu:                           windows:
      architecture: x86                  runtime_type: windows
      accelerator: null                  architecture: x86
      lease_ttl_seconds: 900             lease_ttl_seconds: 3600
      max_ttl_second: 8000               max_ttl_second: 8000
      wait_timeout_seconds: 1200         wait_timeout_seconds: 3600
      sandbox:                           sandbox:
        image: ...                         image: ...
        env: [{name,value}]                node_name / boot_source
        resources:                         resources: {requests, limits}
          cpu_request: "2"                 security_context / volumes
          cpu_limit: "3"                   volume_mounts / pod_spec
          memory_request: "6Gi"            env: [{name,value}]
          memory_limit: "8Gi"              wait_ready: true
```

### 1.2 当前代码里的落点（都要改）

| 位置 | 现状 | 问题 |
|---|---|---|
| [api/routers/instances.py:426](api/routers/instances.py#L426) | `base.sandbox.x86_cpu.sandbox.image = req.image_name` | **硬编码 `x86_cpu`**，Windows 实例根本改不到 |
| [hive.py:452](../hive.py#L452) | `build_make_config("x86_cpu", config=yaml_config)` | **硬编码 `x86_cpu`**，跑 Windows 时取的是错的块 |
| [api/routers/instances.py:377](api/routers/instances.py#L377) | 以 `settings/config.yaml` 为模板生成实例 `config.yaml` | 沙箱段来自模板，与 `configs/*.yaml` 完全脱钩 |
| [api/services/queue_scheduler.py](../platform/api/services/queue_scheduler.py) | 按 `concurrent_num` 估算沙箱占用 | 不知道不同沙箱规格差异（16C32G vs 2C6G） |

**结论**：`configs/*.yaml` 目前只是"被人手动拷进去参考"的文件，平台链路没有消费它。

### 1.3 可参照的既有实现

`harness_configs` 是现成的同构样板，照抄它的分层即可大幅降低设计风险：

- 表：`harness_configs`（`api/core/database.py`）
- 路由：`api/routers/harness_configs.py`（CRUD + `set-default` + `copy`）
- 前端：`frontend/src/views/HarnessConfigs.vue`（列表 + 编辑弹窗 + 文件读写）

---

## 2. 数据模型

新增 `platform.db` 表 `sandbox_configs`，在 `api/core/database.py:init_db()` 里以既有风格创建（`CREATE TABLE IF NOT EXISTS` + 逐列 `ALTER TABLE` 迁移）。

```sql
CREATE TABLE IF NOT EXISTS sandbox_configs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL UNIQUE,   -- 配置名称，如 default-windows
    sandbox_type         TEXT NOT NULL,          -- linux | windows（列表页筛选用）
    sandbox_key          TEXT NOT NULL,          -- config.yaml 里 sandbox.<key>，如 x86_cpu / windows
    resource_pool        TEXT DEFAULT '',        -- 资源池 → sandbox_namespace，如 windows-pool
    cpu_request          TEXT DEFAULT '2',
    cpu_limit            TEXT DEFAULT '3',
    memory_request       TEXT DEFAULT '6Gi',
    memory_limit         TEXT DEFAULT '8Gi',
    env_json             TEXT DEFAULT '[]',      -- [{name, value}] 环境变量
    ttl_seconds          INTEGER DEFAULT 8000,   -- → max_ttl_second
    lease_ttl_seconds    INTEGER DEFAULT 3600,
    wait_timeout_seconds INTEGER DEFAULT 3600,
    s3_uploader_script   TEXT DEFAULT '',        -- → s3.s3_uploader_script
    image                TEXT DEFAULT '',        -- → sandbox.<key>.sandbox.image
    extra_json           TEXT DEFAULT '{}',      -- 类型特有字段（见下）
    is_default           INTEGER DEFAULT 0,      -- 同类型下唯一，默认配置只读不可删
    enabled              INTEGER DEFAULT 1,      -- 停用后不出现在新建任务下拉里
    description          TEXT DEFAULT '',
    created_by           TEXT DEFAULT '',
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**关键设计：`extra_json` 承载类型差异**，避免为 Windows 的一堆 K8s 字段在表里开一堆永远只有 Windows 用的列。

```jsonc
// sandbox_type = windows
{ "runtime_type": "windows", "node_name": null,
  "boot_source": { "type": "image", "ref": "..." },
  "security_context": { "privileged": true },
  "volumes": [...], "volume_mounts": [...],
  "pod_spec": { "nodeSelector": { "node.kubernetes.io/baremetal": "true" } },
  "wait_ready": true }

// sandbox_type = linux
{ "architecture": "x86", "accelerator": null }
```

后端对 `extra_json` 只做 JSON 合法性校验，不解释字段语义；语义在渲染 YAML 时按类型合并。

---

## 3. 后端 API

新建 `api/routers/sandbox_configs.py` + `api/models/sandbox_config.py`，在 `main.py` 注册路由。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/sandbox-configs` | 列表；`?type=linux\|windows`、`?enabled_only=true` |
| GET | `/api/sandbox-configs/{id}` | 详情 |
| POST | `/api/sandbox-configs` | 新建 |
| PUT | `/api/sandbox-configs/{id}` | 编辑（`is_default` 配置拒绝） |
| DELETE | `/api/sandbox-configs/{id}` | 删除（默认配置、被实例引用中的拒绝） |
| POST | `/api/sandbox-configs/{id}/copy` | 复制为副本（`{name}`），**默认配置主要靠这个派生** |
| PUT | `/api/sandbox-configs/{id}/set-default` | 设为该类型默认（同类其余置 0） |
| PUT | `/api/sandbox-configs/{id}/enabled` | 启用/停用（`{enabled: bool}`） |
| POST | `/api/sandbox-configs/import` | 从 `platform/configs/*.yaml` 导入为种子配置 |

**权限**：复用 `require_operator`（写）/ `get_current_user`（读）——与 `harness_configs` 一致。

**`ensure_defaults()`**：仿 `harness_configs.ensure_defaults()`，首次启动时若表为空，自动从 `platform/configs/config_linux.yaml`、`config_win.yaml` 导入，得到设计图里的三条初始数据：

| name | type | key | pool | CPU | 内存 |
|---|---|---|---|---|---|
| `default-windows` | windows | `windows` | `windows-pool` | 2/3 | 6/8 Gi *(按图，实际可沿用 16/32)* |
| `custom-windows` | windows | `windows` | `windows-pool` | 4/8 | 8/16 Gi |
| `default-linux` | linux | `x86_cpu` | `linux-pool` | 2/3 | 6/8 Gi |

---

## 4. 配置渲染（本次的核心改动）

抽一个纯函数 `render_sandbox_block(row) -> dict`，把 DB 行渲染成 config.yaml 里 `sandbox.<key>` 的完整块。**Linux / Windows 两条渲染分支**，各自产出符合自身结构的 dict。

### 4.1 创建实例时

- `InstanceCreate` 增加 `sandbox_config_id: Optional[int]`。
- `create_instance` 里：取到 sandbox 配置后，
  ```
  base.sandbox = { row.sandbox_key: render_sandbox_block(row) }
  base.sandbox_namespace = row.resource_pool
  base.s3.s3_uploader_script = row.s3_uploader_script
  base.s3.s3_download_script = row.s3_uploader_script   # linux 两者同值
  ```
  并把 `sandbox_config_id` 记进 `create_params`（复制/重跑实例时能还原）。
- **未选沙箱配置时保持现状**（用模板），不破坏已有实例。

### 4.2 修掉 `x86_cpu` 硬编码

- 生成的 `config.yaml` 里写一个显式字段 **`sandbox_active_key: <key>`**。
- `hive.py:452` 改为：
  ```python
  key = yaml_config.get("sandbox_active_key") or next(iter(yaml_config.sandbox.keys()))
  config = build_make_config(key, config=yaml_config)
  ```
  兼容旧实例（没有该字段时自动取 `sandbox` 下的唯一键）。

### 4.3 队列调度（可选，二期）

> 全局沙箱容量闸（`check_global_sandbox_capacity` / `MAX_TOTAL_SANDBOXES`）已取消：
> 队列放行只取决于「同队列内是否已有 `preparing`/`running`」，`concurrent_num` 不再参与放行判断。
> 若未来要按 CPU 规格做资源核算，需要重新引入该机制，届时可让队列按 `sandbox_config_id`
> 读 CPU 规格加权。表里已预留字段。

---

## 5. 前端设计

### 5.1 路由与菜单

- `frontend/src/router/index.js` 增加 `{ path: 'sandbox-configs', name: 'SandboxConfigs', component: () => import('../views/SandboxConfigs.vue') }`
- `Layout.vue` 菜单加一项「沙箱配置」（图标 `Grid` / `Cpu`），位置放在「Harness配置」之后。
- 新建 `frontend/src/views/SandboxConfigs.vue`，整体结构照抄 `HarnessConfigs.vue`（列表 + 编辑页）。

### 5.2 列表页（对应设计图左上）

- 顶部：标题「沙箱配置 / 统一管理任务运行所需的沙箱模板」+ 右侧「+ 新建配置」按钮。
- 筛选条：`全部 (3)` / `Linux (1)` / `Windows (2)` 分段按钮 + 右侧「搜索配置名称」输入框（前端本地过滤即可）。
- 表格列：

  | 列 | 渲染 |
  |---|---|
  | 配置名称 | `name` + `is_default` 时跟一个「默认」tag |
  | 类型 | 图标 + `Linux`/`Windows`（Windows 用蓝色方块图标，Linux 用企鹅图标） |
  | 资源池 | `resource_pool` |
  | CPU 请求/限制 | `2 / 3 核` |
  | 内存请求/限制 | `6 / 8 GiB` |
  | 状态 | `enabled ? 启用 : 停用`（绿点/灰点） |
  | 操作 | 默认配置 → `查看` `复制`；非默认 → `编辑` `复制` `停用` + `···` 更多（设为默认/删除） |

- 底部：`共 N 条` + 分页（与设计图一致的 Element Plus `el-pagination`）。

### 5.3 新建 / 编辑页（对应设计图右上 + 下方四块）

左右布局：左侧 `el-menu` 锚点导航，右侧内容面板。**注意设计图里四块内容是并列展示的，实际实现建议用左侧导航切换（也可保留纵向平铺滚动）**——建议按左侧导航切换，编辑态一屏一块，更清爽。

五个面板：

| 面板 | 字段 |
|---|---|
| 基本信息 | 配置名称（必填、唯一）；沙箱类型（Linux / Windows 单选，**编辑态禁用**，因为类型决定 `sandbox_key` 和 `extra_json` 结构） |
| 资源配置 | 资源池（下拉，`allow-create`；预设 `windows-pool` / `linux-pool`）；CPU 请求(核)* / CPU 限制(核)* ；内存请求* / 内存限制*；底部蓝色提示条「Request 用于资源调度，Limit 用于限制资源使用上限。请输入大于 0 的数值，且 Request 不得大于 Limit」 |
| 环境变量 | Key / Value 两列表格 + 删除按钮 +「+ 添加环境变量」按钮 |
| 超时设置 | TTL（秒）* → `ttl_seconds`；Lease TTL（秒）* → `lease_ttl_seconds`；Wait Timeout（秒）* → `wait_timeout_seconds`；每项下方一行灰色说明文字 |
| S3 上传配置 | `s3_uploader_script` 多行文本域（设计图里提示"填写脚本内容或脚本引用路径"）；底部说明「建议采用脚本引用或版本 ID，避免在模板中直接保存敏感凭据」 |

**前端校验**（与提示条一致，提交前拦截）：
- CPU/内存 请求 ≤ 限制
- 数值 > 0
- 配置名称非空且不与现有重名
- 三个超时为正整数

Windows 类型额外显示一个「高级（Windows）」折叠面板，直接编辑 `extra_json` 的 JSON（`boot_source` / `pod_spec` / `volumes` 等），一期可先用 JSON 文本框，二期再做表单化。

### 5.4 新建任务页联动

[CreateInstance.vue](frontend/src/views/CreateInstance.vue) 的「高级配置」tab 增加：

```
沙箱配置 [下拉]  → 选项来自 GET /api/sandbox-configs?enabled_only=true
                  显示 name + 类型标签；默认选中该类型 is_default 的配置
```

- 选中 Windows 沙箱时，若当前 Harness 类型/镜像与之冲突，给一条提示（不强制阻断）。
- 该字段进 `form`，随 `POST /api/instances` 提交 `sandbox_config_id`。
- **与现有「镜像名称」字段的关系需要决策**（见 §8 待确认）。

---

## 6. 实施步骤

| 阶段 | 内容 | 产出 |
|---|---|---|
| P0 | DB 表 + `ensure_defaults()` 从 `configs/*.yaml` 导入种子 | 三条默认配置入库 |
| P1 | `render_sandbox_block()` + `create_instance` 接入 `sandbox_config_id` | 新实例 config.yaml 正确反映所选沙箱 |
| P2 | `hive.py` 去掉 `x86_cpu` 硬编码，改读 `sandbox_active_key` | Windows 沙箱能真正跑起来 |
| P3 | `sandbox_configs` router + 模型 + `main.py` 注册 | API 可用（curl 可验） |
| P4 | `SandboxConfigs.vue` 列表页 + 路由 + 菜单 | 列表可看可操作 |
| P5 | 编辑页五个面板 + 校验 | 增删改查闭环 |
| P6 | `CreateInstance.vue` 加沙箱下拉 | 端到端打通 |
| P7 | （可选）从既有实例反推配置导入；队列按 CPU 规格加权 | 体验与调度优化 |

P0–P2 是后端主干，先做能立刻验证 Windows 链路是否通；P3–P6 是界面。

---

## 7. 兼容与迁移

- **不改已有实例**：`create_params` 里没有 `sandbox_config_id` 的旧实例走原模板路径；`hive.py` 对缺失 `sandbox_active_key` 做回退。
- **不改已有 API 契约**：`InstanceCreate` 只是新增可选字段，`image_name` 等字段语义不变。
- **`configs/*.yaml` 的定位变更**：从"参考文件"变为"种子来源"。导入后仍保留文件，但不再是运行时真源（真源是 DB）。建议在文件头加注释说明这一点。
- **回滚**：新表不影响既有表结构；不选沙箱配置时行为与现在完全一致。

---

## 8. 待确认问题（已全部定夺，见上方「已确认的产品决策」）

1. **镜像字段归谁？** 现有 `CreateInstance` 有「镜像名称」下拉（来自 `images` 表），而沙箱配置里天然也有 `image`。二选一会让用户困惑。建议：**沙箱配置持有默认镜像，新建任务页的镜像下拉作为"本次覆盖"**，留空则用沙箱配置的。
2. **资源池 = `sandbox_namespace` 吗？** `configs/*.yaml` 里叫 `sandbox_namespace: omni-env-default-worker`（两个文件同值），设计图里叫 `windows-pool` / `linux-pool`。需要确认资源池是另一个概念（如 K8s 集群/节点池）还是就是 namespace。
3. **Windows 的 2/3 核、6/8Gi 是不是笔误？** 现有 `config_win.yaml` 是 16 核 / 32Gi，设计图 mock 里写的是 2/3、6/8Gi。种子数据按哪个？
4. **谁能编辑沙箱配置？** 建议 operator 及以上；默认配置任何人都只能查看/复制。
5. **`extra_json` 一期是否上表单？** 建议一期先用 JSON 文本框兜底，避免为 Windows 的 K8s 字段做大量表单工作。

---

## 9. 顺带发现的问题（与本需求无关，但建议看一眼）

`platform/settings/hermes/` 下存在极深的递归拷贝目录：

```
settings/hermes/默认_copy2/hermes/默认_copy2/.../hermes/默认_copy2/...
```

`harness_configs` 的 `copy` 接口在"版本名推导目录"上有 bug：复制时把 `hermes/<版本>` 又当成 `settings` 根再拼一次，于是每复制一次就嵌套一层。建议在实现沙箱配置的 `copy` 时不要照抄这段目录推导逻辑。
