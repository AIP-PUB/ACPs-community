# syntax=docker/dockerfile:1
# postgres-pgvector 派生镜像。
#
# 基于 postgres:17-bookworm 通过 apt（PGDG 官方仓库，随基础镜像预置）安装 pgvector
# 扩展文件，让扩展在镜像内可用；真正对某个数据库执行 `CREATE EXTENSION vector` 属于
# 数据库初始化/迁移阶段的动作，不是镜像构建阶段的动作，不在这里执行。
#
# 非 FROM 输入的版本固定（ 要求）：postgresql-17-pgvector 包版本
# 通过 `apt-cache policy postgresql-17-pgvector` 在目标基础镜像上探测得到，
# 2026-07-05 对 postgres:17-bookworm@sha256:17b6c778de50f4bb9a878c36e736110fbcd9b7020377d6fdfdf20f7c0347e40a
# 探测结果为 0.8.4-1.pgdg12+1（来自基础镜像内置的 PGDG apt 源）。更新基础镜像
# digest 时应重新探测并更新此处版本号，避免"只锁 FROM digest、不锁 apt 包版本"
# 导致派生镜像内容漂移。
#
# 用法：
# docker buildx build --platform linux/arm64 \
# --build-arg UPSTREAM_IMAGE=postgres:17-bookworm@sha256:... \
# --build-arg INFRA_ID=postgres-pgvector --build-arg UPSTREAM_VERSION=17-bookworm \
# --build-arg ACPS_VERSION=2.2.0 --build-arg PLATFORM=linux/arm64 \
# -t acps/postgres-pgvector:17-bookworm-2.2.0-linux-arm64 \
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

# 固定版本，不使用裸 `apt-get install postgresql-17-pgvector`（会随 PGDG 源内容漂移）。
ARG PGVECTOR_APT_VERSION="0.8.4-1.pgdg12+1"

RUN apt-get update \
    && apt-get install -y --no-install-recommends "postgresql-17-pgvector=${PGVECTOR_APT_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

LABEL org.opencontainers.image.title="${INFRA_ID}" \
    org.opencontainers.image.version="${UPSTREAM_VERSION}-${ACPS_VERSION}" \
    org.opencontainers.image.created="${IMAGE_CREATED}" \
    pub.acps.image.kind="infra" \
    pub.acps.infra.id="${INFRA_ID}" \
    pub.acps.infra.upstream.image="${UPSTREAM_IMAGE_NAME}" \
    pub.acps.infra.upstream.digest="${UPSTREAM_DIGEST}" \
    pub.acps.infra.pgvector.apt_version="${PGVECTOR_APT_VERSION}" \
    pub.acps.platform="${PLATFORM}"
