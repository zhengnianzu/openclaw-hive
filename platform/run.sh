#!/bin/bash
# OpenClaw Hive Platform 启动脚本

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"


ACTION="${1:-help}"

# 两词写法 `shallow logs [tracker|local|obs] [-f]` → 归一到 shallow-logs。
# 用户习惯敲 `./run.sh shallow logs -f`（对标 docker/journalctl 的风格），
# 这里把子命令左移一位，让下面的 case 只认 shallow-logs 一个入口。
if [ "$ACTION" = "shallow" ] && [ "${2:-}" = "logs" ]; then
    ACTION="shallow-logs"
    # 丢掉 "shallow logs" 两个词，余下的（tracker/local/obs、-f）前移给 $2 起
    shift 2 2>/dev/null || shift $#
    set -- "shallow-logs" "$@"
fi

HOST="${2:-0.0.0.0}"
PORT="${3:-8087}"
NGINX_PORT="${4:-80}"

# 解释器统一固定，避免 install 与 start 装/跑在两套环境上。
# 裸 `pip install` + 裸 `uvicorn` 各自按 PATH 解析：换 shell（sudo、systemd、cron、
# 或装了用户级 ~/.local/bin）时可能 pip 装进 anaconda、uvicorn 却用 /usr/bin/python3，
# 于是启动直接 ModuleNotFoundError: No module named 'bcrypt'。
# 可用 PYTHON=... 覆盖，默认取当前 shell 的 python3。
PYTHON="${PYTHON:-$(command -v python3)}"

# 提醒：激活的 venv 与即将使用的解释器不一致时才会出问题——
# 依赖装进 A、服务用 B 跑（典型症状：装完仍报 No module named 'bcrypt'）。
# 注意不能按「venv 是否在本仓库内」判断：本机常态就是 VIRTUAL_ENV=/root/.venv
# （仓库外的独立 venv），那样判断会每次启动都误报。
_active_venv="${VIRTUAL_ENV:-}"
if [ -n "$_active_venv" ]; then
    _py_dir="$(cd "$(dirname "$PYTHON")" 2>/dev/null && pwd)"
    _venv_bin="$(cd "$_active_venv/bin" 2>/dev/null && pwd)"
    if [ -n "$_py_dir" ] && [ -n "$_venv_bin" ] && [ "$_py_dir" != "$_venv_bin" ]; then
        echo "警告: 激活的虚拟环境与将使用的解释器不一致:"
        echo "      VIRTUAL_ENV=$_active_venv"
        echo "      将使用 python3 = $PYTHON"
        echo "      依赖会装错地方。如非本意，请先 deactivate，或用 PYTHON=/path/to/python3 指定解释器。"
    fi
fi

# 优先用 `$PYTHON -m` 调用，确保 pip/uvicorn 一定落在同一个解释器里。
# uv 建的 venv 默认不带 pip（没有 ensurepip），此时回退到 uv pip。
if "$PYTHON" -m pip --version >/dev/null 2>&1; then
    PIP="$PYTHON -m pip"
elif command -v uv >/dev/null 2>&1; then
    PIP="uv pip install --python $PYTHON"
else
    PIP="$PYTHON -m pip"   # 保留原样，让报错信息自己说话
fi
UVICORN="$PYTHON -m uvicorn"

# 释放端口：上次启动的 uvicorn 若没被记录在 platform.pid（手动 nohup、PID 文件丢失、
# 或 stop 时只杀了父进程而 worker 仍持有监听），再次 start 会直接
# "ERROR: [Errno 98] Address already in use"。启动前统一清掉占用者。
# 先 TERM 给进程自己收尾的机会，1 秒后仍在则 KILL。
# 取监听某端口的 PID。
# **不能用 lsof**：lsof 会把全系统所有进程的 fd 都读一遍，本机有 30 个进程持有
# sfs_turbo 的 fd（单个最多 1.3 万个），而该挂载上还有卡住的 RPC，实测
# `lsof -ti tcp:8087` 30 秒都不返回 —— 于是 free_port 第一步就挂死，
# 表现为「run.sh 卡住不动」，跟端口占用毫无关系。
# `ss -lntp` 只读 netlink 的 socket 表，实测 0.5 秒内返回，且直接带 pid。
_port_pids() {
    local port="$1"
    ss -lntp 2>/dev/null | awk -v p=":$port" '$4 ~ p"$"' \
        | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
}

