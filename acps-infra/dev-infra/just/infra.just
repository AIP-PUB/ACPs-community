# 共享 dev-infra Just 模块 — 由各 Python 服务项目的 Justfile import。
#
#   dev_infra_script := "../acps-infra/dev-infra/dev-infra.sh"
#   import "../acps-infra/dev-infra/just/infra.just"
#
# 导入前必须在项目 Justfile 中定义 dev_infra_script。

infra action='help' *args:
    #!/usr/bin/env bash
    set -eu -o pipefail

    action="{{action}}"
    args_string="{{args}}"
    dev_infra_script="{{dev_infra_script}}"

    requested_args=()
    if [[ -n "${args_string}" ]]; then
        read -r -a requested_args <<< "${args_string}"
    fi

    if [[ "${action}" == "help" ]]; then
        just _shared_project_help infra
        exit 0
    fi

    case "${action}" in
        check|status|up|down|wait|logs|reset)
            ;;
        *)
            echo "[ERROR] 未知 infra action: ${action}" >&2
            exit 2
            ;;
    esac

    if [[ "${#requested_args[@]}" -gt 0 ]]; then
        exec "${dev_infra_script}" "${action}" "${requested_args[@]}"
    fi

    exec "${dev_infra_script}" "${action}"
