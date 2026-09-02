# syntax=docker/dockerfile:1
# rabbitmq 派生镜像。
#
# 基于官方 rabbitmq:4.2-management-alpine 追加启用 acps-infra 约定的插件集合
# （rabbitmq_auth_mechanism_ssl / rabbitmq_auth_backend_http / rabbitmq_auth_backend_cache），
# 这些插件默认不随 "-management" tag 启用，必须显式声明才能被 RabbitMQ 加载。
# management 插件已经随 "-management" tag 内置启用，enabled_plugins.template 里
# 仍然显式列出是为了让最终生效的插件集合有一份自描述、可比对的清单。
#
# 用法：
# docker buildx build --platform linux/arm64 \
# --build-arg UPSTREAM_IMAGE=rabbitmq:4.2-management-alpine@sha256:... \
# --build-arg INFRA_ID=rabbitmq --build-arg UPSTREAM_VERSION=4.2-management-alpine \
# --build-arg ACPS_VERSION=2.2.0 --build-arg PLATFORM=linux/arm64 \
# -t acps/rabbitmq:4.2-management-alpine-2.2.0-linux-arm64 \
# --output type=docker,dest=out.image.tar -f Dockerfile.

ARG UPSTREAM_IMAGE
FROM ${UPSTREAM_IMAGE}

ARG UPSTREAM_IMAGE=""
ARG UPSTREAM_IMAGE_NAME
ARG UPSTREAM_DIGEST
ARG INFRA_ID
ARG UPSTREAM_VERSION
ARG ACPS_VERSION
ARG PLATFORM
ARG IMAGE_CREATED=""
ARG SOURCE_REVISION=""

COPY enabled_plugins.template /etc/rabbitmq/enabled_plugins

LABEL org.opencontainers.image.title="${INFRA_ID}" \
    org.opencontainers.image.version="${UPSTREAM_VERSION}-${ACPS_VERSION}" \
    org.opencontainers.image.created="${IMAGE_CREATED}" \
    pub.acps.image.kind="infra" \
    pub.acps.infra.id="${INFRA_ID}" \
    pub.acps.infra.upstream.image="${UPSTREAM_IMAGE_NAME}" \
    pub.acps.infra.upstream.digest="${UPSTREAM_DIGEST}" \
    pub.acps.platform="${PLATFORM}"
