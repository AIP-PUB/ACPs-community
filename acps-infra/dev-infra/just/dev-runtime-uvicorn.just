# 共享 uvicorn runtime 配方 — 标准单进程服务的 start/stop/status/logs/restart/bootstrap。

_shared_dev_runtime_uvicorn action='start' *args:
    #!/usr/bin/env bash
    set -eu -o pipefail

    source "../acps-infra/dev-infra/just/app-lib.sh"

    action="{{action}}"
    args_string="{{args}}"
    host="{{app_host}}"
    port="{{app_port}}"
    pid_file="{{app_pid_file}}"
    log_file="{{app_log_file}}"
    app_module="{{app_module}}"
    project_name="{{project_name}}"
    reload_policy="{{uvicorn_reload_policy}}"
    reload_probe_python="{{uvicorn_reload_probe_python}}"
    reload_excludes="{{uvicorn_reload_excludes}}"
    health_url="http://127.0.0.1:${port}/health"

    APP_LIVE_PID_FILE="${pid_file}"

    requested_args=()
    if [[ -n "${args_string}" ]]; then
        read -r -a requested_args <<< "${args_string}"
    fi

    while [[ "${#requested_args[@]}" -gt 0 ]] && [[ "${requested_args[0]}" == "--" ]]; do
        requested_args=("${requested_args[@]:1}")
    done

    uvicorn_reload_args=()

    build_reload_args() {
        local exclude_pattern=""
        uvicorn_reload_args=()
        case "${reload_policy}" in
            ""|always)
                uvicorn_reload_args=(--reload)
                ;;
            never)
                ;;
            settings-probe)
                if [[ -z "${reload_probe_python}" ]]; then
                    echo "[ERROR] uvicorn_reload_policy=settings-probe 但未声明 uvicorn_reload_probe_python。" >&2
                    exit 2
                fi

                if uv run --no-sync python -c "${reload_probe_python}"; then
                    uvicorn_reload_args=(--reload)
                fi
                ;;
            *)
                echo "[ERROR] 未知 uvicorn_reload_policy：${reload_policy}" >&2
                exit 2
                ;;
        esac

        if [[ "${#uvicorn_reload_args[@]}" -eq 0 ]] || [[ -z "${reload_excludes}" ]]; then
            return
        fi

        for exclude_pattern in ${reload_excludes}; do
            uvicorn_reload_args+=(--reload-exclude "${exclude_pattern}")
        done
    }

    justfile_declares_recipe() {
        local recipe_name="$1"

        if [[ ! -f Justfile ]]; then
            return 1
        fi

        grep -Eq "^${recipe_name}(\\s|:|$)" Justfile
    }

    reject_unexpected_runtime_args() {
        local command_name="$1"
        shift || true

        if [[ "$#" -eq 0 ]]; then
            return 0
        fi

        echo "[ERROR] dev ${command_name} 不接受额外参数：$*" >&2
        exit 2
    }

    case "${action}" in
        bootstrap)
            if ! justfile_declares_recipe "_bootstrap_common"; then
                echo "[ERROR] 标准 uvicorn runtime 需要项目声明隐藏 _bootstrap_common recipe。" >&2
                exit 2
            fi

            just _bootstrap_common
            just prep migrate dev
            ;;
        start|"")
            local_mode="bg"
            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                case "${requested_args[0]}" in
                    bg|fg)
                        local_mode="${requested_args[0]}"
                        requested_args=("${requested_args[@]:1}")
                        ;;
                esac
            fi

            while [[ "${#requested_args[@]}" -gt 0 ]] && [[ "${requested_args[0]}" == "--" ]]; do
                requested_args=("${requested_args[@]:1}")
            done

            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                reject_unexpected_runtime_args "start" "${requested_args[@]}"
            fi

            if pid="$(read_live_pid)"; then
                if [[ "${local_mode}" == "bg" ]]; then
                    echo "[INFO]  ${project_name} 已在后台运行（pid=${pid}）。"
                    exit 0
                fi

                echo "[ERROR] ${project_name} 当前已有后台实例运行（pid=${pid}）。请先执行 just dev stop，再执行 just dev start fg。" >&2
                exit 2
            fi

            if [[ "${local_mode}" == "bg" ]]; then
                just dev bootstrap
                ensure_log_dir_for_file "${log_file}"
                build_reload_args

                if [[ "${#requested_args[@]}" -gt 0 ]]; then
                    if [[ "${#uvicorn_reload_args[@]}" -gt 0 ]]; then
                        pid="$(launch_detached "${log_file}" uv run uvicorn "${app_module}" "${uvicorn_reload_args[@]}" --host "${host}" --port "${port}" "${requested_args[@]}")"
                    else
                        pid="$(launch_detached "${log_file}" uv run uvicorn "${app_module}" --host "${host}" --port "${port}" "${requested_args[@]}")"
                    fi
                else
                    if [[ "${#uvicorn_reload_args[@]}" -gt 0 ]]; then
                        pid="$(launch_detached "${log_file}" uv run uvicorn "${app_module}" "${uvicorn_reload_args[@]}" --host "${host}" --port "${port}")"
                    else
                        pid="$(launch_detached "${log_file}" uv run uvicorn "${app_module}" --host "${host}" --port "${port}")"
                    fi
                fi
                printf '%s\n' "${pid}" > "${pid_file}"

                sleep 3
                if ! kill -0 "${pid}" 2>/dev/null; then
                    rm -f "${pid_file}"
                    echo "[ERROR] ${project_name} 后台启动失败，请检查日志：${log_file}" >&2
                    exit 1
                fi

                echo "[INFO]  ${project_name} 已在后台启动（pid=${pid}）。"
                echo "[INFO]  服务地址：http://localhost:${port}"
                echo "[INFO]  OpenAPI 文档：http://localhost:${port}/docs"
                echo "[INFO]  健康检查：${health_url}"
                echo "[INFO]  日志文件：${log_file}"
                exit 0
            fi

            just dev bootstrap
            ensure_log_dir_for_file "${log_file}"
            build_reload_args
            echo "[INFO]  启动 ${project_name}（前台）..."
            echo "[INFO]  服务地址：http://localhost:${port}"
            echo "[INFO]  OpenAPI 文档：http://localhost:${port}/docs"
            echo "[INFO]  健康检查：${health_url}"
            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                if [[ "${#uvicorn_reload_args[@]}" -gt 0 ]]; then
                    exec uv run uvicorn "${app_module}" "${uvicorn_reload_args[@]}" --host "${host}" --port "${port}" "${requested_args[@]}"
                fi

                exec uv run uvicorn "${app_module}" --host "${host}" --port "${port}" "${requested_args[@]}"
            fi

            if [[ "${#uvicorn_reload_args[@]}" -gt 0 ]]; then
                exec uv run uvicorn "${app_module}" "${uvicorn_reload_args[@]}" --host "${host}" --port "${port}"
            fi

            exec uv run uvicorn "${app_module}" --host "${host}" --port "${port}"
            ;;
        stop)
            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                reject_unexpected_runtime_args "stop" "${requested_args[@]}"
            fi
            if pid="$(read_live_pid)"; then
                kill_wait "${pid}"
                rm -f "${pid_file}"
                echo "[INFO]  已停止 ${project_name} 后台实例（pid=${pid}）。"
            else
                echo "[INFO]  ${project_name} 当前没有后台实例。"
            fi
            kill_port "${port}"
            ;;
        status)
            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                reject_unexpected_runtime_args "status" "${requested_args[@]}"
            fi
            if pid="$(read_live_pid)"; then
                echo "[INFO]  ${project_name} 正在运行（pid=${pid}）。"
            else
                echo "[INFO]  ${project_name} 当前未运行。"
            fi
            ;;
        logs)
            ensure_log_dir_for_file "${log_file}"
            if [[ ! -f "${log_file}" ]]; then
                echo "[ERROR] 未找到日志文件：${log_file}" >&2
                exit 1
            fi

            if [[ "${requested_args[0]:-}" == "follow" ]]; then
                if [[ "${#requested_args[@]}" -gt 1 ]]; then
                    reject_unexpected_runtime_args "logs" "${requested_args[@]:1}"
                fi
                exec tail -f "${log_file}"
            fi

            if [[ "${#requested_args[@]}" -gt 0 ]]; then
                reject_unexpected_runtime_args "logs" "${requested_args[@]}"
            fi

            tail -n 200 "${log_file}"
            ;;
        restart)
            if [[ "${#requested_args[@]}" -gt 0 ]] && [[ "${requested_args[0]}" != "bg" ]]; then
                echo "[ERROR] dev restart 只管理后台实例；如需前台运行，请直接执行 just dev start fg。" >&2
                exit 2
            fi
            just _shared_dev_runtime_uvicorn stop
            just _shared_dev_runtime_uvicorn start
            ;;
        *)
            echo "[ERROR] 未知共享 uvicorn runtime action: ${action}" >&2
            exit 2
            ;;
    esac
