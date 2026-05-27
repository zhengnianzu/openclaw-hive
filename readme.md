# OpenClaw V4 Hive

OpenClaw 分布式任务编排系统，用于在 Kubernetes 沙箱环境中批量运行 OpenClaw AI Agent 自动化任务。支持多任务并行、自动打包部署、OBS 云存储集成和完整的任务生命周期管理。

## 快速开始

### 环境要求

- Python 3.12+（推荐使用 `conda activate openclaw`）
- Kubernetes 集群访问权限
- OBS 云存储配置（obsutil 已安装）
- `execution_client` 模块

### 快速上手

```bash
# 1. 从模板创建任务配置
cp config_template.yaml task_configs/config_1.yaml
# 编辑必改项：user_id、traj_save_path、task_output_path 等（模板中有 ⚠️ 标注）
vim task_configs/config_1.yaml

# 2. 启动
bash hive.sh --start --config task_configs/config_1.yaml
```

### 常用命令

```bash
bash hive.sh --start                              # 自动发现所有的 task_configs/config_*.yaml 并全部启动
bash hive.sh --start --config task_configs/config_1.yaml  # 启动单个任务
bash hive.sh --start --config task_configs/config_1.yaml --config task_configs/config_1.yaml # 启动多个任务
bash hive.sh --status                             # 查看所有任务运行状态
bash hive.sh --stop --config task_configs/config_1.yaml   # 停止指定任务
bash hive.sh --stop-all                           # 停止所有任务
bash hive.sh --clear --config task_configs/config_1.yaml  # 清理 K8s Pod
```

## 项目结构

```
openclaw_v4_hive/
├── hive.sh                  # 服务管理脚本（入口）
├── hive.py                  # 主任务编排引擎
├── run_clear.py               # Pod 清理工具
├── config_template.yaml       # 配置模板（⚠️ 标注了必改项，复制到 task_configs/ 使用）
├── requirements.txt           # Python 依赖
├── task_configs/              # 任务配置目录（每个 config_*.yaml 对应一个独立任务进程）
│   ├── config_1.yaml
│   ├── config_2.yaml
│   └── ...
├── uploads/                   # 上传到沙箱的文件
│   ├── openclaw-task.tar      # 任务代码包（启动时自动从 ../openclaw-task 打包）
│   ├── openclaw.json          # OpenClaw Gateway 配置
│   ├── user_proxy_model.json  # 模型代理配置
│   └── configs/               # 任务输入配置（JSON 格式，每个文件对应一个子任务）
├── logs/                      # 日志和 PID 文件
│   ├── nohup_config_1.log
│   ├── nohup_config_1.pid
│   └── ...
├── outputs/                   # 任务输出
│   ├── complete.jsonl         # 已完成任务记录
│   └── failed.jsonl           # 失败任务记录
├── downloads/                 # OBS 下载缓存
└── execution_client/          # 执行客户端库
```

## 执行流程

```
hive.sh --start
    │
    ├─ 1. conda activate openclaw
    ├─ 2. tar 打包 ../openclaw-task → uploads/openclaw-task.tar
    ├─ 3. 发现 task_configs/config_*.yaml
    └─ 4. 每个 config 启动独立的 python hive.py 后台进程
              │
              ├─ 扫描 uploads/configs/ 获取子任务列表
              ├─ 启动 N 个并发 Worker
              └─ 每个 Worker 循环执行：
                    ├─ 创建 K8s Pod
                    ├─ 上传并解压 openclaw-task.tar
                    ├─ 从 OBS 下载 skills / user_profile / agents
                    ├─ 上传子任务配置到沙箱
                    ├─ 启动 OpenClaw Gateway
                    ├─ 执行 openclaw_automation.py
                    ├─ 上传执行轨迹到 OBS
                    └─ 记录完成/失败状态
```

## config.yaml 配置详解

配置文件分为 5 个核心段落：

---

### remote_server — 远程执行服务连接（注意修改user_id）

