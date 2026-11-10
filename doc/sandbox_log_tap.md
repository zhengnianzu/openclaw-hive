# 独立沙箱日志导出器

`sandbox_log_tap.py` 是独立于 `hive.py` 的后端日志插件。它根据已有沙箱的
`env_id` 调用 SDK `attach_sandbox` 重建只用于日志读取的 client，不复用
Hive 任务所持有的 `ExecutionClient`。

当前阶段只负责把沙箱日志生成到本地 `log_exports` 目录，不包含前端和
HTTP/SSE 接口。

## 行为

- 输入 `env_id` 和日志文件名 `run.log` / `gateway.log`。
- 从 `config.yaml` 读取控制面地址、API key 和日志路径。
- 首次快照是调用时日志文件的全量内容，不是尾部 200KB。
- 内部按 256KB 分块传输，避免大日志一次进入内存。
- 开启 `--follow` 后，全量快照完成才会持续追加新字节。
- 沙箱是否已关闭由控制面判定，不靠猜异常类型（见下节）。
- 不会调用 `client.close()`；SDK 的该方法会关闭沙箱。

## 一次性导出全量 run.log

```powershell
python sandbox_log_tap.py `
  --config config.yaml `
  --env-id zx-xxxxxxxx `
  --log run.log
```

默认生成：

```text
outputs/<config文件名>/log_exports/<env_id>/run.log
```

例如 `--config config.yaml --env-id zx-123` 会生成：

```text
outputs/config/log_exports/zx-123/run.log
```

## 全量快照后持续追踪

```powershell
python sandbox_log_tap.py `
  --config config.yaml `
  --env-id zx-xxxxxxxx `
  --log run.log `
  --follow
```

`--follow` 会一直运行到沙箱关闭，也可以用 `Ctrl+C` 提前停止。两种情况下
已写入的本地日志都会保留。

## 导出 gateway.log

```powershell
python sandbox_log_tap.py `
  --config config.yaml `
  --env-id zx-xxxxxxxx `
  --log gateway.log `
  --follow
```

`gateway.log` 的默认本地路径为：

```text
outputs/config/log_exports/zx-xxxxxxxx/gateway.log
```

## 自定义输出目录

```powershell
python sandbox_log_tap.py `
  --config config.yaml `
  --env-id zx-xxxxxxxx `
  --log run.log `
  --output-dir D:\hive-logs\log_exports
```

实际文件为：

```text
D:\hive-logs\log_exports\zx-xxxxxxxx\run.log
```

## 沙箱是否关闭：以控制面为准

任务跑完后 Hive 会在 `finally` 里关掉沙箱。而「沙箱已关闭」和「网络抖了一下」
在探测这一层**长得一模一样** —— 请求都是挂到超时然后抛 `httpx.ReadTimeout`
（注意它既不是 `OSError` 也不是 `ValueError`）。靠异常类型猜会两头都猜不准：
把关闭当成失败会刷一屏 traceback，把抖动当成关闭会让 `--follow` 提前收尾。

所以判定权交给控制面。`fetch_sandbox_state()` 调 SDK 的 `get_sandbox_info()`，
状态在 `payload["sandbox"]["status"]`（与 SDK 内部 `wait_sandbox_ready` 取值方式
一致），已关闭的还会带 `close_reason` 和 `closed_at`：

```json
{"sandbox_id": "zx-xxxxxxxx", "status": "closed",
 "close_reason": "manual_close",
 "closed_at": "2026-11-09T01:58:44.169586+00:00"}
```

终态是 `{"closed", "deleted"}`；控制面返回 404 时按 `deleted` + `sandbox_not_found`
处理。

### 只在两个时机查控制面

1. **attach 之前查一次。** 沙箱早就关了的话立刻给准确报错，不必先 attach、
   再等探测挂满 30 秒。实测 0.27 秒返回，并直接说明是谁在什么时候关的。
2. **探测失败时查。** 这正是需要分辨「关了」和「抖了」的时刻。

`--follow` 的空闲轮询**不查**控制面，所以稳态跟日志时不产生任何额外的控制面
请求。代价是沙箱在跟随过程中关闭时，要先等一次探测挂满 `PROBE_TIMEOUT`
（30 秒，模块常量，不对外暴露）才会发现，然后由控制面给出准确结论并干净退出。

### 判定结果如何影响行为

| 控制面说法 | 行为 |
|---|---|
| `closed` / `deleted` | 抛 `SandboxClosed`，**不重试** —— 沙箱不会自己回来 |
| `running` | 判定为网络抖动，重试（`UNREACHABLE_RETRIES = 2`，间隔 2s） |
| 问不出来（控制面自己连不上） | 退回异常启发式，同样走重试 |

`fetch_sandbox_state()` 对任何异常都只返回 `SandboxState("")`（unknown），
**绝不往外抛** —— 状态查询不能成为新的失败来源。关键不变式：
**unknown ≠ closed**，控制面挂掉时不会被误判成沙箱关闭而提前收尾。

控制面问不出来时的兜底启发式（`_is_unreachable`），按可靠性排序：

1. `httpx.TransportError` / `ConnectionError` / `TimeoutError`
2. 异常上挂了 `sdk_debug` / `sdk_payload` 属性 —— `env_sdk` 的
   `controlplane._attach_wait_failure_debug` 只在 attach / 等待路由就绪失败时
   挂这两个属性，但抛的是**裸 `RuntimeError`**，没有专用异常类型
3. HTTP 401 / 403 / 404 / 410
4. 文本特征（`"sandbox was closed"`、`"sandbox_not_ready"` 等）

## 异常层次

```text
LogExportError                 导出失败的基类
└─ SandboxUnreachable          连不上，但控制面没给确定答案 → 可重试
   └─ SandboxClosed            控制面确认 closed/deleted → 不重试，直接收尾
      └─ SandboxNotFound       控制面 404，查无此沙箱
