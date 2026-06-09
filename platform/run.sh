#!/bin/bash
# OpenClaw Hive Platform 启动脚本

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"


ACTION="${1:-help}"
HOST="${2:-0.0.0.0}"
PORT="${3:-8000}"

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
        echo "服务地址: http://${HOST}:${PORT}"
        nohup uvicorn main:app --host "$HOST" --port "$PORT" --workers 2 > platform.log 2>&1 &
        echo $! > platform.pid
        echo "已启动 (PID: $(cat platform.pid))"
        ;;

    stop)
        if [ -f platform.pid ]; then
            kill "$(cat platform.pid)" 2>/dev/null
            rm -f platform.pid
            echo "已停止"
        else
            echo "未运行"
        fi
        ;;

    *)
        echo "用法: $0 {install|build|dev|start|stop} [host] [port]"
        echo ""
        echo "  install   安装依赖（后端 + 前端）"
        echo "  build     构建前端静态文件"
        echo "  dev       开发模式（后端热重载）"
        echo "  start     生产模式启动"
        echo "  stop      停止服务"
        ;;
esac