```yaml
remote_server:
  schema: http                # 协议
  host: 127.0.0.1             # 执行服务地址
  port: 8080                  # 执行服务端口
  project_id: openclaw        # 项目标识
  user_id: zx0522claude47     # 用户标识（用于 Pod 隔离和清理）
  url_path: /v1/env/gem       # API 路径
  http_client:
    max_retry_count: 1        # HTTP 最大重试次数
    connect_timeout: 10.0     # 连接超时（秒）
    read_timeout: 180.0       # 读取超时（秒）
    write_timeout: 60.0       # 写入超时（秒）
```

> `user_id` 很重要，`--clear` 和 `--stop` 时根据它过滤属于你的 Pod。不同人/不同批次建议用不同的 user_id。

---

### s3 — OBS 云存储配置（默认）

```yaml
s3:
  s3_uploader_script: /home/ma-user/obsutil/obsutil   # 沙箱内 obsutil 路径
  s3_download_script: /home/ma-user/obsutil/obsutil   # 沙箱内 obsutil 路径
  bucket_name: "obs://rl-agentdata"                   # OBS 桶名
```

> 这里配置的是**沙箱内部**的 obsutil 路径，不是本地路径。

---

### env_make — K8s Pod 环境配置(注意`image_name`版本)

```yaml
env_make:
  image_name: swr.cn-southwest-2.myhuaweicloud.com/rl_team/openclaw:0.0.8  # 沙箱镜像
  wait_for_ready: true        # 等待 Pod Ready
  wait_timeout: 300           # Pod 启动超时（秒）
  env_id: null                # 运行时自动生成，无需手动填
  runtime_type: k8s           # 运行时类型: k8s | docker | local
  rabbitmq:
    is_use: false             # 是否通过 RabbitMQ 调度 Pod 创建
    url: "amqp://guest:guest@x.x.x.x:5672/"
    queue: "test.direct.reply.queue"
    priority: 10
  args:
    resources:                # Pod 资源限制
      cpu_request: "1"        # CPU 请求
      cpu_limit: "2"          # CPU 上限
      memory_request: "5Gi"   # 内存请求
      memory_limit: "8Gi"     # 内存上限
    env:                      # 注入到沙箱的环境变量
      - name: PIP_INDEX_URL
        value: "https://mirrors.huaweicloud.com/repository/pypi/simple"
      - name: PIP_TRUSTED_HOST
        value: "mirrors.huaweicloud.com"
      - name: NODE_TLS_REJECT_UNAUTHORIZED
        value: "0"
      - name: SERPER_API_KEY
        value: your_serper_api_key
```

> 调整 `cpu_limit` 和 `memory_limit` 时注意集群配额。并发 100 个 Pod × 2 CPU = 需要 200 核。

---

### run_config — 任务运行参数

```yaml
run_config:
  start_index: 0              # 从第几个子任务开始执行（跳过前 N 个）
  total_num: 0                # 执行多少个子任务，0 = 不限制（全部执行）
  concurrent_num: 100         # 并发 Worker 数（即同时运行的 Pod 数）
```
> 调整 `cluade-opus` 一般是100-150，和 `glm` 一般是100

#### run_config.sandbox — 沙箱运行时配置

```yaml
  sandbox:
    result_log: "run.log"                                      # 沙箱内执行日志文件名
    openclaw_local_config_file: "uploads/openclaw.json"        # 本地 Gateway 配置路径
    user_proxy_model_local_file: "uploads/user_proxy_model.json"  # 本地模型代理配置路径
    openclaw_start_timeout: 10 # Gateway 启动等待时间（秒），镜像>0.0.6建议 ≥60（如80）
```

#### run_config.task — 任务路径配置

```yaml
  task:
    task_input_path: "uploads/configs_1"     # 子任务 JSON 配置目录
    task_output_path: "outputs/configs_1"    # 输出目录
    task_complete_record: "complete.jsonl"   # 完成记录文件
    task_failed_record: "failed.jsonl"       # 失败记录文件
    task_download_path: "downloads/configs_1" # OBS 下载缓存目录
    main_code_tar: "uploads/openclaw-task.tar"  # 任务代码包路径
    main_python_file: "openclaw_automation.py"  # 沙箱内执行的入口脚本
```
> `task_output_path` 支持子目录（如 `"outputs/config_1"`），脚本会自动 `mkdir -p` 创建。多任务并行时建议每个 config 用不同的输出目录，避免 `complete.jsonl` 互相覆盖。