free_port() {
    local port="$1" pids
    pids=$(_port_pids "$port")
    if [ -z "$pids" ]; then
        return 0
    fi
    echo "端口 $port 被占用 (PID: $(echo $pids | tr '\n' ' '))，正在释放..."
    kill $pids 2>/dev/null
    for _ in $(seq 1 25); do
        sleep 0.2
        pids=$(_port_pids "$port")
        [ -z "$pids" ] && { echo "端口 $port 已释放"; return 0; }
    done

    # 走了 5 秒还没退，先看是不是 D 状态（卡在不可中断的内核 I/O，NFS/SFS 抖动时常见）。
    # 注意：D 状态**不代表 kill -9 无效** —— 实测卡在 rpc_wait_bit_killable 的进程
    # 收到 SIGKILL 后 4 秒内就退出了（那个等待点本身是可 kill 的）。
    # 所以这里照样发 KILL 并给足等待时间，只有确实杀不掉才报错。
    local stuck d
    for pid in $pids; do
        stuck=$(ps -o stat= -p "$pid" 2>/dev/null | tr -d ' ')
        case "$stuck" in
            D*) echo "提示: 端口 $port 的 PID $pid 处于 D 状态（疑似卡在 NFS I/O），尝试强杀..." ;;
        esac
    done
    kill -9 $pids 2>/dev/null
    for _ in $(seq 1 50); do
        sleep 0.2
        pids=$(_port_pids "$port")
        [ -z "$pids" ] && { echo "端口 $port 已释放"; return 0; }
    done

    echo "错误: 端口 $port 仍被占用 (PID: $(echo $pids | tr '\n' ' '))，SIGKILL 后 10 秒仍未退出"
    for pid in $pids; do
        echo "      PID $pid stat=$(ps -o stat= -p "$pid" 2>/dev/null | tr -d ' ') wchan=$(cat /proc/$pid/wchan 2>/dev/null)"
    done
    echo "      这类进程通常是父进程 uvicorn(master) 仍在向子进程转发信号。"
    echo "      请先杀 master：kill -9 \$(pgrep -f 'uvicorn main:app.*--port $port')"
    return 1
}

# 启动后健康检查：nohup 起来不代表服务可用。
# 历史上出现过「uvicorn 进程在、端口在 Listen，但 4 个 worker 全卡在 NFS D 状态，
# 监听队列 Recv-Q 打满，任何请求都超时」却没有任何报错的情况。
# 这里用 /api/health 确认真的能响应，失败时把日志尾部打出来并让脚本非 0 退出。
wait_healthy() {
    local port="$1" max_wait="${2:-40}" pidfile="platform.pid"
    local i code
    if ! command -v curl >/dev/null 2>&1; then
        echo "未找到 curl，跳过健康检查"
        return 0
    fi
    echo "健康检查: http://127.0.0.1:${port}/api/health"
    for ((i = 0; i < max_wait; i += 2)); do
        sleep 2
        code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://127.0.0.1:${port}/api/health" 2>/dev/null)
        if [ "$code" = "200" ]; then
            echo "服务健康检查通过"
            return 0
        fi
    done
    echo ""
    echo "错误: 服务在 ${max_wait}s 内没有通过健康检查（最后状态码: ${code:-无}）"
    local pid
    pid=$(cat "$pidfile" 2>/dev/null)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "      进程 $pid 仍在，但无法响应请求。当前状态:"
        ps -o pid,stat,wchan:28,cmd -p "$pid" 2>/dev/null | tail -2
        echo "      全部 8087 相关进程:"
        ps -eo pid,stat,wchan:28,args 2>/dev/null | grep -F -- "--port ${port}" | grep -v grep
        echo "      若 stat 是 D，说明卡在不可中断 I/O（NFS/SFS 抖动），kill 无效，需重启机器。"
    else
        echo "      进程已退出（pid 文件: ${pid:-无}）"
    fi
    echo "      --- platform.log 尾部 40 行 ---"
    [ -f platform.log ] && tail -n 40 platform.log
    return 1
}

# 输出分析 worker：浅层（分级/补全）+ 产物下载（沙箱交付物全量）两个独立进程
#  - 浅层: pid worker.pid / 日志 worker.log
#  - 产物: pid deep_worker.pid / 日志 deep_worker.log（沿用旧文件名，避免升级时残留进程失控）
# 注：原「深层轨迹/日志按需下载」已并入产物下载，两者落盘目录本来就是同一个。
# 停 worker：先 TERM 等 5 秒，仍在则 KILL。
# 不能 kill 完立刻删 pid 文件——进程没退出就删，下次 start 会再起一个，
# 两个进程同时消费同一张表（CAS 能防重复，但会白跑一轮 OBS）。
_stop_pidfile() {
    local pidfile="$1" name="$2"
    if [ ! -f "$pidfile" ]; then
        echo "$name 未运行"
        return 0
    fi
    local pid
    pid=$(cat "$pidfile" 2>/dev/null)
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$pidfile"
        echo "$name 未运行（已清理残留 pid 文件）"
        return 0
    fi
    kill "$pid" 2>/dev/null
    local i
    for i in 1 2 3 4 5; do
        sleep 1
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$pidfile"
            echo "$name 已停止 (PID $pid)"
            return 0
        fi
    done
    kill -9 "$pid" 2>/dev/null
    sleep 0.3
    rm -f "$pidfile"
    echo "$name 已强制停止 (PID $pid)"
}

start_worker() {
    if [ -f worker.pid ] && kill -0 "$(cat worker.pid)" 2>/dev/null; then
        echo "浅层 worker 已在运行 (PID: $(cat worker.pid))"
        return
    fi
    rm -f worker.pid          # 残留 pid 文件（进程已死）
    nohup $PYTHON offline/output_worker.py --shallow-only > worker.log 2>&1 &
    echo $! > worker.pid
    echo "浅层 worker 已启动 (PID: $(cat worker.pid))"
}

stop_worker() {
    _stop_pidfile worker.pid "浅层 worker"
}

start_deep_worker() {
    if [ -f deep_worker.pid ] && kill -0 "$(cat deep_worker.pid)" 2>/dev/null; then
        echo "产物下载 worker 已在运行 (PID: $(cat deep_worker.pid))"
        return
    fi
    rm -f deep_worker.pid
    nohup $PYTHON offline/output_worker.py --deep-only > deep_worker.log 2>&1 &
    echo $! > deep_worker.pid
    echo "产物下载 worker 已启动 (PID: $(cat deep_worker.pid))"
}

