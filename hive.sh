#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 激活 conda 环境
eval "$(conda shell.bash hook)"
conda activate openclaw

# openclaw-task 源码目录（与本项目同级）
TASK_SRC_DIR="${SCRIPT_DIR}/openclaw-task"

# 归档目录
LOGS_DIR="${SCRIPT_DIR}/logs"
PIDS_DIR="${SCRIPT_DIR}/logs"
TASK_CONFIGS_DIR="${SCRIPT_DIR}/config_tasks"

# 确保目录存在
mkdir -p "$LOGS_DIR" "$PIDS_DIR" "$TASK_CONFIGS_DIR"

# 多 config 支持
CONFIG_FILES=()

# 根据 config 文件名生成对应的 PID 和日志文件
get_instance_name() {
    local cfg="$1"
    basename "$cfg" .yaml
}

get_pid_file() {
    local cfg="$1"
    echo "${PIDS_DIR}/nohup_$(get_instance_name "$cfg").pid"
}

get_log_file() {
    local cfg="$1"
    echo "${LOGS_DIR}/nohup_$(get_instance_name "$cfg").log"
}

get_pid() {
    local pid_file="$1"
    if [ -f "$pid_file" ]; then
        cat "$pid_file"
    else
        echo ""
    fi
}

is_running_cfg() {
    local cfg="$1"
    local pid_file
    pid_file=$(get_pid_file "$cfg")
    local pid
    pid=$(get_pid "$pid_file")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        return 0
    fi
    return 1
}

# 打包 openclaw-task 获取最新代码（只打一次）
do_pack() {
    if [ ! -d "$TASK_SRC_DIR" ]; then
        echo "Error: openclaw-task source not found at $TASK_SRC_DIR"
        exit 1
    fi
    local tar_target="${SCRIPT_DIR}/uploads/openclaw-task.tar"
    echo "Packing openclaw-task from $TASK_SRC_DIR ..."
    tar -cf "$tar_target" -C "$(dirname "$TASK_SRC_DIR")" "$(basename "$TASK_SRC_DIR")"
    echo "Packed: $tar_target"
}

# 启动单个 config
start_one() {
    local cfg="$1"
    local name pid_file log_file

    name=$(get_instance_name "$cfg")
    pid_file=$(get_pid_file "$cfg")
    log_file=$(get_log_file "$cfg")

    if is_running_cfg "$cfg"; then
        echo "  [$name] already running (PID: $(get_pid "$pid_file"))"
        return 1
    fi

    echo "  [$name] starting..."
    nohup python hive.py --config "$cfg" >> "$log_file" 2>&1 &
    echo $! > "$pid_file"
    echo "  [$name] started (PID: $!, log: $log_file)"
}

# 停止单个 config
stop_one() {
    local cfg="$1"
    local name pid_file pid

    name=$(get_instance_name "$cfg")
    pid_file=$(get_pid_file "$cfg")
    pid=$(get_pid "$pid_file")

    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        echo "  [$name] not running"
    else
        echo "  [$name] stopping (PID: $pid)..."
        kill "$pid" 2>/dev/null
        echo "  [$name] stopped"
    fi
    rm -f "$pid_file"
}

do_start() {
    # 打包一次（所有任务共享同一个 tar）
    do_pack

    echo "Starting ${#CONFIG_FILES[@]} task(s)..."
    for cfg in "${CONFIG_FILES[@]}"; do
        start_one "$cfg"
    done
}

do_stop() {
    echo "Stopping ${#CONFIG_FILES[@]} task(s)..."
    for cfg in "${CONFIG_FILES[@]}"; do
        stop_one "$cfg"
    done
    # 清理 pods
    for cfg in "${CONFIG_FILES[@]}"; do
        echo "  Clearing pods for [$(get_instance_name "$cfg")]..."
        python run_clear.py --config "$cfg" --delete
    done
}

do_stop_all() {
    echo "Stopping all services..."
    for pid_file in "${PIDS_DIR}"/nohup_*.pid; do
        [ -f "$pid_file" ] || continue
        local pid
        pid=$(cat "$pid_file")
        local name
        name=$(basename "$pid_file" .pid | sed 's/^nohup_//')
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "  [$name] stopping (PID: $pid)..."
            kill "$pid" 2>/dev/null
        fi
        rm -f "$pid_file"
    done
    echo "All services stopped"
}

do_restart() {
    do_stop
    sleep 2
    do_start
}

do_status() {
    echo "Task status:"
    local found=false
    for pid_file in "${PIDS_DIR}"/nohup_*.pid; do
        [ -f "$pid_file" ] || continue
        found=true
        local name pid
        name=$(basename "$pid_file" .pid | sed 's/^nohup_//')
        pid=$(cat "$pid_file")
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "  [$name] running (PID: $pid)"
        else
            echo "  [$name] stopped"
            rm -f "$pid_file"
        fi
    done
    if [ "$found" = false ]; then
        echo "  No tasks found"
    fi
}

do_clear() {
    for cfg in "${CONFIG_FILES[@]}"; do
        echo "Clearing pods for [$(get_instance_name "$cfg")]..."
        python run_clear.py --config "$cfg" --delete
    done
}