#### run_config.obs — OBS 路径配置

```yaml
  obs:
    download_timeout: 1200    # OBS 下载超时（秒）
    upload_timeout: 900       # OBS 上传超时（秒）
    traj_save_bucket: "obs://rl-agentdata"  # 轨迹上传目标桶
    traj_save_path: "openclaw_trajs/traj_0522_webconfig"  # 轨迹上传路径（末尾不加 /）
    skill_download_path: "skills/260325/skill_localize/skills_library"  # 技能库 OBS 路径
    user_config_download_path: "task_data/260520/web_configs"  # 子任务配置 OBS 路径（放置为空）
    user_profile_download_path:   # 用户画像 OBS 路径（留空则跳过）
    agents_download_path: "task_data/260413_noenv/noenv_configs/agents"  # Agent 配置 OBS 路径
    default_skills:               # 默认技能列表，无论子任务 skills 是否为空都会加载
      - "find-skills"
```

**OBS 路径字段详解：**

| 字段 | 留空时的行为 | 非空时的行为 | 沙箱内目标路径 |
|------|-------------|-------------|---------------|
| `skill_download_path` | 看子任务 JSON 的 `agents[].skills` 列表，列表为空则跳过 | 从 `obs://rl-agentdata/{skill_download_path}/{skill_name}/` 下载每个 skill | `/home/ma-user/.openclaw/skills/` |
| `user_config_download_path` | **使用本地 `uploads/configs/` 目录中已有的 JSON 文件**作为子任务列表 | 从 OBS 下载到 `downloads/` 缓存，再复制到 `uploads/configs/`（覆盖本地文件） | 本地目录，不上传沙箱 |
| `user_profile_download_path` | **跳过**，不下载用户画像。沙箱内无此数据，任务 JSON 中若引用了 `user_dir.path` 会报错 | 拼接子任务 JSON 中 `input_dir.user_dir.path` 作为子路径，从 OBS 下载完整用户画像目录 | `/home/ma-user/workspace/openclaw-task/{user_dir}/` |
| `agents_download_path` | **跳过**，不下载 Agent 配置。沙箱内无预置 Agent 会话数据 | 将整个 OBS 目录下载到沙箱代码目录下（保持目录结构） | `/home/ma-user/workspace/openclaw-task/` |
| `default_skills` | 列表为空则不额外加载任何 skill | **无论子任务 JSON 的 `agents[0].skills` 是否为空**，都会下载列表中的 skill（与任务 skills 合并去重） | `/home/ma-user/.openclaw/skills/` |

**补充说明：**

- `default_skills`：内嵌的默认技能列表。即使子任务 JSON 中 `agents[0].skills` 为空，这些 skill 也会被下载到沙箱。适用于全局必备的工具型 skill（如 `find-skills`）。与任务指定的 skills 自动合并去重。
- `skill_download_path`：即使配置了路径，实际下载哪些 skill 由子任务 JSON 中 `agents[0].skills` 列表 + `default_skills` 合并后决定。两者都为空则跳过。
- `user_config_download_path`：这是**本地编排层**的配置，决定子任务列表从哪里来。留空 = 手动把 JSON 放到 `uploads/configs/`；非空 = 从 OBS 自动拉取。下载有本地缓存（`downloads/` 目录存在则跳过重复下载）。
- `user_profile_download_path`：留空时如果子任务 JSON 的 `input_dir.user_dir.path` 有值，会触发 RuntimeError。所以如果你的任务需要用户画像，这个字段必须配置。
- `agents_download_path`：留空时只打印 warning 日志，不影响任务继续执行。适用于不需要预置 Agent 会话的场景。
- `traj_save_path`：末尾**不要**加 `/`，否则 OBS 路径会出现 `//`。

