#!/bin/sh
set -eu

is_enabled() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

require_numeric_id() {
    name="$1"
    value="$2"
    case "$value" in
        ''|*[!0-9]*)
            echo "MediaFlux container: $name must be a non-negative integer" >&2
            exit 2
            ;;
    esac
}

mkdir -p /app/db/cache /app/db/logs /data/strm

# 官方镜像内部端口是稳定契约；宿主机端口只通过 Docker 的端口映射调整。
# 备份恢复的 user.env 可能来自本机部署，不能让其中的 WEB_PORT 改变容器
# 监听端口并使固定 1258 的 healthcheck 永久失败。
export WEB_PORT=1258

# 显式指定 Docker user 时，尊重调用方预先准备的权限。
if [ "$(id -u)" -ne 0 ]; then
    exec "$@"
fi

# 家庭服务器默认使用兼容模式，避免宿主机、qB 与媒体服务器 UID/GID
# 不一致导致挂载目录不可读写；需要非 root 时显式设为 0。
if is_enabled "${MEDIAFLUX_RUN_AS_ROOT:-1}"; then
    # root 模式可能生成 root-owned 数据；使旧的非 root owner marker 失效，
    # 保证以后切回相同 PUID/PGID 时仍会执行一次完整权限迁移。
    rm -f /app/db/.mediaflux-owner-*
    umask "${UMASK:-022}"
    exec "$@"
fi

puid="${PUID:-10001}"
pgid="${PGID:-10001}"
require_numeric_id PUID "$puid"
require_numeric_id PGID "$pgid"
if [ "$puid" -eq 0 ] || [ "$pgid" -eq 0 ]; then
    echo "MediaFlux container: PUID/PGID must be greater than 0; use MEDIAFLUX_RUN_AS_ROOT=1 for compatibility mode" >&2
    exit 2
fi

current_gid="$(id -g mediaflux)"
current_uid="$(id -u mediaflux)"
if [ "$current_gid" != "$pgid" ]; then
    groupmod --non-unique --gid "$pgid" mediaflux
fi
if [ "$current_uid" != "$puid" ]; then
    usermod --non-unique --uid "$puid" --gid "$pgid" mediaflux
fi

# 首次切换到某组 PUID/PGID 时迁移 MediaFlux 自有数据目录；后续启动只修复
# 三个固定目录，避免日志、缓存或备份增长后每次都递归扫描整棵挂载树。
data_owner_marker="/app/db/.mediaflux-owner-${puid}-${pgid}"
if [ ! -f "$data_owner_marker" ] || is_enabled "${MEDIAFLUX_FIX_DATA_PERMISSIONS:-0}"; then
    chown -R "$puid:$pgid" /app/db
    rm -f /app/db/.mediaflux-owner-*
    : > "$data_owner_marker"
    chown "$puid:$pgid" "$data_owner_marker"
else
    chown "$puid:$pgid" /app/db /app/db/cache /app/db/logs
fi

# STRM 大库默认只修复挂载根目录；显式迁移时才递归处理。
chown "$puid:$pgid" /data/strm
if is_enabled "${MEDIAFLUX_FIX_STRM_PERMISSIONS:-0}"; then
    chown -R "$puid:$pgid" /data/strm
fi

umask "${UMASK:-022}"
exec gosu "$puid:$pgid" "$@"
