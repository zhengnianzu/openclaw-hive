#!/bin/bash
# OpenClaw Hive Platform 启动脚本

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"


ACTION="${1:-help}"
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

# 实时下载 worker：只处理用户点击过（artifact_priority=100）的任务。
# 与 deep worker 分开进程、分开线程池，点击不必等几万条积压。
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
        start_worker
        start_deep_worker
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
        stop_worker
        stop_deep_worker
        stop_realtime_worker
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
        stop_worker
        stop_deep_worker
        stop_realtime_worker
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
        start_worker
        start_deep_worker
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

    worker-logs)
        if [ -f worker.log ]; then
            echo "=== 浅层 worker.log ==="
            tail -f worker.log
        else
            echo "worker.log 不存在，浅层 worker 可能未启动"
        fi
        ;;

    deep-worker-logs)
        if [ -f deep_worker.log ]; then
            echo "=== 产物下载 deep_worker.log ==="
            tail -f deep_worker.log
        else
            echo "deep_worker.log 不存在，产物下载 worker 可能未启动"
        fi
        ;;

    worker-status)
        if [ -f worker.pid ] && kill -0 "$(cat worker.pid)" 2>/dev/null; then
            echo "浅层 worker 运行中 (PID: $(cat worker.pid))"
        else
            echo "浅层 worker 未运行"
        fi
        if [ -f deep_worker.pid ] && kill -0 "$(cat deep_worker.pid)" 2>/dev/null; then
            echo "产物下载 worker 运行中 (PID: $(cat deep_worker.pid))"
        else
            echo "产物下载 worker 未运行"
        fi
        if [ -f realtime_worker.pid ] && kill -0 "$(cat realtime_worker.pid)" 2>/dev/null; then
            echo "实时下载 worker 运行中 (PID: $(cat realtime_worker.pid))"
        else
            echo "实时下载 worker 未运行"
        fi
        echo "--- 浅层 worker.log 尾部 20 行 ---"
        if [ -f worker.log ]; then
            tail -n 20 worker.log
        else
            echo "(worker.log 不存在)"
        fi
        echo "--- 产物下载 deep_worker.log 尾部 20 行 ---"
        if [ -f deep_worker.log ]; then
            tail -n 20 deep_worker.log
        else
            echo "(deep_worker.log 不存在)"
        fi
        ;;

    worker-start)
        start_worker
        start_deep_worker
        start_realtime_worker
        ;;

    worker-stop)
        stop_worker
        stop_deep_worker
        stop_realtime_worker
        ;;

    worker-restart)
        stop_worker
        stop_deep_worker
        stop_realtime_worker
        sleep 1
        start_worker
        start_deep_worker
        start_realtime_worker
        ;;

    shallow-start|shallow-stop|shallow-restart)
        case "$ACTION" in
            shallow-start) start_worker ;;
            shallow-stop)  stop_worker ;;
            shallow-restart) stop_worker; sleep 1; start_worker ;;
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
        echo "  logs             查看主服务日志（实时）"
        echo "  worker-logs      查看浅层 worker 日志（实时）"
        echo "  deep-worker-logs 查看产物下载 worker 日志（实时）"
        echo "  worker-status    查看浅层/产物下载 worker 状态 + 日志尾部"
        echo ""
        echo "  —— Worker 独立控制（不影响 uvicorn / nginx）——"
        echo "  worker-start     启动浅层 + 产物下载"
        echo "  worker-stop      停止浅层 + 产物下载"
        echo "  worker-restart   重启浅层 + 产物下载"
        echo "  shallow-start|stop|restart   只动浅层（分级/补全）"
        echo "  deep-start|stop|restart      只动产物下载（deep/artifacts 两个名字等价）"
        echo "  artifacts-start|stop|restart 同 deep（产物下载跑在 deep_worker 进程里）"
        echo "  realtime-start|stop|restart  只动实时下载（仅处理用户点击的任务，2s 轮询）"
        echo ""
        echo "默认端口: uvicorn=${PORT}, nginx=${NGINX_PORT}"
        ;;
esac
