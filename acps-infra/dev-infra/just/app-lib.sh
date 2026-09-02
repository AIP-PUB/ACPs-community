#!/usr/bin/env bash
# 共享后台进程辅助函数 — 由各项目 Justfile app/test 配方 source。
#
#   source "../acps-infra/dev-infra/just/app-lib.sh"
#
# 函数：
#   ensure_log_dir_for_file <path>
#   read_live_pid [pid_file]
#   kill_wait <pid>
#   kill_port <port>
#   launch_detached <log_file> <cmd...>

set -euo pipefail

ensure_log_dir_for_file() {
    local target_path="$1"
    mkdir -p "$(dirname "${target_path}")"
}

read_live_pid() {
    local f="${1:-${APP_LIVE_PID_FILE:-}}"
    [[ -n "${f}" ]] || return 1
    [[ -f "${f}" ]] || return 1
    local pid
    pid="$(tr -d '[:space:]' <"${f}")"
    [[ -n "${pid}" ]] || { rm -f "${f}"; return 1; }
    if kill -0 "${pid}" 2>/dev/null; then
        printf '%s\n' "${pid}"
        return 0
    fi
    rm -f "${f}"
    return 1
}

kill_wait() {
    local pid="$1"
    kill -- "-${pid}" 2>/dev/null || kill "${pid}" 2>/dev/null || return 0
    local i
    for i in {1..10}; do
        sleep 1
        kill -0 "${pid}" 2>/dev/null || return 0
    done
    kill -9 -- "-${pid}" 2>/dev/null || kill -9 "${pid}" 2>/dev/null || true
    sleep 1
}

kill_port() {
    local port="$1"
    local pids
    pids="$(lsof -ti :"${port}" 2>/dev/null || true)"
    if [[ -n "${pids}" ]]; then
        echo "${pids}" | xargs kill -9 2>/dev/null || true
    fi
}

launch_detached() {
    local target_log_file="$1"
    shift

    local py_bin
    py_bin="$(command -v python3 || command -v python)"
    if [[ -z "${py_bin}" ]]; then
        echo "[ERROR] 未找到 python 解释器，无法后台启动进程。" >&2
        exit 1
    fi

    "${py_bin}" -c 'import os, subprocess, sys; log_file = sys.argv[1]; cmd = sys.argv[2:]; log = open(log_file, "ab", buffering=0); proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, preexec_fn=os.setsid, close_fds=True); print(proc.pid)' "${target_log_file}" "$@"
}