stop_deep_worker() {
    _stop_pidfile deep_worker.pid "产物下载 worker"
}

# ── 浅层分析架构（tracker + shallow-realtime + shallow-auto 三进程）──
# 浅层有两个**阶段**（都要做、顺序固定）：
#   阶段1 local：读本地 logs/task-N.log 判 DONE（写 task_records.task_done）
#   阶段2 obs  ：拉 OBS tsr 定级（写 task_records.traj_level），依赖阶段1 的 task_done
# 需要进程级隔离的不是阶段，而是**场景**：
#   shallow-realtime 只领用户点过的行（priority>=100），2s 一轮，秒级响应
#   shallow-auto     只领背景行（priority<100），按 id DESC 最新优先，全量同步
# 旧架构按阶段拆成 shallow-local/shallow-obs 两进程，两种场景混在一条队列里抢并发，
# 实测用户点的那批有 2.3 万行卡在阶段2（阶段2 只有 16 并发、要和 92 万背景行抢）。
# 合并成「一个进程内跑完两阶段」后，同一轮里阶段1 刚写出的 task_done 阶段2 立刻可用。
start_tracker() {
    if [ -f tracker.pid ] && kill -0 "$(cat tracker.pid)" 2>/dev/null; then
        echo "task_tracker 已在运行 (PID: $(cat tracker.pid))"
        return
    fi
    rm -f tracker.pid
    nohup $PYTHON offline/task_tracker.py > tracker.log 2>&1 &
    echo $! > tracker.pid
    echo "task_tracker 已启动 (PID: $(cat tracker.pid))"
}
stop_tracker() {
    _stop_pidfile tracker.pid "task_tracker"
}

# success-tracker：采集每个实例的成功率时序（详情页「任务执行情况」趋势图）。
# 与浅层那套无关，只复用了 task_tracker 读 jsonl 的函数；offset 记在自己的
# success_tracker_state 表里，不会和 task_tracker 互相推进偏移。
start_success_tracker() {
    if [ -f success_tracker.pid ] && kill -0 "$(cat success_tracker.pid)" 2>/dev/null; then
        echo "success-tracker 已在运行 (PID: $(cat success_tracker.pid))"
        return
    fi
    rm -f success_tracker.pid
    nohup $PYTHON offline/success_tracker.py > success_tracker.log 2>&1 &
    echo $! > success_tracker.pid
    echo "success-tracker 已启动 (PID: $(cat success_tracker.pid))"
}
stop_success_tracker() {
    _stop_pidfile success_tracker.pid "success-tracker"
}

start_shallow_realtime() {
    if [ -f shallow_realtime.pid ] && kill -0 "$(cat shallow_realtime.pid)" 2>/dev/null; then
        echo "shallow-realtime 已在运行 (PID: $(cat shallow_realtime.pid))"
        return
    fi
    rm -f shallow_realtime.pid
    nohup $PYTHON offline/shallow_worker.py --mode realtime > shallow_realtime.log 2>&1 &
    echo $! > shallow_realtime.pid
    echo "shallow-realtime 已启动 (PID: $(cat shallow_realtime.pid))"
}
stop_shallow_realtime() {
    _stop_pidfile shallow_realtime.pid "shallow-realtime"
}

start_shallow_auto() {
    if [ -f shallow_auto.pid ] && kill -0 "$(cat shallow_auto.pid)" 2>/dev/null; then
        echo "shallow-auto 已在运行 (PID: $(cat shallow_auto.pid))"
        return
    fi
    rm -f shallow_auto.pid
    nohup $PYTHON offline/shallow_worker.py --mode auto > shallow_auto.log 2>&1 &
    echo $! > shallow_auto.pid
    echo "shallow-auto 已启动 (PID: $(cat shallow_auto.pid))"
}
stop_shallow_auto() {
    _stop_pidfile shallow_auto.pid "shallow-auto"
}

# shallow-query：把任务描述（config json 的 queries[].text）回填进
# task_records.query_text，供「会话列表」的内容检索用。
# 按实例 LRU 只保留最近 20 个（搜索是交互行为，历史实例没人搜）。
start_shallow_query() {
    if [ -f shallow_query.pid ] && kill -0 "$(cat shallow_query.pid)" 2>/dev/null; then
        echo "shallow-query 已在运行 (PID: $(cat shallow_query.pid))"
        return
    fi
    rm -f shallow_query.pid
    nohup $PYTHON offline/shallow_query.py > shallow_query.log 2>&1 &
    echo $! > shallow_query.pid
    echo "shallow-query 已启动 (PID: $(cat shallow_query.pid))"
}
stop_shallow_query() {
    _stop_pidfile shallow_query.pid "shallow-query"
}

# auto-tracker：历史实例的发现投递（背景车道）。
# task_tracker 是纯点击驱动,没点过的实例一行都不投 —— 实测全库 2126 个已结束实例
# 只有 16 个被跟踪,243 个实例共 301 万行永远等不到分析,而 shallow-auto 全程闲着。
# 本进程每轮挑**一个**「已结束+有缺口」的历史实例投进背景车道,逐个推进。
start_auto_tracker() {
    if [ -f auto_tracker.pid ] && kill -0 "$(cat auto_tracker.pid)" 2>/dev/null; then
        echo "auto-tracker 已在运行 (PID: $(cat auto_tracker.pid))"
        return
    fi
    rm -f auto_tracker.pid
    nohup $PYTHON offline/auto_tracker.py > auto_tracker.log 2>&1 &
    echo $! > auto_tracker.pid
    echo "auto-tracker 已启动 (PID: $(cat auto_tracker.pid))"
}
stop_auto_tracker() {
    _stop_pidfile auto_tracker.pid "auto-tracker"
}

