# Hive 按需沙箱日志读取插件

## 目标

`sandbox_log_tap.py` 为运行中的 Hive 任务提供按需日志读取能力：

- 未发起读取请求时，不向沙箱发送任何日志请求。
- 复用任务已经持有的 `ExecutionClient`，不会重新创建沙箱。
- 只允许读取当前任务配置的 `run.log` 和 `gateway.log`。
- 支持一次性快照和显式 follow。
- 日志同时流式返回给调用方，并写入 Hive 输出目录下的本地文件。

## 使用 `config.yaml` 的完整操作流程

当前 `config.yaml` 中配置了：

```yaml
run_config:
  task:
    task_output_path: outputs
```

`hive.py` 会在输出目录后追加配置文件名。因此执行
`python hive.py --config config.yaml` 时，实际输出目录是 `outputs/config`，
日志控制文件是：

```text
outputs/config/.sandbox-log-tap.json
```

### 1. 启动 Hive

在终端 A 中执行：

```powershell
python hive.py --config config.yaml
```

等待 Hive 输出：

```text
Sandbox log tap ready: ...\outputs\config\.sandbox-log-tap.json
```

控制文件生成只表示日志控制服务已经启动。具体任务还需要等沙箱创建成功并注册
`ExecutionClient` 后，才会出现在活动任务列表中。

### 2. 查看当前活动沙箱

在终端 B 中执行：

```powershell
python sandbox_log_tap.py `
  --control-file ".\outputs\config\.sandbox-log-tap.json" `
  --list
```

返回示例：

```json
{
  "event": "tasks",
  "tasks": [
    {
      "env_id": "zx-abc123",
      "task_idx": 0,
      "config_name": "task-001.json",
      "logs": {
        "run": "/home/ma-user/workspace/workdir/run.log",
        "gateway": "/home/ma-user/workspace/workdir/gateway.log"
      }
    }
  ]
}
```

`--list` 只读取 Hive 进程内的活动任务注册表，不会向沙箱发送日志请求。

### 3. 一次性加载当前 `run.log`

```powershell
python sandbox_log_tap.py `
  --control-file ".\outputs\config\.sandbox-log-tap.json" `
  --task-idx 0 `
  --log run
```

该命令会复用任务 0 已有的 `ExecutionClient`，读取调用时已经存在的完整
`run.log`，追到当前 EOF 后立即停止。日志同时输出到终端并保存到：

```text
outputs/config/log_exports/task-0/run.log
```

也可以使用活动任务的 `env_id`：

```powershell
python sandbox_log_tap.py `
  --control-file ".\outputs\config\.sandbox-log-tap.json" `
  --env-id "zx-abc123" `
  --log run
```

### 4. 一次性加载当前 `gateway.log`

```powershell
python sandbox_log_tap.py `
  --control-file ".\outputs\config\.sandbox-log-tap.json" `
  --task-idx 0 `
  --log gateway
```

本地保存位置：

```text
outputs/config/log_exports/task-0/gateway.log
```

### 5. 持续更新到任务结束

```powershell
python sandbox_log_tap.py `
  --control-file ".\outputs\config\.sandbox-log-tap.json" `
  --task-idx 0 `
  --log run `
  --follow
