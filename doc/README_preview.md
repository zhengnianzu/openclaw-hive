# 沙箱实时日志预览（日志中心「实时」tab）

本文描述平台日志中心的**沙箱实时日志**功能：在任务运行期间，把沙箱内正在写入的
`run.log` 增量拉到平台本地并在中栏浏览区展示，涉及的全部代码位置与排查方法。

> **解决的问题**：沙箱执行不回传过程输出，只在任务收尾后才给结果。
> Hive 创建沙箱后会把 `env_id` 打进 `task-<idx>.log`，但此后到任务结束之间
> 平台侧完全看不到沙箱内部在做什么。
>
> 实测盲区（`outputs/config/logs/task-1.log`）：`env_id` 出现在第 2 行（4.8s），
> 任务收尾在 255.4s —— **中间 250 秒无任何可见输出**。任务卡在 gateway 建立
> 或 agents 下载之后时，无法判断是真的在跑还是已经挂住。

---

## 一、行为

- 只对**运行中**的 `task-<idx>.log` 提供，左栏选中该任务后中栏出现「实时」tab。
- 点开后每 2s 向沙箱发一次增量探测，拉取沙箱内 `run.log` 的新字节。
- 首帧是**全量快照**（从第 0 字节开始），不是尾部窗口 —— 打开时不丢历史。
- 拉到的字节 append 到平台本地缓存文件，**响应一律从该缓存文件回读**。
- 任务结束后「实时」tab 消失，自动落回「日志」tab。
- **每切换一个任务需重新点击一次「实时」**，不自动续上（见第五节）。
- 不会调用 `client.close()`；SDK 的该方法会关闭沙箱本身。

### 远端与本地路径

沙箱内（由 `resolve_export_settings` 从 `config.yaml` 推导，Linux 沙箱实测）：

```text
/home/ma-user/workspace/workdir/run.log
```

平台本地缓存：

```text
outputs/<config文件名>/sandbox_live/<env_id>/run.log
```

该目录已被 `.gitignore` 覆盖（`platform/instances/` 与 `outputs/`），随实例产物
一起被现有清理流程带走，不单独清理。

---

## 二、请求链路

```text
前端 midTab='live' → setInterval 2s → pollLive()
  └─ GET /api/logs/{instance_id}/sandbox-live/{task-N.log}?offset=N
       ├─ ① _resolve_log_file 文件名白名单校验（正则不接受 '/' 与 '..'）
       ├─ ② _scan_events(task-N.log) → summary{env_id, status}
       │     ├─ status 非空  → task_running=false，不 attach，直接返回
       │     └─ env_id 为空  → "环境尚未就绪"，前端继续等
       ├─ ③ OmegaConf.load(config_path) → resolve_export_settings
       │     → 得到沙箱内 run.log 的绝对路径
       ├─ ④ _get_client(env_id)：命中缓存则复用，否则 attach_sandbox
       ├─ ⑤ _read_remote_chunk(offset=本地缓存大小, 256KB)  ← 一个 exec RPC
       ├─ ⑥ append 到本地缓存文件
       └─ ⑦ 从缓存文件 seek(offset) 读出 → 返回
```

### 代码位置

| 职责 | 位置 |
|---|---|
| 后端接口 | `platform/api/routers/sandbox_live.py` |
| 路由注册 | `platform/main.py`（`app.include_router(sandbox_live.router)`） |
| `env_id` 进 summary | `platform/api/routers/logs.py` 的 `_scan_events` |
| 前端 tab 与轮询 | `platform/frontend/src/views/InstanceDetail.vue`（`.logc-mid` 中栏） |
| 沙箱探测底层 | `sandbox_log_tap.py`（`_attach_client` / `_read_remote_chunk` / `resolve_export_settings`） |

**复用说明**：没有用 `sandbox_log_tap.export_sandbox_log`（它自带 follow 循环、
写死本地文件），而是复用更底层的三个函数，由接口自己控制「一次请求拉一段」的
节奏 —— 这正好匹配 HTTP 轮询模型。详见 [sandbox_log_tap.md](sandbox_log_tap.md)。

---

## 三、「运行中」怎么判定

判据来自 `_scan_events` 的 summary，**不需要新增 DB 字段**：

> 有 `env_id`（沙箱已建）**且** `status` 为空（还没出现 `任务执行状态=` 行）

`task_records.status` **不适用** —— 它只有 `任务成功` / `任务异常`，运行中是
`NULL`，和「未执行」无法区分。

实测对照（instance `2611101123-wanyi2611101123-78af`）：

| task | env_id | status | 结论 |
|---|---|---|---|
| task-96 | 有 | `None` | 运行中 → tab 显示 |
| task-11 | 有 | `任务异常` | 已结束 → tab 隐藏、不 attach |

---

## 四、响应字段与状态提示

```json
{
  "text": "...", "next_offset": 89647, "size": 89647,
  "task_running": true, "sandbox_alive": true,
  "truncated": false, "note": ""
}
```

- `next_offset` 是**字节**偏移，`text` 是解码后的字符串，两者长度本就不同
  （多字节 UTF-8）。前端只需把 `next_offset` 原样回传。
- `truncated=true` 表示远端日志被截断/重建，前端清屏重来。

### note 取值与含义

| note | 含义 | 是否已 attach 沙箱 |
|---|---|---|
| `""` | 正常 | 是 |
| `任务日志尚未生成` | `task-<idx>.log` 还不存在 | 否 |
| `任务已结束` | 已出现 `任务执行状态=` 行 | 否 |
| `环境尚未就绪` | 日志里还没打出 `env_id`，沙箱未建好 | 否 |
| `沙箱内日志尚未创建` | **探测成功到达沙箱，但沙箱内 `run.log` 还没被创建** | 是 |
| `暂时连不上沙箱，重连中：…` | 控制面未确认终态，按可重试处理 | 尝试过 |
| `探测失败：…` | 探测脚本本身出错（非连通性问题） | 是 |

