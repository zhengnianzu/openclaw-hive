# OpenClaw Hive Platform

基于 FastAPI + Vue3 的任务管理平台，替代 SSH + CLI 的工作方式，提供可视化的多任务实例管理、OBS 目录浏览、实时日志查看等功能。

## 项目结构

```
platform/
├── run.sh                          # 一键启动脚本
├── main.py                         # FastAPI 入口
├── requirements.txt                # Python 依赖
├── api/
│   ├── core/
│   │   ├── config.py               # 全局配置 (Settings)
│   │   ├── database.py             # SQLite 数据库 (users + task_instances)
│   │   └── security.py             # JWT 认证 + 密码哈希
│   ├── models/
│   │   ├── auth.py                 # 用户模型 (注册/登录/Token)
│   │   └── instance.py             # 实例模型 (CRUD/OBS)
│   ├── routers/
│   │   ├── auth.py                 # 注册/登录 API
│   │   ├── instances.py            # 多实例管理 (创建/启动/停止/重跑/删除/统计)
│   │   ├── obs.py                  # OBS 目录浏览 + 搜索
│   │   └── logs.py                 # 实时日志 WebSocket + 历史日志 + OBS日志下载
│   └── services/
├── frontend/
│   ├── package.json
│   ├── vite.config.js
│   ├── index.html
│   └── src/
│       ├── main.js
│       ├── App.vue
│       ├── api/index.js            # Axios 封装 + JWT 拦截
│       ├── router/index.js         # 路由 + 登录守卫
│       └── views/
│           ├── Login.vue           # 登录/注册页
│           ├── Layout.vue          # 侧边栏布局
│           ├── Dashboard.vue       # 多实例列表 + 统计 + 操作
│           ├── CreateInstance.vue   # 新建任务 + OBS目录选择器
│           ├── InstanceDetail.vue   # 实例详情 + 错误分布 + OBS历史日志
│           └── LogViewer.vue       # 实时/静态/错误日志 + 关键字过滤
```

## 启动方式

```bash
cd openclaw-hive/platform

# 1. 配置环境变量
cp .env.example .env
# 编辑 .env，按需修改路径和密码

# 2. 安装依赖
./run.sh install

# 3. 生产部署（推荐）
./run.sh build                      # 构建前端
./run.sh start                      # 后台启动，一个端口 :8000 搞定
./run.sh stop                       # 停止

# 4. 开发模式（需要两个终端）
./run.sh dev                        # 终端1：后端 :8000（热重载）
cd frontend && npm run dev          # 终端2：前端 :3000（热重载）
```

首次启动会自动根据 `.env` 中的 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 创建管理员账号。

## 环境变量配置

复制 `.env.example` 为 `.env` 并按需修改：

```bash
# ===== 路径配置 =====
OBSUTIL_PATH=/home/nianzuzheng/project/openclaw-hive/platform/obsutil/obsutil
OBS_BUCKET=obs://rl-agentdata
CONFIG_TEMPLATE=/home/nianzuzheng/project/openclaw-hive/config.yaml
HIVE_ROOT=/home/nianzuzheng/project/openclaw-hive

# ===== 安全配置 =====
SECRET_KEY=change-me-to-a-random-string     # JWT 签名密钥，生产环境务必修改
ACCESS_TOKEN_EXPIRE_MINUTES=1440            # Token 有效期（分钟），默认 24 小时

# ===== 数据库 =====
DB_PATH=/home/nianzuzheng/project/openclaw-hive/platform/platform.db

# ===== 初始管理员账号（首次启动自动创建） =====
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin123
```

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `OBSUTIL_PATH` | `{HIVE_ROOT}/platform/obsutil/obsutil` | obsutil 可执行文件路径 |
| `OBS_BUCKET` | `obs://rl-agentdata` | OBS 桶地址 |
| `CONFIG_TEMPLATE` | `{HIVE_ROOT}/config.yaml` | 创建任务实例的 config 模板 |
| `HIVE_ROOT` | 自动检测 | openclaw-hive 项目根目录 |
| `SECRET_KEY` | 随机生成 | JWT 签名密钥 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | 1440 | Token 有效期（分钟） |
| `DB_PATH` | `{HIVE_ROOT}/platform/platform.db` | SQLite 数据库路径 |
| `ADMIN_USERNAME` | `admin` | 初始管理员用户名 |
| `ADMIN_PASSWORD` | `admin123` | 初始管理员密码 |

## 核心功能

| 功能 | 说明 |
|------|------|
| 登录认证 | JWT Token + 注册/登录 |
| 多任务实例管理 | task_instances 表，每个 config.yaml 独立一个实例 |
| 可视化 K8s 沙箱运行 | Dashboard 实例列表 + 进度条 + 完成/失败/运行中统计 |
| 自动生成 config | CreateInstance 表单，基于 config.yaml 模板生成 |
| OBS 目录浏览选择 | OBS Browser 弹窗，点击选择回填到表单 |
| 实时日志 | WebSocket tail -f nohup.log |
| 查看历史/OBS日志 | OBS 日志列表 + 在线查看 + 下载 |
| 启动/停止/重跑 | 通过 API 调用 hive.py / run_clear.py |

## API 接口

### 认证

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/register` | 注册用户 |
| POST | `/api/auth/login` | 登录获取 Token |

### 任务实例

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/instances` | 列出所有实例 |
| POST | `/api/instances` | 创建实例（自动生成 config） |
| GET | `/api/instances/{id}` | 获取实例详情 |
| POST | `/api/instances/{id}/start` | 启动实例 |
| POST | `/api/instances/{id}/stop` | 停止实例 |
| POST | `/api/instances/{id}/retry-failed` | 重跑失败任务 |
| GET | `/api/instances/{id}/overview` | 获取实例统计（完成/失败/错误分布） |
| DELETE | `/api/instances/{id}` | 删除实例 |

### OBS 浏览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/obs/list?path=obs://...` | 列出 OBS 目录内容 |
| GET | `/api/obs/search?path=...&keyword=...` | 搜索 OBS 文件 |

### 日志

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/logs/{id}/main?tail=200` | 获取主进程日志（最后 N 行） |
| GET | `/api/logs/{id}/clean?tail=100` | 获取错误日志 |
| WS | `/api/logs/ws/{id}` | WebSocket 实时日志流 |
| GET | `/api/logs/{id}/obs-logs` | 列出 OBS 上的日志文件 |
| GET | `/api/logs/{id}/obs-view?file_path=...` | 在线查看 OBS 日志内容 |
| GET | `/api/logs/{id}/obs-download?file_path=...` | 下载 OBS 日志文件 |
