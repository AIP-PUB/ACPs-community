# syntax=docker/dockerfile:1
# 通用 Python 应用镜像 Dockerfile。
#
# 故意不使用远端 frontend 之外的隐藏网络依赖：只依赖当前 Docker/BuildKit 内建的
# `# syntax=docker/dockerfile:1` 稳定别名（解析到已缓存的官方 dockerfile frontend），
# 不锁定具体 frontend digest——与 release/assembly/Dockerfile 的策略略有不同，
# 因为这里必须使用 BuildKit 的 `RUN --mount=type=bind`（见下）才能保证 wheelhouse
# 不进入任何镜像 layer，属于不可替代的 BuildKit 专属能力。
#
# 用法（由 image-packaging/build-app-image.sh 编排调用，构建上下文见该脚本生成的
# build-context/ 目录，须同时包含 app-release/、runtime-app/、app-run.sh、
# Dockerfile、entrypoint.sh）：
# docker buildx build --platform linux/arm64 \
# --build-arg PYTHON_RUNTIME_IMAGE=python:3.14-slim@sha256:... \
# --build-arg APP_ID=registry-server --build-arg APP_VERSION=2.2.0 \
# --build-arg APP_WHEEL=registry_server-2.2.0-py3-none-any.whl \
# --build-arg APP_RELEASE_FILE=registry-server-linux-arm64-cp314-app-release-2.2.0.tar.gz \
# --build-arg APP_RELEASE_SHA256=<sha256> \
# --build-arg APP_PLATFORM=linux/arm64 --build-arg PYTHON_TAG=cp314 \
# -t acps/registry-server:2.2.0-linux-arm64 \
# --output type=docker,dest=out.image.tar -f Dockerfile build-context/

ARG PYTHON_RUNTIME_IMAGE
FROM ${PYTHON_RUNTIME_IMAGE} AS runtime

# 重新声明同名 ARG 以便在 FROM 之后的指令（LABEL）里引用其值；显式给一个空字符串
# 默认值只是为了消除 BuildKit lint 对 "ARG 与 FROM 同名但无默认值" 的误报
# （InvalidDefaultArgInFrom），实际值总是由 --build-arg 在构建时提供，不依赖这个默认值。
ARG PYTHON_RUNTIME_IMAGE=""
ARG APP_ID
ARG APP_VERSION
ARG APP_WHEEL
ARG APP_RELEASE_FILE
ARG APP_RELEASE_SHA256
ARG APP_PLATFORM
ARG PYTHON_TAG
ARG VARIANT=""
ARG APP_WHEEL_EXTRA=""
ARG IMAGE_CREATED=""
ARG SOURCE_REVISION=""

WORKDIR /opt/acps

# groupadd/useradd/mkdir 是幂等兜底：正式的 Python runtime 基础镜像应该已经内置这些
# 用户和目录约定；这里保留只是为了在 runtime 基础镜像尚未跟上约定时
# 不阻塞构建，用户/目录初始化的权责仍然归属 runtime 基础镜像。
RUN (getent group acps >/dev/null || groupadd --system acps) \
    && (id -u acps >/dev/null 2>&1 || useradd --system --gid acps --home-dir /opt/acps --shell /usr/sbin/nologin acps) \
    && mkdir -p /opt/acps/bin /var/lib/acps /var/log/acps /run/acps

COPY runtime-app/ /opt/acps/app/
COPY app-release/build-manifest.toml /opt/acps/build-manifest.toml
COPY app-run.sh /opt/acps/bin/app-run
COPY entrypoint.sh /usr/local/bin/acps-entrypoint
COPY normalize_installed_wheels.py /usr/local/bin/acps-normalize-installed-wheels

# wheelhouse 只通过 BuildKit bind mount 临时挂载给这一次 RUN 使用，不经过 COPY
# 写进任何镜像层——如果先 COPY 再在后续指令里 rm -rf，wheelhouse 内容依然会保留在
# COPY 产生的那一层里（Docker 镜像层是增量叠加的，后面的层删除文件只是在合并视图里
# 不可见，导出的镜像数据体积并不会真正减小）。用 --mount=type=bind 从根本上避免
# 这个问题：wheelhouse 内容永远不出现在任何一层里（ 关键约束）。
#
# APP_WHEEL_EXTRA 只在目标 variant 声明了对应 pip extra 时才非空（见
# startup-strategies.toml 的 variant_pip_extras）。例如 discovery-server 的
# GPU 本地模型推理栈（torch/FlagEmbedding 等）在 pyproject.toml 里是
# optional-dependencies.gpu，不是硬依赖——单纯 `pip install app_wheel` 不会拉入
# 这些包，即使它们已经在 wheelhouse/ 中；必须安装 "app_wheel[gpu]" 才能让 pip
# 解析到这组 extras。
#
# 另一个已知上游问题：当前 `nvidia-cusparselt-cu13` 的 aarch64 wheel 文件名是
# manylinux2014_aarch64，但内嵌 dist-info/WHEEL Tag 却写成了
# manylinux2014_sbsa。`pip install` 仍能成功，但后续 `pip check` 会把这个已安装
# wheel 判定为"当前平台不支持"。这里在离线安装后、`pip check` 前执行一个极窄的
# 元数据归一化脚本，只修正这个已知 vendor wheel 的已安装 WHEEL Tag。
RUN --mount=type=bind,source=app-release/wheelhouse,target=/mnt/wheelhouse \
    python -m venv /opt/acps/venv \
    && wheel_spec="/mnt/wheelhouse/${APP_WHEEL}" \
    && if [ -n "${APP_WHEEL_EXTRA}" ]; then wheel_spec="${wheel_spec}[${APP_WHEEL_EXTRA}]"; fi \
    && /opt/acps/venv/bin/pip install --no-index \
    --find-links=/mnt/wheelhouse \
    "${wheel_spec}" \
    && /opt/acps/venv/bin/python /usr/local/bin/acps-normalize-installed-wheels \
    && /opt/acps/venv/bin/pip check \
    && chmod +x /opt/acps/bin/app-run /usr/local/bin/acps-entrypoint \
    && chown -R acps:acps /opt/acps /var/lib/acps /var/log/acps /run/acps \
    && rm -rf /root/.cache/pip

ENV PATH="/opt/acps/venv/bin:${PATH}"
ENV ACPS_APP_ID="${APP_ID}"
ENV ACPS_APP_ROOT="/opt/acps/app"

LABEL org.opencontainers.image.title="${APP_ID}" \
    org.opencontainers.image.version="${APP_VERSION}" \
    org.opencontainers.image.revision="${SOURCE_REVISION}" \
    org.opencontainers.image.created="${IMAGE_CREATED}" \
    pub.acps.image.kind="app" \
    pub.acps.app.id="${APP_ID}" \
    pub.acps.app.release.file="${APP_RELEASE_FILE}" \
    pub.acps.app.release.sha256="${APP_RELEASE_SHA256}" \
    pub.acps.platform="${APP_PLATFORM}" \
    pub.acps.python_tag="${PYTHON_TAG}" \
    pub.acps.variant="${VARIANT}" \
    pub.acps.runtime.python_image="${PYTHON_RUNTIME_IMAGE}"

WORKDIR /opt/acps/app
USER acps:acps
ENTRYPOINT ["/usr/local/bin/acps-entrypoint"]
CMD ["/opt/acps/bin/app-run"]
