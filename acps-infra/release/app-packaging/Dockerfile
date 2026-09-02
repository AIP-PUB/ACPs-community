# 参数化装配 Dockerfile。
#
# 故意不声明远端 `# syntax=docker/dockerfile:...` 前端镜像：本文件只使用当前
# Docker/BuildKit 已内建支持的基础语法，避免把 Docker Hub 上的 frontend image
# 变成一条未锁定、未记录在 images.lock 里的隐藏网络依赖。
#
# builder 镜像 digest 必须与同目录 images.lock 的 [builder] 表保持一致——Docker
# FROM 指令无法在构建期动态读取外部 TOML 文件，因此这里按平台各写一个 FROM 阶段，
# 由 Buildx 通过 TARGETOS/TARGETARCH 自动选择，调用方不需要额外传参数。
#
# 用法（在已解开的 assembly kit 根目录下执行，build context 必须同时包含
# packages/ 和 assembly/）：
# docker buildx build --platform linux/arm64 \
# --build-arg APP=registry-server --build-arg PYTHON_TAG=cp314 \
# --target export -f assembly/Dockerfile --output type=local,dest=out.

FROM quay.io/pypa/manylinux_2_28_x86_64@sha256:35795c40ab9eed587a0f51a752c7defeabcc8a2ccd434d3497be201cdd34cb63 AS base-linux-amd64
FROM quay.io/pypa/manylinux_2_28_aarch64@sha256:1aa721f140a4e88caf74e375992e4d79a1f991dc799a968de237752ce073a01b AS base-linux-arm64

FROM base-${TARGETOS}-${TARGETARCH} AS assemble

COPY packages/ /kit/packages/
COPY assembly/assemble.sh /kit/assemble.sh

ARG APP
ARG PYTHON_TAG=cp314
ARG VARIANT=
ARG TARGETPLATFORM

# --variant 总是显式传递（可能是空字符串），不使用 shell 条件展开——由 assemble.sh
# 统一判断空值语义（未声明 variant / 声明了 variant 但传空 / 传了非法 variant 三种情况）。
RUN chmod +x /kit/assemble.sh \
    && /kit/assemble.sh --app "${APP}" --python-tag "${PYTHON_TAG}" --variant "${VARIANT}" --platform "${TARGETPLATFORM}" --output /out

# 装配容器带有完整编译工具链和输入材料，不能直接作为导出内容；只有这个 scratch 阶段
# COPY 的 /out 目录才是真正要导出的应用最终发布包产物。
FROM scratch AS export
COPY --from=assemble /out /