# ── 旧的按阶段拆分的两个进程（已被 shallow-realtime/auto 取代）──
# 保留启停函数仅为回滚方便；正常不要和新进程同时跑 —— 它们的 claim 不带车道过滤，
# 会和新进程抢同一批行（CAS 不会重复处理，但会互相拖慢并打乱车道隔离）。
start_shallow_local() {
    if [ -f shallow_local.pid ] && kill -0 "$(cat shallow_local.pid)" 2>/dev/null; then
        echo "shallow-local 已在运行 (PID: $(cat shallow_local.pid))"
        return
    fi
    rm -f shallow_local.pid
    nohup $PYTHON offline/shallow_local.py > shallow_local.log 2>&1 &
    echo $! > shallow_local.pid
    echo "shallow-local 已启动 (PID: $(cat shallow_local.pid))"
}
stop_shallow_local() {
    _stop_pidfile shallow_local.pid "shallow-local"
}

start_shallow_obs() {
    if [ -f shallow_obs.pid ] && kill -0 "$(cat shallow_obs.pid)" 2>/dev/null; then
        echo "shallow-obs 已在运行 (PID: $(cat shallow_obs.pid))"
        return
    fi
    rm -f shallow_obs.pid
    nohup $PYTHON offline/shallow_obs.py > shallow_obs.log 2>&1 &
    echo $! > shallow_obs.pid
    echo "shallow-obs 已启动 (PID: $(cat shallow_obs.pid))"
}
stop_shallow_obs() {
    _stop_pidfile shallow_obs.pid "shallow-obs"
}

# 实时下载 worker（deep-realtime）：只处理用户点击过（artifact_priority=100）的任务。
# **没有 deep-auto**：交付物全量自动下载会写爆磁盘（实测积压 97 万条、盘只剩 214G），
# 所以产物是纯点击驱动 —— tracker 投递时置 pending_click，点开详情才入队。
start_realtime_worker() {
    if [ -f realtime_worker.pid ] && kill -0 "$(cat realtime_worker.pid)" 2>/dev/null; then
        echo "实时下载 worker 已在运行 (PID: $(cat realtime_worker.pid))"
        return
    fi
    rm -f realtime_worker.pid
    nohup $PYTHON offline/output_worker.py --realtime-only > realtime_worker.log 2>&1 &
    echo $! > realtime_worker.pid
    echo "实时下载 worker 已启动 (PID: $(cat realtime_worker.pid))"
}

stop_realtime_worker() {
    _stop_pidfile realtime_worker.pid "实时下载 worker"
}

