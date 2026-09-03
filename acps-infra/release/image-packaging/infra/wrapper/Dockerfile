# syntax=docker/dockerfile:1
# 通用基础设施镜像 label-only wrapper。
#
# 只适用于内容完全等于上游镜像、不需要额外安装任何软件或导入任何配置的场景
# （例如 Redis、Redpanda、VictoriaMetrics、ClickHouse、MinIO、OpenSearch）。
# 不改动上游镜像内容，只在其基础上追加 ACPS provenance label。
#
# 用法：
# docker buildx build --platform linux/arm64 \
# --build-arg UPSTREAM_IMAGE=redis:7-alpine@sha256:... \
# --build-arg INFRA_ID=redis --build-arg UPSTREAM_VERSION=7-alpine \
# --build-arg ACPS_VERSION=2.2.0 --build-arg IMAGE_CREATED=... \
# -t acps/redis:7-alpine-2.2.0-linux-arm64 \
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

LABEL org.opencontainers.image.title="${INFRA_ID}" \
    org.opencontainers.image.version="${UPSTREAM_VERSION}-${ACPS_VERSION}" \
    org.opencontainers.image.created="${IMAGE_CREATED}" \
    pub.acps.image.kind="infra" \
    pub.acps.infra.id="${INFRA_ID}" \
    pub.acps.infra.upstream.image="${UPSTREAM_IMAGE_NAME}" \
    pub.acps.infra.upstream.digest="${UPSTREAM_DIGEST}" \
    pub.acps.platform="${PLATFORM}"