---

## 子任务配置格式

放在 `uploads/configs/` 下的 JSON 文件，每个文件对应一个独立的沙箱执行单元：

**注意：**
- `agents[].model` 设为 `null` 使用 Gateway 默认模型，或填写 `openclaw.json` 中已注册的模型 ID
- `api_key` 必须与 `openclaw.json` 中 `gateway.auth.token` 一致
- `agents[].skills` 列表中的名称对应 OBS `skill_download_path` 下的子目录

---

## 多任务并行

每个 `task_configs/config_*.yaml` 启动一个独立进程，互不干扰：

```bash
# 场景：同时跑 3 批不同配置的任务
task_configs/
├── config_claude.yaml      # 用 claude 模型，并发 100
├── config_gemini.yaml      # 用 gemini 模型，并发 50
└── config_rerun.yaml       # 重跑失败任务，并发 20
```

每个 config 可以有不同的 `user_id`、`concurrent_num`、`image_name`、OBS 路径等。

---

## 常见操作

### 重跑失败任务

```bash
python hive.py --config task_configs/config_1.yaml --failed
```

会读取 `outputs/failed.jsonl`，只重跑其中记录的任务。

### 跳过前 N 个任务

```yaml
run_config:
  start_index: 50    # 从第 51 个开始
  total_num: 100     # 只跑 100 个
```

### 清理残留 Pod

```bash
bash hive.sh --clear --config task_configs/config_1.yaml
# 或直接用 Python
python run_clear.py --config task_configs/config_1.yaml --delete
```

### 重置任务（跑错后重新开始）

当任务跑错需要重启时，使用 `--reset` 一键清理旧数据：

```bash
# 重置指定任务
bash hive.sh --reset --config config_tasks/config_1.yaml

# 重置多个
bash hive.sh --reset --config config_tasks/config_1.yaml --config config_tasks/config_2.yaml

# 不指定 config 则自动发现所有 config_*.yaml 并重置
bash hive.sh --reset
```

`--reset` 会依次执行：
1. 停止运行中的进程
2. 删除 `logs/nohup_<name>.log` 和 `.pid`
3. 删除 `env_info.db`
4. 清空 `outputs/<task_output_path>/`（从 config.yaml 中读取路径）
5. 清空 `downloads/<task_download_path>/`（OBS 缓存，下次重新拉取）

执行前会要求确认。重置完成后用 `--start` 重新启动即可。

---

## 运维操作

### 查看实时日志

```bash
tail -f logs/nohup_config_1.log
```

### 查看沙箱 Pod 状态

```bash
kubectl get pods | grep 'zx0522claude47' (对应user_id)
```

### 进入沙箱调试

```bash
kubectl exec -it <pod-name> -- /bin/bash
# 例如：
kubectl exec -it sandbox-k8s-podzx0522claude47feb742330cd24e6c -- /bin/bash
```

### 查看任务完成进度

```bash
wc -l outputs/complete.jsonl
wc -l outputs/failed.jsonl
```

### 手动清理沙箱 Pod

当脚本异常退出后 Pod 可能残留，手动批量清理：

```bash
kubectl get pods | grep 'zx0522claude47' | awk '{print $1}' | xargs -r kubectl delete pod
```

> 将 `zx0522claude47` 替换为你 config.yaml 中的 `user_id`。

---

## 镜像版本说明

| 镜像版本 | 对应 OpenClaw 版本 | 备注 |
|---------|-------------------|------|
| 0.0.6   | 2026.3.2          | 稳定版本 |
| 0.0.8   | 2026.4.26         | Gateway 启动较慢，`openclaw_start_timeout` 建议 ≥60 |

镜像地址格式：`swr.cn-southwest-2.myhuaweicloud.com/rl_team/openclaw:<version>`

---

## 依赖安装

```bash
pip install -r requirements.txt
```

主要依赖：aiofiles、omegaconf、httpx、httpx_sse、pydantic、loguru、aio_pika
