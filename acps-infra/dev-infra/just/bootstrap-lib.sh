#!/usr/bin/env bash
# 共享 bootstrap helper。当前主要复用 doctor-lib 的 scope 编排。

set -euo pipefail

bootstrap_scope() {
    local scope="$1"
    local checks="$2"

    run_scope_bootstrap "${scope}" "${checks}"
}
