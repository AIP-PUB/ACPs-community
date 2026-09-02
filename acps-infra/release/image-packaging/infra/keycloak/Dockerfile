# syntax=docker/dockerfile:1
# keycloak 派生镜像。
#
# 基于官方 quay.io/keycloak/keycloak 追加预置 realm 定义文件（来自
# acps-infra/dev-infra/keycloak/realms/（开发联调用同源模板；镜像包内另有副本），已确认不含 client secret / 私钥等敏感内容，
# 只是 realm/client/认证流程结构定义）。是否在启动命令追加 `--import-realm` 由
# 安装阶段决定，本镜像不覆盖上游默认 ENTRYPOINT/CMD。
#
# 用法：
# docker buildx build --platform linux/arm64 \
# --build-arg UPSTREAM_IMAGE=quay.io/keycloak/keycloak:26.6.3@sha256:... \
# --build-arg INFRA_ID=keycloak --build-arg UPSTREAM_VERSION=26.6.3 \
# --build-arg ACPS_VERSION=2.2.0 --build-arg PLATFORM=linux/arm64 \
# -t acps/keycloak:26.6.3-2.2.0-linux-arm64 \
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

COPY realms/ /opt/keycloak/data/import/

LABEL org.opencontainers.image.title="${INFRA_ID}" \
    org.opencontainers.image.version="${UPSTREAM_VERSION}-${ACPS_VERSION}" \
    org.opencontainers.image.created="${IMAGE_CREATED}" \
    pub.acps.image.kind="infra" \
    pub.acps.infra.id="${INFRA_ID}" \
    pub.acps.infra.upstream.image="${UPSTREAM_IMAGE_NAME}" \
    pub.acps.infra.upstream.digest="${UPSTREAM_DIGEST}" \
    pub.acps.platform="${PLATFORM}"