case "$ACTION" in
    install)
        echo "=== 安装后端依赖 ==="
        $PIP install -r requirements.txt

        echo "=== 安装前端依赖 ==="
        cd frontend && npm install && cd ..
        echo "安装完成"

        # LibreOffice：仅「沙箱交付物」的 PPT/Word/Excel 在线预览需要（转 PDF）。
        # 没装不影响平台运行——这些格式降级为仅下载。
        # 故意不在这里直接 sudo apt install：install 原本无需提权，
        # 自动提权会在非 root 环境卡住或反复要密码。改成检测 + 提示 + 可选安装。
        if command -v soffice >/dev/null 2>&1 || command -v libreoffice >/dev/null 2>&1; then
            echo ""
            echo "=== LibreOffice 已安装（PPT/Word/Excel 可在线预览）==="
        else
            echo ""
            echo "=== 未检测到 LibreOffice ==="
            echo "    影响：PPT / Word / Excel 只能下载，不能在线预览（PDF/图片/HTML 不受影响）"
            echo "    安装命令（Debian/Ubuntu，需要 root）："
            echo "      sudo apt install -y libreoffice-core libreoffice-impress \\"
            echo "                        libreoffice-writer libreoffice-calc"
            echo "    或直接执行： $0 soffice-install"
        fi
        ;;

    soffice-install)
        # 单独一条命令装 LibreOffice，避免 install 被提权阻塞
        if command -v soffice >/dev/null 2>&1 || command -v libreoffice >/dev/null 2>&1; then
            echo "LibreOffice 已安装，无需重复安装"
            exit 0
        fi
        if ! command -v apt-get >/dev/null 2>&1; then
            echo "未找到 apt-get，本脚本只支持 Debian/Ubuntu。"
            echo "请按发行版自行安装 LibreOffice（需要 soffice 可执行文件）。"
            exit 1
        fi
        echo "=== 安装 LibreOffice（需要 sudo）==="
        sudo apt-get update
        sudo apt-get install -y libreoffice-core libreoffice-impress \
                                libreoffice-writer libreoffice-calc
        if command -v soffice >/dev/null 2>&1; then
            echo "安装完成：$(command -v soffice)"
            echo "服务无需重启（每次预览时实时检测 soffice 是否存在）"
        else
            echo "安装后仍未检测到 soffice，请检查安装是否完整"
            exit 1
        fi
        ;;

    build)
        echo "=== 构建前端 ==="
        cd frontend && npm run build && cd ..
        echo "构建完成，静态文件在 frontend/dist/"
        ;;

    dev)
        echo "=== 启动开发模式 ==="
        echo "后端: http://${HOST}:${PORT}"
        echo "前端: http://localhost:3000"
        echo ""
        echo "请在另一个终端执行: cd frontend && npm run dev"
        echo ""
        free_port "$PORT" || exit 1
        $UVICORN main:app --host "$HOST" --port "$PORT" --reload
        ;;

    start)
        echo "=== 启动生产模式 ==="
        # 确保前端已构建
        if [ ! -d "frontend/dist" ]; then
            echo "前端未构建，正在构建..."
            cd frontend && npm run build && cd ..
        fi

        echo "启动 uvicorn (workers=4)..."
        echo "服务地址: http://${HOST}:${PORT} (API直连)"
        # 端口清不掉就不要往下启动：否则新进程 bind 失败，而 start 仍然打印
        # "Uvicorn 已启动"，日志里只留一行 Errno 98，很难看出来。
        free_port "$PORT" || exit 1
        nohup $UVICORN main:app --host "$HOST" --port "$PORT" --workers 4 --limit-concurrency 100 > platform.log 2>&1 &
        echo $! > platform.pid
        echo "Uvicorn 已启动 (PID: $(cat platform.pid))"
        # 目标架构 4 进程（不含旧单体循环与 deep-auto 背景全量下载）
        start_tracker
        start_success_tracker
        start_auto_tracker
        start_shallow_realtime
        start_shallow_auto
        start_shallow_query
        start_realtime_worker

        # 启动 Nginx（如果已安装）
        if command -v nginx &> /dev/null; then
            echo "配置并启动 Nginx..."
            # 生成带正确路径的 nginx 配置
            NGINX_CONF="$SCRIPT_DIR/nginx.conf"
            if nginx -t -c "$NGINX_CONF" 2>/dev/null; then
                nginx -c "$NGINX_CONF"
                echo "Nginx 已启动，访问地址: http://${HOST}:${NGINX_PORT}"
            else
                echo "Nginx 配置检查失败，请手动配置:"
                echo "  sudo cp $NGINX_CONF /etc/nginx/conf.d/platform.conf"
                echo "  sudo nginx -t && sudo systemctl reload nginx"
                echo ""
                echo "或直接通过 uvicorn 访问: http://${HOST}:${PORT}"
            fi
        else
            echo "未安装 Nginx，静态文件将通过 uvicorn 服务"
            echo "建议安装 Nginx 以获得更好的性能: apt install nginx"
            echo ""
            echo "访问地址: http://${HOST}:${PORT}"
        fi
        ;;

    stop)
        if [ -f platform.pid ]; then
            kill "$(cat platform.pid)" 2>/dev/null
            rm -f platform.pid
            echo "Uvicorn 已停止"
        else
            echo "Uvicorn 未运行"
        fi
        # 停全部：目标架构 4 进程 + 旧进程（单体循环 / deep-auto），
        # 两边都停才不会在升级期间留下孤儿进程继续抢队列。
        stop_tracker
        stop_success_tracker
        stop_auto_tracker
        stop_shallow_realtime
        stop_shallow_auto
        stop_shallow_query
        stop_realtime_worker
        stop_shallow_local
        stop_shallow_obs
        stop_worker
        stop_deep_worker
        # uvicorn --workers N 是「父进程 + N 个 spawn 出来的子进程」。
        # 只 kill pid 文件里那个 master，子进程会被 reparent 到 PID 1 变成孤儿，
        # **继续占着端口**——表现就是「进程列表里看不到 uvicorn，但 8087 仍在监听」，
        # 下次 start 直接 Errno 98。这里按端口兜底清一遍。
        free_port "$PORT"
        # 停止 Nginx（如果由我们启动）
        if command -v nginx &> /dev/null; then
            nginx -s stop 2>/dev/null && echo "Nginx 已停止"
        fi
        ;;

    restart)
        echo "=== 重启服务 ==="
        if [ -f platform.pid ]; then
            kill "$(cat platform.pid)" 2>/dev/null
            rm -f platform.pid
            echo "已停止旧进程"
        fi
        # 停全部：目标架构 4 进程 + 旧进程（单体循环 / deep-auto），
        # 两边都停才不会在升级期间留下孤儿进程继续抢队列。
        stop_tracker
        stop_success_tracker
        stop_auto_tracker
        stop_shallow_realtime
        stop_shallow_auto
        stop_shallow_query
        stop_realtime_worker
        stop_shallow_local
        stop_shallow_obs
        stop_worker
        stop_deep_worker
        if command -v nginx &> /dev/null; then
            nginx -s stop 2>/dev/null
        fi
        sleep 1
        if [ ! -d "frontend/dist" ]; then
            echo "前端未构建，正在构建..."
            cd frontend && npm run build && cd ..
        fi

        echo "启动 uvicorn (workers=4)..."
        free_port "$PORT" || exit 1
        nohup $UVICORN main:app --host "$HOST" --port "$PORT" --workers 4 --limit-concurrency 100 > platform.log 2>&1 &
        echo $! > platform.pid
        echo "Uvicorn 已启动 (PID: $(cat platform.pid))"
        # 目标架构 4 进程（不含旧单体循环与 deep-auto 背景全量下载）
        start_tracker
        start_success_tracker
        start_auto_tracker
        start_shallow_realtime
        start_shallow_auto
        start_shallow_query
        start_realtime_worker

        if command -v nginx &> /dev/null; then
            NGINX_CONF="$SCRIPT_DIR/nginx.conf"
            if nginx -t -c "$NGINX_CONF" 2>/dev/null; then
                nginx -c "$NGINX_CONF"
                echo "Nginx 已启动，访问地址: http://${HOST}:${NGINX_PORT}"
            else
                echo "访问地址: http://${HOST}:${PORT}"
            fi
        else
            echo "访问地址: http://${HOST}:${PORT}"
        fi
        ;;

    logs)
        if [ -f platform.log ]; then
            tail -f platform.log
        else
            echo "日志文件不存在，服务可能未启动"
        fi
        ;;

    # 单个 worker 的实时日志。这 4 个就是当前架构的全部常驻进程。
    #   ./run.sh logs-tracker | logs-rt | logs-auto | logs-dl
    logs-tracker|logs-atracker|logs-rt|logs-auto|logs-dl|logs-query)
        case "$ACTION" in
            logs-tracker) _f=tracker.log;          _n="tracker（发现投递/点击）" ;;
            logs-atracker) _f=auto_tracker.log;    _n="auto-tracker（历史实例投递）" ;;
            logs-rt)      _f=shallow_realtime.log; _n="shallow-realtime（实时分析/点击）" ;;
            logs-auto)    _f=shallow_auto.log;     _n="shallow-auto（背景分析/全量）" ;;
            logs-dl)      _f=realtime_worker.log;  _n="deep-realtime（实时下载/交付物）" ;;
            logs-query)   _f=shallow_query.log;     _n="shallow-query（任务描述回填）" ;;
        esac
        if [ -f "$_f" ]; then
            echo "=== $_n → $_f ==="
            tail -n 30 -f "$_f"
        else
            echo "$_f 不存在 —— $_n 可能未启动，先跑 ./run.sh worker-start"
        fi
        ;;

    # 旧进程的日志（已被 rt/auto 取代，保留仅为排查历史问题）。
    # worker.log = --shallow-only 单体循环；deep_worker.log = --deep-only 背景全量下载。
    worker-logs)
        if [ -f worker.log ]; then
            echo "=== [旧] 单体浅层循环 worker.log（已被 rt/auto 取代）==="
            echo "    看当前架构请用: ./run.sh logs-rt / logs-auto / shallow-logs"
            tail -f worker.log
        else
            echo "worker.log 不存在（旧的 --shallow-only 进程，当前架构已不使用）"
            echo "看当前 4 个 worker: ./run.sh shallow-logs"
        fi
        ;;

    deep-worker-logs)
        if [ -f deep_worker.log ]; then
            echo "=== [旧] 背景全量下载 deep_worker.log（当前架构已不启用）==="
            echo "    实时下载日志请用: ./run.sh logs-dl"
            tail -f deep_worker.log
        else
            echo "deep_worker.log 不存在（旧的 --deep-only 进程，当前架构已不使用）"
            echo "看实时下载: ./run.sh logs-dl"
        fi
        ;;

    # 浅层进程的实时日志。支持两种写法：
    #   ./run.sh shallow-logs            （tail -f 全部）
    #   ./run.sh shallow logs            （等价，$2=logs 时转发到这里）
    #   ./run.sh shallow-logs rt         （只看 shallow-realtime；auto / tracker 同理）
    # tail -f 跟多个文件时会自动打 ==> 文件名 <== 分隔，不必自己拼前缀。
    shallow-logs)
        # $2 可能是子目标（tracker/rt/auto/...），也可能是 start 时的 host，这里只认前者
        case "$2" in
            tracker) _logs=(tracker.log) ;;
            atracker) _logs=(auto_tracker.log) ;;
            rt|realtime) _logs=(shallow_realtime.log) ;;
            auto)    _logs=(shallow_auto.log) ;;
            dl|download) _logs=(realtime_worker.log) ;;
            local)   _logs=(shallow_local.log) ;;      # 旧进程
            obs)     _logs=(shallow_obs.log) ;;        # 旧进程
            *)       _logs=(tracker.log auto_tracker.log shallow_realtime.log
                            shallow_auto.log realtime_worker.log) ;;
        esac
        _present=()
        for f in "${_logs[@]}"; do
            [ -f "$f" ] && _present+=("$f")
        done
        if [ ${#_present[@]} -eq 0 ]; then
            echo "浅层日志不存在（${_logs[*]}）—— 进程可能未启动，先跑 ./run.sh shallow-all-start"
        else
            echo "=== tail -f ${_present[*]} ==="
            tail -n 20 -f "${_present[@]}"
        fi
        ;;

    worker-status)
        # 当前架构的 4 个常驻进程（按场景分车道，不按阶段）
        echo "=== Worker 状态 ==="
        _ws() {   # $1=pid文件 $2=显示名 $3=日志
            if [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; then
                _p=$(cat "$1")
                _e=$(ps -o etime= -p "$_p" 2>/dev/null | tr -d ' ')
                printf "  %-34s 运行中  PID=%-8s 已跑 %s\n" "$2" "$_p" "$_e"
            else
                printf "  %-34s 未运行\n" "$2"
            fi
        }
        _ws tracker.pid          "tracker（发现投递/点击）"         tracker.log
        _ws auto_tracker.pid     "auto-tracker（历史实例投递）"      auto_tracker.log
        _ws shallow_realtime.pid "shallow-realtime（实时分析/点击）" shallow_realtime.log
        _ws shallow_auto.pid     "shallow-auto（背景分析/全量）"      shallow_auto.log
        _ws shallow_query.pid    "shallow-query（任务描述回填）"      shallow_query.log
        _ws realtime_worker.pid  "deep-realtime（实时下载）"         realtime_worker.log

        # 旧进程：正常应全部「未运行」。还在跑说明升级时漏停，会和新进程抢队列。
        for _pf in worker.pid deep_worker.pid shallow_local.pid shallow_obs.pid; do
            if [ -f "$_pf" ] && kill -0 "$(cat "$_pf")" 2>/dev/null; then
                echo "  ⚠ 旧进程仍在运行: $_pf (PID $(cat "$_pf")) —— 会和新架构抢队列，建议停掉"
            fi
        done

        echo ""
        echo "=== 各 worker 日志尾部 5 行 ==="
        for _pair in "tracker.log:tracker" \
                     "auto_tracker.log:auto-tracker" \
                     "shallow_realtime.log:shallow-realtime" \
                     "shallow_auto.log:shallow-auto" \
                     "shallow_query.log:shallow-query" \
                     "realtime_worker.log:deep-realtime"; do
            _f="${_pair%%:*}"; _n="${_pair##*:}"
            echo "--- $_n ($_f) ---"
            if [ -f "$_f" ]; then tail -n 5 "$_f"; else echo "($_f 不存在)"; fi
        done
        ;;

    # worker-start/stop/restart = 目标架构的 4 个常驻进程。
    #   tracker（发现投递）+ shallow-realtime（点击，两阶段）
    #   + shallow-auto（背景全量，最新优先）+ deep-realtime（点击的交付物）
    # 刻意**不启** start_worker（旧 --shallow-only 单体循环，已被 rt/auto 取代）
    # 与 start_deep_worker（背景全量下载 = deep-auto，会写爆磁盘，产物改纯点击驱动）。
    # 要跑旧进程用 ./run.sh shallow-start / deep-start 单独起。
    worker-start)
        start_tracker
        start_success_tracker
        start_auto_tracker
        start_shallow_realtime
        start_shallow_auto
        start_shallow_query
        start_realtime_worker
        ;;

    worker-stop)
        stop_tracker
        stop_success_tracker
        stop_auto_tracker
        stop_shallow_realtime
        stop_shallow_auto
        stop_shallow_query
        stop_realtime_worker
        ;;

    worker-restart)
        stop_tracker
        stop_success_tracker
        stop_auto_tracker
        stop_shallow_realtime
        stop_shallow_auto
        stop_shallow_query
        stop_realtime_worker
        sleep 1
        start_tracker
        start_success_tracker
        start_auto_tracker
        start_shallow_realtime
        start_shallow_auto
        start_shallow_query
        start_realtime_worker
        ;;

    shallow-start|shallow-stop|shallow-restart)
        case "$ACTION" in
            shallow-start) start_worker ;;
            shallow-stop)  stop_worker ;;
            shallow-restart) stop_worker; sleep 1; start_worker ;;
        esac
        ;;

    # 浅层新架构三件套：一起启停，避免只起一半导致队列停滞
    tracker-start|tracker-stop|tracker-restart)
        case "$ACTION" in
            tracker-start) start_tracker ;;
            tracker-stop)  stop_tracker ;;
            tracker-restart) stop_tracker; sleep 1; start_tracker ;;
            success-tracker-start) start_success_tracker ;;
            success-tracker-stop)  stop_success_tracker ;;
            success-tracker-restart) stop_success_tracker; sleep 1; start_success_tracker ;;
        esac
        ;;
    query-start|query-stop|query-restart)
        case "$ACTION" in
            query-start) start_shallow_query ;;
            query-stop)  stop_shallow_query ;;
            query-restart) stop_shallow_query; sleep 1; start_shallow_query ;;
        esac
        ;;
    atracker-start|atracker-stop|atracker-restart)
        case "$ACTION" in
            atracker-start) start_auto_tracker ;;
            atracker-stop)  stop_auto_tracker ;;
            atracker-restart) stop_auto_tracker; sleep 1; start_auto_tracker ;;
        esac
        ;;
    rt-start|rt-stop|rt-restart)
        case "$ACTION" in
            rt-start) start_shallow_realtime ;;
            rt-stop)  stop_shallow_realtime ;;
            rt-restart) stop_shallow_realtime; sleep 1; start_shallow_realtime ;;
        esac
        ;;
    auto-start|auto-stop|auto-restart)
        case "$ACTION" in
            auto-start) start_shallow_auto ;;
            auto-stop)  stop_shallow_auto ;;
            auto-restart) stop_shallow_auto; sleep 1; start_shallow_auto ;;
        esac
        ;;
    # 旧的按阶段拆分的两个进程（已被 rt/auto 取代，保留仅为回滚）
    local-start|local-stop|local-restart)
        case "$ACTION" in
            local-start) start_shallow_local ;;
            local-stop)  stop_shallow_local ;;
            local-restart) stop_shallow_local; sleep 1; start_shallow_local ;;
        esac
        ;;
    obs-start|obs-stop|obs-restart)
        case "$ACTION" in
            obs-start) start_shallow_obs ;;
            obs-stop)  stop_shallow_obs ;;
            obs-restart) stop_shallow_obs; sleep 1; start_shallow_obs ;;
        esac
        ;;
    shallow-all-start|shallow-all-stop|shallow-all-restart)
        # 目标架构的 4 个常驻进程：tracker + shallow-realtime + shallow-auto + deep-realtime。
        # 不含 deep-auto（产物纯点击驱动，全量下载会写爆磁盘）。
        case "$ACTION" in
            shallow-all-start)
                start_tracker; start_success_tracker; start_auto_tracker
                start_shallow_realtime; start_shallow_auto
                start_shallow_query; start_realtime_worker ;;
            shallow-all-stop)
                stop_tracker; stop_success_tracker; stop_auto_tracker
                stop_shallow_realtime; stop_shallow_auto
                stop_shallow_query; stop_realtime_worker ;;
            shallow-all-restart)
                stop_tracker; stop_success_tracker; stop_auto_tracker
                stop_shallow_realtime; stop_shallow_auto
                stop_shallow_query; stop_realtime_worker; sleep 1
                start_tracker; start_success_tracker; start_auto_tracker
                start_shallow_realtime; start_shallow_auto
                start_shallow_query; start_realtime_worker ;;
        esac
        ;;

    deep-start|deep-stop|deep-restart|artifacts-start|artifacts-stop|artifacts-restart)
        case "$ACTION" in
            deep-start|artifacts-start) start_deep_worker ;;
            deep-stop|artifacts-stop)   stop_deep_worker ;;
            deep-restart|artifacts-restart) stop_deep_worker; sleep 1; start_deep_worker ;;
        esac
        ;;

    realtime-start|realtime-stop|realtime-restart)
        case "$ACTION" in
            realtime-start) start_realtime_worker ;;
            realtime-stop)  stop_realtime_worker ;;
            realtime-restart) stop_realtime_worker; sleep 1; start_realtime_worker ;;
        esac
        ;;

    *)
        echo "用法: $0 {install|soffice-install|build|dev|start|stop|restart|logs|worker-status|worker-start|worker-stop|worker-restart|shallow-*|deep-*} [host] [port] [nginx_port]"
        echo ""
        echo "  install          安装依赖（后端 + 前端；并检测 LibreOffice）"
        echo "  soffice-install  安装 LibreOffice（PPT/Word/Excel 在线预览用，需 sudo）"
        echo "  build            构建前端静态文件"
        echo "  dev              开发模式（后端热重载）"
        echo "  start            生产模式启动（uvicorn + nginx + 浅层/产物下载 worker）"
        echo "  stop             停止服务（含 worker）"
        echo "  restart          重启服务（含 worker）"
        echo "  logs             查看主服务日志（uvicorn，实时）"
        echo ""
        echo "  —— 四个 worker 的日志（当前架构）——"
        echo "  logs-tracker     tracker 发现投递(点击)   → tracker.log"
        echo "  logs-atracker    auto-tracker 历史实例投递 → auto_tracker.log"
        echo "  logs-rt          shallow-realtime 实时分析 → shallow_realtime.log"
        echo "  logs-auto        shallow-auto 背景分析     → shallow_auto.log"
        echo "  logs-dl          deep-realtime 实时下载    → realtime_worker.log"
        echo "  logs-query       shallow-query 任务描述回填 → shallow_query.log"
        echo "  shallow-logs     四个一起看（tail -f 全部，自动打文件名分隔）"
        echo "                   也可写 ./run.sh shallow logs [tracker|rt|auto|dl]"
        echo "  worker-status    四个 worker 的 PID/运行时长 + 各自日志尾部"
        echo ""
        echo "  —— 旧进程日志（已被 rt/auto 取代，仅排查历史问题用）——"
        echo "  worker-logs      [旧] --shallow-only 单体循环 → worker.log"
        echo "  deep-worker-logs [旧] --deep-only 背景全量下载 → deep_worker.log"
        echo ""
        echo "  —— Worker 独立控制（不影响 uvicorn / nginx）——"
        echo "  worker-start     启动浅层 + 产物下载"
        echo "  worker-stop      停止浅层 + 产物下载"
        echo "  worker-restart   重启浅层 + 产物下载"
        echo "  shallow-all-start|stop|restart  目标架构 4 进程一起启停"
        echo "                   （tracker + auto-tracker + shallow-realtime + shallow-auto + deep-realtime）"
        echo "  tracker-start|stop|restart   只动发现投递(点击驱动)"
        echo "  atracker-start|stop|restart  只动历史实例投递(每轮一个实例)"
        echo "  rt-start|stop|restart        只动实时分析（用户点击，两阶段，2s）"
        echo "  auto-start|stop|restart      只动背景分析（全量，最新优先）"
        echo "  query-start|stop|restart     只动任务描述回填（内容检索用，LRU 20 实例）"
        echo "  realtime-start|stop|restart  只动实时下载（点击的交付物，2s 轮询）"
        echo "  local-start|obs-start ...    旧的按阶段拆分进程（已被 rt/auto 取代，仅回滚用）"
        echo "  shallow-start|stop|restart   只动浅层（旧的 --shallow-only 单体循环）"
        echo "  deep-start|stop|restart      只动产物下载（deep/artifacts 两个名字等价）"
        echo "  artifacts-start|stop|restart 同 deep（产物下载跑在 deep_worker 进程里）"
        echo ""
        echo "默认端口: uvicorn=${PORT}, nginx=${NGINX_PORT}"
        ;;
esac