# 从 config.yaml 中提取 task_output_path
get_output_path() {
    local cfg="$1"
    python -c "
from omegaconf import OmegaConf
cfg = OmegaConf.load('$cfg')
print(cfg.run_config.task.get('task_output_path', 'outputs'))
" 2>/dev/null || echo "outputs"
}

# 从 config.yaml 中提取 task_download_path
get_download_path() {
    local cfg="$1"
    python -c "
from omegaconf import OmegaConf
cfg = OmegaConf.load('$cfg')
print(cfg.run_config.task.get('task_download_path', 'downloads'))
" 2>/dev/null || echo "downloads"
}

# 重置任务数据：清理 logs、env_info.db、outputs、downloads
do_reset() {
    echo "Resetting ${#CONFIG_FILES[@]} task(s)..."
    echo ""
    echo "WARNING: This will delete logs, env_info.db, outputs and download cache!"
    echo "  Target: ${CONFIG_FILES[*]}"
    read -p "Continue? [y/N] " confirm
    if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
        echo "Aborted."
        return 0
    fi

    for cfg in "${CONFIG_FILES[@]}"; do
        local name output_path download_path
        name=$(get_instance_name "$cfg")
        output_path=$(get_output_path "$cfg")
        download_path=$(get_download_path "$cfg")

        echo ""
        echo "=== Resetting [$name] ==="

        # 先停止运行中的进程
        if is_running_cfg "$cfg"; then
            stop_one "$cfg"
        fi

        # 1. 清理日志和 PID
        rm -f "${LOGS_DIR}/nohup_${name}.log" && echo "  [REMOVED] logs/nohup_${name}.log"
        rm -f "${PIDS_DIR}/nohup_${name}.pid" && echo "  [REMOVED] logs/nohup_${name}.pid"

        # 2. 删除 env_info.db
        if [ -f "${SCRIPT_DIR}/env_info.db" ]; then
            rm -f "${SCRIPT_DIR}/env_info.db"
            echo "  [REMOVED] env_info.db"
        fi

        # 3. 清空 outputs 对应内容
        if [ -d "${SCRIPT_DIR}/${output_path}" ]; then
            rm -rf "${SCRIPT_DIR}/${output_path}"
            echo "  [REMOVED] ${output_path}/"
        fi

        # 4. 清理 downloads 缓存
        if [ -d "${SCRIPT_DIR}/${download_path}" ]; then
            rm -rf "${SCRIPT_DIR}/${download_path}"
            echo "  [REMOVED] ${download_path}/"
        fi
    done

    echo ""
    echo "Reset complete. You can now restart tasks with: $0 --start"
}

# 自动发现 config_tasks/ 下所有 config_*.yaml 文件（排除 config_template.yaml）
discover_configs() {
    for f in "${TASK_CONFIGS_DIR}"/config_*.yaml; do
        [ -f "$f" ] || continue
        [[ "$(basename "$f")" == "config_template.yaml" ]] && continue
        CONFIG_FILES+=("$f")
    done
    if [ ${#CONFIG_FILES[@]} -eq 0 ]; then
        echo "Error: no config_*.yaml files found in $TASK_CONFIGS_DIR (config_template.yaml is excluded)"
        echo "Hint: cp ${TASK_CONFIGS_DIR}/config_template.yaml ${TASK_CONFIGS_DIR}/config_1.yaml"
        echo "      Or specify: $0 --start --config config_tasks/your_config.yaml"
        exit 1
    fi
}

# ============================================================================
# 解析参数
# ============================================================================
ACTION=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)
            CONFIG_FILES+=("$2")
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
        --stop-all)
            ACTION="stop_all"
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
        --reset)
            ACTION="reset"
            shift
            ;;
        *)
            echo "Usage: $0 {--start|--stop|--stop-all|--restart|--reset|--status|--clear} [--config <path>]..."
            echo ""
            echo "Examples:"
            echo "  $0 --start                          # 自动发现并启动 config_tasks/config_*.yaml"
            echo "  $0 --start --config config_tasks/config_1.yaml   # 启动单个"
            echo "  $0 --start --config c1.yaml --config c2.yaml     # 启动多个"
            echo "  $0 --stop-all                       # 停止所有运行中的任务"
            echo "  $0 --status                         # 查看所有任务状态"
            echo "  $0 --reset --config config_tasks/config_1.yaml   # 重置任务数据后重新开始"
            exit 1
            ;;
    esac
done

if [ -z "$ACTION" ]; then
    echo "Usage: $0 {--start|--stop|--stop-all|--restart|--reset|--status|--clear} [--config <path>]..."
    exit 1
fi

# 如果没有手动指定 --config，自动发现 config_tasks/config_*.yaml
if [ ${#CONFIG_FILES[@]} -eq 0 ] && [ "$ACTION" != "status" ] && [ "$ACTION" != "stop_all" ]; then
    discover_configs
    echo "Auto-discovered ${#CONFIG_FILES[@]} config(s): ${CONFIG_FILES[*]}"
fi

case "$ACTION" in
    start)
        do_start
        ;;
    stop)
        do_stop
        ;;
    stop_all)
        do_stop_all
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
    reset)
        do_reset
        ;;
esac

exit 0