```

follow 会先输出调用时已经存在的完整日志，再持续读取新增内容，直到任务结束。
也可以按 `Ctrl+C` 提前停止。只有 follow 命令存活期间才会发起增量读取请求。

### 使用限制

- 远程日志读取只对仍处于活动状态的任务沙箱有效。
- 任务结束时会先注销日志连接，再关闭 `ExecutionClient`。
- 全部 worker 结束后，`.sandbox-log-tap.json` 会保留并标记为 `stopped`。
- 下次运行 Hive 时会原位覆盖控制文件，写入新的端口、token 和 `running` 状态。
- 已经写入 `log_exports` 的本地日志会继续保留。
- 已结束任务如未提前导出，应改用任务日志或 OBS 交付物查看。

## 工作方式

Hive 在运行 worker 前启动一个仅监听 `127.0.0.1` 的控制服务，并在输出目录写入：

```text
<task_output_path>/.sandbox-log-tap.json
```

控制文件包含服务状态、临时端口和随机 token，权限会尽量设置为仅当前用户可读。
Hive 退出时文件保留并将状态改为 `stopped`；重跑时直接覆盖为新的 `running`
控制信息。

任务沙箱创建成功后，Hive 把以下内容登记到进程内注册表：

- `env_id`
- `task_idx`
- 当前任务已有的 `ExecutionClient`
- `run.log` 和 `gateway.log` 的沙箱绝对路径

任务结束前先从注册表注销，再关闭 `ExecutionClient`。

## 命令行用法

下面的控制文件路径以实例的实际输出目录为准。

### 查看当前活动任务

```bash
python sandbox_log_tap.py \
  --control-file /path/to/outputs/config/.sandbox-log-tap.json \
  --list
```

### 点击式加载当前 run.log

```bash
python sandbox_log_tap.py \
  --control-file /path/to/outputs/config/.sandbox-log-tap.json \
  --task-idx 7 \
  --log run \
  --sandbox-path /root/sandbox-log/run.log
```

这会读取调用时已经存在的日志，追到当前 EOF 后立即停止。日志保存在：

```text
<task_output_path>/log_exports/task-7/run.log
```

### 按需跟随 gateway.log

```bash
python sandbox_log_tap.py \
  --control-file /path/to/outputs/config/.sandbox-log-tap.json \
  --task-idx 7 \
  --log gateway \
  --follow
```

该模式会先加载已有日志，再持续更新到任务结束。终止命令后，Hive 会停止该读取循环。

`--sandbox-path` 是可选参数；传入时必须与该任务注册的 `run.log` 或
`gateway.log` 路径一致，不能借此读取其它沙箱文件。

也可以用 `--env-id` 代替 `--task-idx`：

```bash
python sandbox_log_tap.py \
  --control-file /path/to/.sandbox-log-tap.json \
  --env-id zx-abc123 \
  --log run
```

## 按钮后端调用

后端按钮无需直接操作 `ExecutionClient`，调用 `iter_log_request()` 即可：

```python
from sandbox_log_tap import iter_log_request


async def load_run_log(control_file: str, task_idx: int):
    async for event in iter_log_request(
        control_file,
        task_idx=task_idx,
        log="run",
        follow=False,
    ):
        if event["event"] == "chunk":
            # 把 event["text"] 转发给按钮对应的 HTTP/SSE/WebSocket 响应。
            yield event["text"]
        elif event["event"] == "error":
            raise RuntimeError(event["error"])
```

如果按钮只负责“拉取到本地”，消费到 `done` 即可。`done.local_path` 是最终文件路径。

## 事件格式

一次 fetch 会依次返回：

```text
start -> chunk... -> done
```

关键字段：

- `start.local_path`：本地输出文件。
- `chunk.text`：本次新增文本。
- `done.reason`：`complete` 或 `task_closing`。
- `error.error`：失败原因。

## 带宽控制

- 空闲时：零次远端日志请求。
- snapshot：每次最多读取 256 KiB，未到 EOF 时立即读取下一块，到 EOF 后结束。
- follow：有内容时继续读取；无内容时在本地按 2、4、8、10 秒退避。
- 字节游标仅在服务内部用于增量读取，不暴露给按钮或 CLI。
- 每个任务使用独立读锁，防止多个按钮同时对同一沙箱并发读取日志。
- 沙箱文件被截断或重建时，本地镜像也会清空并从头同步。

## 安全边界

- 控制服务只监听 `127.0.0.1`。
- 请求必须携带控制文件中的随机 token。
- 远端路径只能命中任务注册时记录的 `run.log` 或 `gateway.log`。
- 本地文件只能写入 `<task_output_path>/log_exports/`。
- 日志服务启动或关闭失败只会产生 warning，不会中断 Hive 主任务。
