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

# 提醒：激活了别人的 venv 时，python3 会指向那个项目的环境，
# 依赖会装错地方、服务也用错解释器跑（典型症状：装完仍报 No module named 'bcrypt'）。
_active_venv="${VIRTUAL_ENV:-}"
if [ -n "$_active_venv" ]; then
    _venv_root="$(cd "$_active_venv/.." 2>/dev/null && pwd)"
    _self_root="$(cd "$SCRIPT_DIR/.." 2>/dev/null && pwd)"
    if [ "$_venv_root" != "$_self_root" ]; then
        echo "警告: 当前激活的虚拟环境不属于本仓库:"
        echo "      VIRTUAL_ENV=$_active_venv"
        echo "      将使用 python3 = $PYTHON"
        echo "      如非本意，请先 deactivate，或用 PYTHON=/path/to/python3 指定解释器。"
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
free_port() {
    local port="$1" pids
    pids=$( { lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null || fuser -n tcp "$port" 2>/dev/null; } | tr -s ' ' '\n' | grep -E '^[0-9]+$' | sort -u )
    if [ -z "$pids" ]; then
        return 0
    fi
    echo "端口 $port 被占用 (PID: $(echo $pids | tr '\n' ' '))，正在释放..."
    kill $pids 2>/dev/null
    for _ in 1 2 3 4 5; do
        sleep 0.2
        pids=$( { lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null || fuser -n tcp "$port" 2>/dev/null; } | tr -s ' ' '\n' | grep -E '^[0-9]+$' | sort -u )
        [ -z "$pids" ] && { echo "端口 $port 已释放"; return 0; }
    done
    kill -9 $pids 2>/dev/null
    sleep 0.3
    pidof_check=$( { lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null || fuser -n tcp "$port" 2>/dev/null; } | tr -s ' ' '\n' | grep -E '^[0-9]+$' | sort -u )
    if [ -n "$pidof_check" ]; then
        echo "警告: 端口 $port 仍被占用 (PID: $(echo $pidof_check | tr '\n' ' '))"
        return 1
    fi
    echo "端口 $port 已释放"
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
        free_port "$PORT"
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
        free_port "$PORT"
        nohup $UVICORN main:app --host "$HOST" --port "$PORT" --workers 4 --limit-concurrency 100 > platform.log 2>&1 &
        echo $! > platform.pid
        echo "Uvicorn 已启动 (PID: $(cat platform.pid))"
        start_worker
        start_deep_worker

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
        if command -v nginx &> /dev/null; then
            nginx -s stop 2>/dev/null
        fi
        sleep 1
        if [ ! -d "frontend/dist" ]; then
            echo "前端未构建，正在构建..."
            cd frontend && npm run build && cd ..
        fi

        echo "启动 uvicorn (workers=4)..."
        free_port "$PORT"
        nohup $UVICORN main:app --host "$HOST" --port "$PORT" --workers 4 --limit-concurrency 100 > platform.log 2>&1 &
        echo $! > platform.pid
        echo "Uvicorn 已启动 (PID: $(cat platform.pid))"
        start_worker
        start_deep_worker

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
        ;;

    worker-stop)
        stop_worker
        stop_deep_worker
        ;;

    worker-restart)
        stop_worker
        stop_deep_worker
        sleep 1
        start_worker
        start_deep_worker
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
        echo ""
        echo "默认端口: uvicorn=${PORT}, nginx=${NGINX_PORT}"
        ;;
esac
