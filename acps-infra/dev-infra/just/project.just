# 共享 project 配方 — 统一提供顶层 help 入口。

[default]
help:
    @just _shared_project_help

_shared_project_help section='all':
    #!/usr/bin/env bash
    set -eu -o pipefail

    requested_section="{{section}}"
    project_name="{{project_name}}"
    project_kind="{{project_kind}}"
    project_hook_lib="{{project_just_hook_lib}}"
    dev_runtime_profile="{{dev_runtime_profile}}"
    dev_runtime_actions="{{dev_runtime_actions}}"
    dev_checks="{{dev_checks}}"
    dev_bootstrap_checks="{{dev_bootstrap_checks}}"
    test_checks="{{test_checks}}"
    test_bootstrap_checks="{{test_bootstrap_checks}}"
    test_all_steps="{{test_all_steps}}"
    test_e2e_profiles="{{test_e2e_profiles}}"
    qa_profile="{{qa_profile}}"
    package_scope_enabled="{{package_scope_enabled}}"
    package_required_paths="{{package_required_paths}}"

    if [[ -n "${project_hook_lib}" ]]; then
        if [[ ! -f "${project_hook_lib}" ]]; then
            echo "[ERROR] 项目 hook 文件不存在：${project_hook_lib}" >&2
            exit 1
        fi
        # shellcheck source=/dev/null
        source "${project_hook_lib}"
    fi

    has_token() {
        local token="$1"
        local list="$2"
        [[ " ${list} " == *" ${token} "* ]]
    }

    run_help_hook() {
        local section="$1"
        if declare -F run_project_help_section >/dev/null 2>&1; then
            run_project_help_section "${section}"
        fi
    }

    render_sequence() {
        local list="$1"
        local rendered="${list// / -> }"
        printf '%s\n' "${rendered}"
    }

    print_multiline_with_indent() {
        local indent="$1"
        shift
        while IFS= read -r line; do
            if [[ -n "${line}" ]]; then
                printf '%s%s\n' "${indent}" "${line}"
            else
                printf '\n'
            fi
        done <<< "$*"
    }

    describe_check_step() {
        local check_id="$1"
        local service_name=""
        local project_description=""

        case "${check_id}" in
            python-toolchain)
                echo "检查 python3 与项目目标 Python 版本可用。"
                ;;
            just-tool)
                echo "检查 just 可执行文件可用。"
                ;;
            uv-sync)
                echo "确保锁文件与 .venv 同步；需要修复时等价于 just prep sync。"
                ;;
            dotenv)
                echo "确保 .env 已就绪；需要修复时等价于 just prep env。"
                ;;
            hooks)
                echo "确保 Git hooks 已安装；需要修复时等价于 just prep hooks。"
                ;;
            infra-*)
                service_name="${check_id#infra-}"
                echo "确保共享依赖 ${service_name} 已启动且健康；需要修复时等价于 just infra up ${service_name} && just infra wait ${service_name}。"
                ;;
            migrate-dev)
                echo "确保开发环境数据库迁移位于 head；需要修复时等价于 just prep migrate dev。"
                ;;
            migrate-test)
                echo "确保测试环境数据库迁移位于 head；需要修复时等价于 just prep migrate test。"
                ;;
            *)
                if declare -F describe_project_check_step >/dev/null 2>&1; then
                    project_description="$(describe_project_check_step "${check_id}" 2>/dev/null || true)"
                    if [[ -n "${project_description}" ]]; then
                        echo "${project_description}"
                        return
                    fi
                fi
                echo "项目自定义检查/修复项：${check_id}。"
                ;;
        esac
    }

    print_check_list() {
        local checks="$1"
        local check_id=""

        if [[ -z "${checks}" ]]; then
            echo "    当前项目未声明共享 check 列表。"
            return
        fi

        for check_id in ${checks}; do
            printf '    - %s: %s\n' "${check_id}" "$(describe_check_step "${check_id}")"
        done
    }

    print_usage() {
        echo "${project_name} Just 命令"
        echo ""
        echo "用法："
        echo "  just"
        echo "  just help"
        echo "  just dev [action] [args...]"
        echo "  just test [action] [args...]"
        echo "  just qa [action] [args...]"
        echo "  just package [action] [args...]"
        echo "  just infra [action] [args...]"
        echo "  just prep [action] [args...]"
        echo ""
        echo "顶层公开入口："
        echo "  help             显示本帮助"
        echo "  dev              开发入口"
        echo "  test             测试入口"
        echo "  qa               代码质量入口"
        echo "  package          wheel 运行包构建入口"
        echo "  infra            共享依赖入口"
        echo "  prep             维护与局部修复入口"
    }

    print_scoped_usage() {
        local command_name="$1"
        local default_action="${2:-}"

        echo "${project_name} ${command_name} 命令"
        echo ""
        echo "用法："
        echo "  just ${command_name} [action] [args...]"
        if [[ -n "${default_action}" ]]; then
            echo "  不传 action 时等同于：just ${command_name} ${default_action}"
        fi
    }

    print_dev_help() {
        echo ""
        if [[ "${project_kind}" == "tool" ]] || [[ "${dev_runtime_profile}" == "none" ]]; then
            echo "dev：开发联调环境准备"
            echo "  help             显示 dev 帮助"
            echo "  check            只读检查开发/联调前置条件；不会修改本地状态"
            echo "  bootstrap        check-first 修复开发前置条件；只处理可自动准备的项目项"
            echo "                   本项目 dev check 项："
            print_check_list "${dev_checks}"
            echo "                   本项目 dev bootstrap ensure 项："
            print_check_list "${dev_bootstrap_checks}"
            run_help_hook "dev"
            return
        fi

        echo "dev：本地应用生命周期入口"
        echo "  help             显示 dev 帮助"
        echo "  check            只读检查开发环境；不会修改本地状态"
        echo "                   本项目 dev check 项："
        print_check_list "${dev_checks}"
        echo "  bootstrap        check-first 修复开发前置条件；只处理可自动准备的项目项"
        echo "                   本项目 dev bootstrap ensure 项："
        print_check_list "${dev_bootstrap_checks}"

        if has_token "start" "${dev_runtime_actions}"; then
            echo "  start            默认后台启动；执行前总会先 just dev bootstrap"
            echo "  start bg         与 start 等价；若检测到受管后台实例已运行，则直接提示已启动"
            echo "  start fg         前台启动；执行前总会先 just dev bootstrap"
            echo "                   若检测到受管后台实例已运行，则报错并提示先 just dev stop"
            echo "                   若后台未运行但端口被其他前台/外部进程占用，启动失败时会直接暴露端口占用错误"
        fi
        if has_token "stop" "${dev_runtime_actions}"; then
            echo "  stop             停止受管后台实例；幂等，未运行时只提示当前状态"
        fi
        if has_token "status" "${dev_runtime_actions}"; then
            echo "  status           查看受管后台实例状态；不负责探测外部或手动前台进程"
        fi
        if has_token "logs" "${dev_runtime_actions}"; then
            echo "  logs             查看最近后台日志"
            echo "  logs follow      持续跟随后台日志；适合观察 bg / restart 的输出"
        fi
        if has_token "restart" "${dev_runtime_actions}"; then
            echo "  restart [bg]     仅管理受管后台实例；等价 stop 后再执行 start bg"
        fi
        if has_token "smoke" "${dev_runtime_actions}"; then
            echo "  smoke            执行项目声明的开发态 smoke 检查"
        fi

        run_help_hook "dev"
    }

    print_test_help() {
        echo ""
        echo "test：测试入口"
        echo "  help             显示 test 帮助"
        echo "  check            只读检查测试环境前置条件；不会修改本地状态"
        echo "                   本项目 test check 项："
        print_check_list "${test_checks}"
        echo "  bootstrap        check-first 准备测试环境；只处理可自动准备的项目项"
        echo "                   本项目 test bootstrap ensure 项："
        print_check_list "${test_bootstrap_checks}"
        echo "  unit             运行单元测试；[args...] 原样透传给项目测试命令/pytest"
        run_help_hook "test-actions"

        if has_token "integration" "${test_all_steps}"; then
            echo "  integration      运行集成测试；[args...] 原样透传给项目测试命令/pytest"
        fi
        if has_token "e2e" "${test_all_steps}"; then
            if [[ -n "${test_e2e_profiles}" ]]; then
                echo "  e2e              按声明顺序执行 e2e profile：$(render_sequence "${test_e2e_profiles}")"
            else
                echo "  e2e              运行 e2e 测试"
            fi
            echo "                   [args...] 原样透传给对应项目测试命令/pytest"
            echo "                   旧的 <profile>-e2e 顶层公开别名已收敛，统一由 just test e2e 触发"
        fi
        echo "  coverage         运行覆盖率统计；[args...] 原样透传给项目测试命令/pytest"
        echo "  all              依次执行 $(render_sequence "${test_all_steps}")"
        echo "                   all 只是顺序编排已有子命令，不引入额外隐式动作"
        echo "  参数说明："
        echo "    [args...]      透传参数数组；常见取值："
        echo "      -q           精简输出"
        echo "      -k <expr>    pytest 过滤表达式；<expr> 为字符串"
        echo "      <path>       测试目录或测试文件路径，如 tests/unit/、tests/api/test_x.py"

        run_help_hook "test"
    }

    print_qa_help() {
        echo ""
        echo "qa：代码质量入口"
        echo "  help             显示 qa 帮助"
        echo "                   不传 action 时等同于 just qa help"
        echo "  precommit        执行 pre-commit run --all-files"
        echo "                   具体 hook 由仓库 .pre-commit-config.yaml 决定"
        echo "                   当前各项目通常已在这里覆盖格式化、静态检查、类型检查、安全扫描与部分仓库维护检查"
        echo "  pip-audit        导出锁定依赖后执行 pip-audit 依赖漏洞扫描"
        echo "                   该步骤依赖外部漏洞公告，可能包含需要单独判断的 advisory 噪音"
        run_help_hook "qa"
        echo "  参数说明："
        echo "    qa 公开动作当前不接受额外位置参数。"
    }

    print_package_help() {
        echo ""
        echo "package：原生 wheel 运行包构建"
        echo "  help             显示 package 帮助"
        if [[ "${package_scope_enabled}" != "1" ]]; then
            echo "  check            检查当前项目 package scope 是否启用"
            run_help_hook "package"
            return
        fi

        echo "  check [--python-version <version>]"
        echo "                  只读检查打包前置条件"
        echo "  bootstrap [--python-version <version>]"
        echo "                  按需补齐打包前置条件（不生成发布物）"
        echo "  wheel [--python-version <version>]"
        echo "                  构建平台无关应用薄包（不含 wheelhouse/）"

        run_help_hook "package"
    }

    print_infra_help() {
        echo ""
        echo "infra：共享依赖管理"
        echo "  help                                 显示 infra 帮助"
        echo "  check                                只读检查 Docker / Compose / compose.yml / service 映射"
        echo "  status [service...] [--format=tsv]   查看共享依赖状态"
        echo "  up [service...]                      启动共享依赖；不传 service 时默认 postgres"
        echo "  down                                 停止全部共享依赖"
        echo "  wait [service...]                    等待共享依赖就绪；不传 service 时等待当前已创建容器"
        echo "  logs [service...] [--tail N] [--since DURATION] [--follow]"
        echo "                                       查看共享依赖日志"
        echo "  reset [service...] [--volumes] [--yes]"
        echo "                                       修复性重建共享依赖"
        echo "  参数说明："
        echo "    [service...]      0 个或多个公开 service 名称：postgres redis rabbitmq gateway keycloak kafka victoria-metrics clickhouse minio opensearch"
        echo "    --format=tsv      status 输出为制表符分隔；可选值目前仅 tsv"
        echo "    --tail N          logs 尾部行数；N 为正整数"
        echo "    --since DURATION  logs 时间范围；使用 Docker duration 字符串，如 10m / 1h"
        echo "    --follow          持续跟随 logs 输出"
        echo "    --volumes         reset 时同时删除 volume"
        echo "    --yes             全量 reset 或带 --volumes 的确认开关"

        run_help_hook "infra"
    }

    print_prep_help() {
        echo ""
        echo "prep：维护与局部修复动作"
        echo "  help             显示 prep 帮助"
        echo "  env              缺失时根据 .env.example 生成 .env"
        echo "  sync             执行依赖同步；通常用于修复 uv lock 与 .venv 不一致"
        echo "  hooks            安装/更新 Git hooks"
        echo "  package [--python-version <version>]"
        echo "                   直接委托 just package bootstrap；只做打包前置准备，不生成发布物"
        if [[ " ${package_required_paths} " == *" alembic "* ]] || [[ " ${package_required_paths} " == *" alembic.ini "* ]]; then
            echo "  migrate [dev|test]"
            echo "                   dev 表示开发环境数据库同步"
            echo "                   test 表示测试环境数据库同步"
        fi

        run_help_hook "prep"
    }

    case "${requested_section}" in
        all|"")
            print_usage
            print_dev_help
            print_test_help
            print_qa_help
            print_package_help
            print_infra_help
            print_prep_help
            ;;
        dev)
            print_scoped_usage "dev" "help"
            print_dev_help
            ;;
        test)
            print_scoped_usage "test" "all"
            print_test_help
            ;;
        qa)
            print_scoped_usage "qa" "help"
            print_qa_help
            ;;
        infra)
            print_scoped_usage "infra" "help"
            print_infra_help
            ;;
        prep)
            print_scoped_usage "prep" "help"
            print_prep_help
            ;;
        *)
            echo "[ERROR] 未知帮助分区：${requested_section}" >&2
            exit 2
            ;;
    esac
