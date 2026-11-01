FROM --platform=$BUILDPLATFORM node:24-alpine@sha256:8a32c8cf649a49915a66e81e7a609223c708d0adabf288ee3f4077d765542f85 AS static-builder

# 固定镜像仍需更新同发行版安全补丁（CA bundle、zlib），再执行 npm。
RUN apk upgrade --no-cache

WORKDIR /src

COPY package.json package-lock.json ./
RUN npm install --global npm@12.2.1 --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org
RUN npm ci --ignore-scripts --no-audit --no-fund

COPY packaging/scripts/minify_static.mjs /src/packaging/scripts/minify_static.mjs
COPY app/static /src/app/static
RUN npm run build:static -- --input /src/app/static --output /out

FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENV=production \
    MEDIAFLUX_CONTAINER=1 \
    MEDIAFLUX_DATA_DIR=/app/db \
    MEDIAFLUX_CONFIG_DIR=/app/db \
    MEDIAFLUX_CACHE_DIR=/app/db/cache \
    MEDIAFLUX_LOG_DIR=/app/db/logs \
    MEDIAFLUX_STRM_DIR=/data/strm \
    MEDIAFLUX_FFPROBE=/usr/bin/ffprobe

WORKDIR /app

# 本地与云端媒体规格探测依赖 ffprobe；Debian 的 ffmpeg 包同时提供该命令。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg gosu \
    && /usr/bin/ffprobe -version >/dev/null 2>&1 \
    && gosu --version >/dev/null 2>&1 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 mediaflux \
    && useradd --system --uid 10001 --gid mediaflux --home-dir /app mediaflux

COPY requirements-release-runtime.lock /app/requirements-release-runtime.lock
RUN pip install --no-cache-dir --require-hashes -r /app/requirements-release-runtime.lock

COPY app /app/app
COPY --from=static-builder /out/ /app/app/static/
COPY mediaflux.py /app/
COPY packaging/scripts/docker-entrypoint.sh /usr/local/bin/mediaflux-entrypoint
COPY packaging/scripts/generate_build_info.py /tmp/generate_build_info.py

# 版本信息仅影响元数据层，不能让每次提交都重装系统包和 Python 依赖。
ARG VERSION_REF=v0.0.0-dev
ARG GIT_SHA=development
ARG SOURCE_DATE_EPOCH
ARG TARGETARCH

RUN case "${TARGETARCH:-amd64}" in \
      amd64) MEDIAFLUX_ARCH=x86_64 ;; \
      arm64) MEDIAFLUX_ARCH=aarch64 ;; \
      *) echo "unsupported Docker architecture: $TARGETARCH" >&2; exit 2 ;; \
    esac \
    && python /tmp/generate_build_info.py \
      --ref "$VERSION_REF" --commit "$GIT_SHA" \
      --platform linux --arch "$MEDIAFLUX_ARCH" --package docker \
      --output /app/app/_build_info.json \
    && chown -R root:root /app/app /app/mediaflux.py \
    && chmod -R a-w /app/app /app/mediaflux.py \
    && chmod 0444 /app/app/_build_info.json \
    && rm -f /tmp/generate_build_info.py

RUN mkdir -p /app/db/cache /app/db/logs /data/strm \
    && chown -R mediaflux:mediaflux /app/db /data/strm \
    && chmod 0755 /usr/local/bin/mediaflux-entrypoint

EXPOSE 1258

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:1258/readyz', timeout=3)" >/dev/null 2>&1

# 入口初始化 MediaFlux 自有目录；默认兼容 NAS 权限，也支持显式 PUID/PGID 降权。
# 非 root 模式只在首次/UID 变化/显式请求时递归迁移自有数据，不扫描业务媒体挂载。
ENTRYPOINT ["/usr/local/bin/mediaflux-entrypoint"]

# 后台调度器与 Telegram Bot 采用进程内单例，必须保持单 worker。
CMD ["python", "mediaflux.py", "start"]