> **`沙箱内日志尚未创建` 是正常中间状态**，不是故障。沙箱刚建好、
> harness 还没开始往 `run.log` 写时就会看到它。等几秒即可。
> 它与 `环境尚未就绪` 的区别：后者连沙箱都还没建，前者沙箱已在但文件未生成。

---

## 五、资源模型：按需 + 每任务显式点击

每轮轮询是**一个 exec RPC**，所以成本控制是这个功能的核心约束：

- **只在有人看时连接**。前端打开「实时」tab 才会打到接口；关掉后不再有请求。
- **切 task 无条件停**，不自动续上新沙箱。刻意要求每个任务手动点一次 ——
  自动续上会在用户只是浏览任务列表时悄悄打一串沙箱。
- **切出「日志」页面大 tab 也停**（`watch(activeTab)` 里调 `stopLive()`）。
- **attach 的 client 复用窗口 60s**（`_ATTACH_TTL`），避免每 2s 重新 attach。
  过期时**只丢引用、不调 `close()`**。
- **按 `env_id` 加 `asyncio.Lock`**：两个浏览器看同一 task 不会并发 append
  同一文件。

因为本地缓存保留，重新点开同一个 task **不需要从头拉** —— 缓存文件大小本身
就是续传游标，不额外存。

### 前端状态三态

「沙箱是否活着」与「是否在轮询」是两件事，任务跑完后沙箱关了但已缓存内容仍可读：

| 标签 | 条件 | tab 绿点 |
|---|---|---|
| 跟随中 | `liveAlive && liveFollowing` | 绿色呼吸 |
| 已停止 | `liveAlive && !liveFollowing` | 灰色静止 |
| 沙箱已关闭 | `!liveAlive` | 灰色静止 |

---

## 六、异常分级

沿用 `sandbox_log_tap` 的异常层级（`SandboxClosed ⊂ SandboxUnreachable ⊂
LogExportError`），`except` 顺序必须最具体优先：

| 异常 | 处理 |
|---|---|
| `SandboxClosed` | 控制面确认终态 → `sandbox_alive=false`，**保留已缓存内容** |
| `SandboxUnreachable` | 分不清沙箱刚关还是网络抖动 → "重连中"，**不判死** |
| `LogExportError` | 探测脚本自身失败 → "探测失败" |

三者都**不返回 5xx**，而是 200 + `note`，让前端显示提示而非报错页。

**不靠重试次数判死**：用次数推断沙箱死亡会让网络抖动导致日志提前放弃，而日志
随沙箱销毁就永久丢失。只有控制面给出终态结论才收尾。

---

## 七、UTF-8 截断处理

按 256KB 切块可能切断一个多字节字符。直接 `errors="replace"` 会产生**永久的**
`�` —— 下一轮从坏字节之后继续，坏字节再也补不回来。

处理方式：末尾不完整的序列**退回去**，留给下一轮与后续字节拼成完整字符。

```text
naive  → 任务执�      （永久损坏）
fixed  → 任务执        （残缺字节留给下一轮）
```

---

## 八、验证与排查

### 验证接口

```bash
cd /root/openclaw-hive/platform
# 挑一个运行中的实例
sqlite3 platform.db "SELECT id, config_path FROM task_instances WHERE status='running' LIMIT 1;"
```

实测结果（走 FastAPI 完整栈）：

```text
task-96.log  200 running=True  alive=True  len=85887 next=89647
task-11.log  200 running=False alive=False len=0     note='任务已结束'
main.log     400 实时日志仅适用于 task-<idx>.log
```

裸 HTTP 的鉴权与校验：

```text
无 token        → 401（在任何沙箱操作之前）
main.log        → 400（已鉴权时；非 task 日志被拒）
..%2F..%2Fetc%2Fpasswd → 404（路径穿越被拒）
```

### 绕过 UI 直接拉日志

接口不通时，先用脚本确认 attach + 探测链路本身可用：

```bash
cd /root/openclaw-hive
# env_id 从该实例的 task 日志里取
grep -h "env_id=" <instance>/outputs/config/logs/task-<idx>.log | head -1
python sandbox_log_tap.py --config <instance>/config.yaml \
    --env-id <env_id> --log run.log --follow
```

### 常见现象对照

| 现象 | 原因 |
|---|---|
| 「实时」tab 不出现 | 该 task 已结束，或日志里还没打出 `env_id` |
| 显示`沙箱内日志尚未创建` | 正常中间状态，沙箱已建但 harness 还没写 `run.log` |
| 切任务后 tab 掉回「日志」 | **预期行为**，每任务需手动点一次（第五节） |
| 一直「重连中」 | 控制面未确认终态；沙箱可能正忙，或网络抖动 |
| 内容停住但 tab 还在 | 沙箱已关而控制面尚未给出终态；已缓存内容仍可读 |

---

## 九、当前范围

**已落地**：后端接口 + 前端「实时」tab，对运行中任务的 `run.log` 按需增量预览。

**未包含**：

- 只做 `run.log`。后端已支持 `?log=gateway.log`，前端未暴露下拉。
- 无 WebSocket/SSE，用 HTTP 轮询（复用现有 `api.get` 与 token 拦截器）。
- 缓存文件不单独清理，随实例产物生命周期。
- 多人同时看同一 task 时，client 已按 `env_id` 复用，但 RPC 仍是每会话一次；
  如需进一步合并可在同一 `env_id` 上加秒级 chunk 缓存。
