#!/bin/bash
# OpenClaw Hive Platform 启动脚本

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"


ACTION="${1:-help}"
HOST="${2:-0.0.0.0}"
PORT="${3:-8087}"
NGINX_PORT="${4:-80}"

# 输出分析 worker：独立进程管理（pid 文件 worker.pid，日志 worker.log）
start_worker() {
    if [ -f worker.pid ] && kill -0 "$(cat worker.pid)" 2>/dev/null; then
        echo "输出分析 worker 已在运行 (PID: $(cat worker.pid))"
        return
    fi
    nohup python3 offline/output_worker.py > worker.log 2>&1 &
    echo $! > worker.pid
    echo "输出分析 worker 已启动 (PID: $(cat worker.pid))"
}

stop_worker() {
    if [ -f worker.pid ]; then
        kill "$(cat worker.pid)" 2>/dev/null
        rm -f worker.pid
        echo "输出分析 worker 已停止"
    else
        echo "输出分析 worker 未运行"
    fi
}

case "$ACTION" in
    install)
        echo "=== 安装后端依赖 ==="
        pip install -r requirements.txt

        echo "=== 安装前端依赖 ==="
        cd frontend && npm install && cd ..
        echo "安装完成"
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
        uvicorn main:app --host "$HOST" --port "$PORT" --reload
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
        nohup uvicorn main:app --host "$HOST" --port "$PORT" --workers 4 --limit-concurrency 100 > platform.log 2>&1 &
        echo $! > platform.pid
        echo "Uvicorn 已启动 (PID: $(cat platform.pid))"
        start_worker

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
        if command -v nginx &> /dev/null; then
            nginx -s stop 2>/dev/null
        fi
        sleep 1
        if [ ! -d "frontend/dist" ]; then
            echo "前端未构建，正在构建..."
            cd frontend && npm run build && cd ..
        fi

        echo "启动 uvicorn (workers=4)..."
        nohup uvicorn main:app --host "$HOST" --port "$PORT" --workers 4 --limit-concurrency 100 > platform.log 2>&1 &
        echo $! > platform.pid
        echo "Uvicorn 已启动 (PID: $(cat platform.pid))"
        start_worker

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
            tail -f worker.log
        else
            echo "worker.log 不存在，输出分析 worker 可能未启动"
        fi
        ;;

    worker-status)
        if [ -f worker.pid ] && kill -0 "$(cat worker.pid)" 2>/dev/null; then
            echo "输出分析 worker 运行中 (PID: $(cat worker.pid))"
        else
            echo "输出分析 worker 未运行"
        fi
        echo "--- worker.log 尾部 20 行 ---"
        if [ -f worker.log ]; then
            tail -n 20 worker.log
        else
            echo "(worker.log 不存在)"
        fi
        ;;

    *)
        echo "用法: $0 {install|build|dev|start|stop|restart|logs|worker-logs|worker-status} [host] [port] [nginx_port]"
        echo ""
        echo "  install        安装依赖（后端 + 前端）"
        echo "  build          构建前端静态文件"
        echo "  dev            开发模式（后端热重载）"
        echo "  start          生产模式启动（uvicorn + nginx）"
        echo "  stop           停止服务"
        echo "  restart        重启服务"
        echo "  logs           查看主服务日志（实时）"
        echo "  worker-logs    查看输出分析 worker 日志（实时）"
        echo "  worker-status  查看 worker 存活状态 + 日志尾部"
        echo ""
        echo "默认端口: uvicorn=${PORT}, nginx=${NGINX_PORT}"
        ;;
esac
