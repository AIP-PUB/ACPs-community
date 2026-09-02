#!/bin/sh
# 通用容器入口。
#
# 保持最小化：不在这里做任何业务初始化或配置渲染（那些属于安装阶段的职责，
# 不在 image-mode 镜像打包边界内），只负责把容器启动命令原样 exec 出去，
# 确保容器可以正确接收 SIGTERM 等信号（exec 替换掉 shell 进程本身，而不是
# 让 shell 继续以子进程方式运行 "$@"）。
#
# 默认 CMD 是 /opt/acps/bin/app-run（由 build_context.py 生成的 dispatcher）
# 也支持完全覆盖 CMD，例如 acps-cli 场景：
# docker run --rm acps/acps-cli:2.2.0-linux-amd64 acps-cli registry agent list
set -e

exec "$@"
