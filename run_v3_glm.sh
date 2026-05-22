#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${SCRIPT_DIR}/nohup-glm.pid"
LOG_FILE="${SCRIPT_DIR}/nohup-glm.log"

# 默认配置文件
CONFIG_FILE="${SCRIPT_DIR}/config-glm.yaml"

get_pid() {
    if [ -f "$PID_FILE" ]; then
        cat "$PID_FILE"
    else
        echo ""
    fi
}

is_running() {
    pid=$(get_pid)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        return 0
    fi
    return 1
}

do_start() {
    if is_running; then
        pid=$(get_pid)
        echo "Service is already running (PID: $pid)"
        return 1
    fi
    echo "Starting service..."
    nohup python run_v3.py --config "$CONFIG_FILE" >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "Service started (PID: $(get_pid))"
}

do_stop() {
    if ! is_running; then
        echo "Service is not running"
    else
        pid=$(get_pid)
        echo "Stopping service (PID: $pid)..."
        kill "$pid" 2>/dev/null
        rm -f "$PID_FILE"
        echo "Service stopped"
    fi
    # 清理 pods
    do_clear
}

do_restart() {
    do_stop
    sleep 2
    do_start
}

do_status() {
    if is_running; then
        echo "$(get_pid)"
    else
        echo "0"
    fi
}

do_clear() {
    if [ -z "$CONFIG_FILE" ]; then
        echo "Error: --config is required for clear"
        exit 1
    fi
    echo "Clearing pods..."
    python run_clear.py --config "$CONFIG_FILE" --delete
}

# 解析参数
ACTION=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)
            CONFIG_FILE="$2"
            shift 2
            ;;
        --start)
            ACTION="start"
            shift
            ;;
        --stop)
            ACTION="stop"
            shift
            ;;
        --restart)
            ACTION="restart"
            shift
            ;;
        --status)
            ACTION="status"
            shift
            ;;
        --clear)
            ACTION="clear"
            shift
            ;;
        *)
            echo "Usage: $0 {--start|--stop|--restart|--status|--clear} [--config <path>]"
            exit 1
            ;;
    esac
done

if [ -z "$ACTION" ]; then
    echo "Usage: $0 {--start|--stop|--restart|--status|--clear} [--config <path>]"
    exit 1
fi

case "$ACTION" in
    start)
        do_start
        ;;
    stop)
        do_stop
        ;;
    restart)
        do_restart
        ;;
    status)
        do_status
        ;;
    clear)
        do_clear
        ;;
esac

exit 0