```

`SandboxNotFound` 单独分出来，是因为它和「关闭」的修法不同：一个是 `env_id`
写错、或 `env_id` 不属于当前 `--config` 指向的集群，另一个是你来晚了。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功。**包括** `--follow` 期间沙箱正常关闭 |
| 1 | 其它失败（探测脚本出错、本地 IO、参数非法） |
| 2 | 连不上沙箱，且控制面未能确认状态 |
| 3 | 控制面确认沙箱已关闭，且一个字节都没导出 —— 来晚了 |
| 4 | 控制面查无此沙箱 |
| 130 | `Ctrl+C` |

典型的「来晚了」输出：

```text
sandbox zx-xxxxxxxx is already closed (status=closed close_reason=manual_close
closed_at=2026-11-09T01:58:44.169586+00:00). 日志已随沙箱销毁，请改用任务日志或 OBS 交付物。
```

## 事件流

`progress` 回调（同步或异步均可）依次收到：

```text
start  ->  chunk ...  ->  done
```

| 事件 | 字段 |
|---|---|
| `start` | `env_id` `log` `remote_path` `local_path` `follow` |
| `chunk` | `bytes`（本次写入字节数）、`local_size`（累计） |
| `done` | `env_id` `log` `local_path` `bytes` `reason`，非 `complete` 时附 `detail` |

`done.reason` 取值：

- `complete` —— 快照读完（非 follow）
- `sandbox_closed` —— 控制面确认沙箱已关闭
- `sandbox_gone` —— 连不上且控制面未确认，由启发式判定
- `stopped` —— 外部 `stop_event` 置位

## 快照边界

第一次成功探测拿到的文件大小被记为 `snapshot_end`，`--follow` 之前会把
`[0, snapshot_end)` **全部**写完。所以首个快照是完整文件而不是一个 tail 窗口，
哪怕日志正在飞速增长也是如此。越过这条边界后才开始只读 `next_byte` 之后的
新增内容。

远端文件变小（截断或重建）时，探测返回的 `start` 会小于本地游标，此时本地镜像
清零重新同步，避免新旧日志拼在一起。

## Python 调用

```python
import asyncio
from sandbox_log_tap import export_sandbox_log


async def export():
    path = await export_sandbox_log(
        config_path="config.yaml",
        env_id="zx-xxxxxxxx",
        log_name="run.log",
        follow=False,
    )
    print(path)


asyncio.run(export())
```

| 函数 | 用途 |
|---|---|
| `export_sandbox_log(...)` | 一站式：查状态 → attach → 导出 |
| `export_with_client(client, settings, ...)` | 已有 client 时直接导出；`state_check` 为 `None` 时退回启发式 |
| `resolve_export_settings(config, ...)` | 只解析路径，不发请求 |
| `fetch_sandbox_state(env_id, config)` | 单独查沙箱状态，永不抛异常 |

未来的后端服务可以直接调用 `export_sandbox_log()`，并将同一
`(env_id, log_name)` 的多个请求合并为一个导出任务。做「点击查看日志」按钮时，
用 `progress` 回调把 `chunk` 转发给 SSE / WebSocket 即可；只需要「拉到本地」的话，
消费到 `done` 取 `local_path`。

## 安全边界

- 远端路径由 `--config` 推导，只能命中该配置解析出的 `run.log` / `gateway.log`，
  没有任意路径入口。
- `env_id` 必须匹配 `^[A-Za-z0-9_.-]+$`。
- `--log` 只接受 `run.log` / `gateway.log` 两个字面值。
- 本地只写 `log_exports/` 下面。
- 永不调用 `client.close()`，不会把别人正在跑的沙箱关掉。

## 与 Hive 的边界

`hive.py` 不再 import、start、stop 或 register 日志服务。日志导出器的 SDK
连接、命令请求和本地文件写入都在调用方进程中完成，不占用 Hive 的
client 连接池、事件循环和日志带宽。

## 已知限制

**任务结束后就读不到了。** 沙箱一关，日志随之销毁。想留存失败任务的日志，
要么在任务运行期间 `--follow`，要么改用 `outputs/<config>/logs/task-N.log`
和 OBS 上的交付物。

**导出失败会覆盖上一次的结果。** 本地文件是 `open("wb")` 打开的，进入即截断。
如果这次导出中途失败，上一次成功导出的那份就被破坏了（零字节文件会被删除，
部分写入的不会）。同理，两个进程同时导出同一个 `(env_id, log_name)` 会互相
写坏 —— 它们落在同一个路径上。修法是写临时文件再 `os.replace` 原子替换，
尚未实现。
