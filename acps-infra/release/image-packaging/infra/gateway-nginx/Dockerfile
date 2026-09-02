# syntax=docker/dockerfile:1
# gateway-nginx 派生镜像。
#
# 基于官方 nginx:1.27-alpine 追加一份最小默认反向代理模板，供安装阶段作为起点
# 覆盖/挂载真实拓扑配置；不内置生产域名、证书或最终路由规则（ 边界）。
#
# 用法：
# docker buildx build --platform linux/arm64 \
# --build-arg UPSTREAM_IMAGE=nginx:1.27-alpine@sha256:... \
# --build-arg INFRA_ID=gateway-nginx --build-arg UPSTREAM_VERSION=1.27-alpine \
# --build-arg ACPS_VERSION=2.2.0 --build-arg PLATFORM=linux/arm64 \
# -t acps/gateway-nginx:1.27-alpine-2.2.0-linux-arm64 \
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

COPY conf.d/default.conf.template /etc/nginx/templates/default.conf.template

LABEL org.opencontainers.image.title="${INFRA_ID}" \
    org.opencontainers.image.version="${UPSTREAM_VERSION}-${ACPS_VERSION}" \
    org.opencontainers.image.created="${IMAGE_CREATED}" \
    pub.acps.image.kind="infra" \
    pub.acps.infra.id="${INFRA_ID}" \
    pub.acps.infra.upstream.image="${UPSTREAM_IMAGE_NAME}" \
    pub.acps.infra.upstream.digest="${UPSTREAM_DIGEST}" \
    pub.acps.platform="${PLATFORM}"
