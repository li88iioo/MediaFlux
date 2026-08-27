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

# 显式指定 Docker user 时，尊重调用方预先准备的权限。
if [ "$(id -u)" -ne 0 ]; then
    exec "$@"
fi

# 家庭服务器默认使用兼容模式，避免宿主机、qB 与媒体服务器 UID/GID
# 不一致导致挂载目录不可读写；需要非 root 时显式设为 0。
if is_enabled "${MEDIAFLUX_RUN_AS_ROOT:-1}"; then
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

# 数据库目录体积小且完全由 MediaFlux 管理，可以安全自动修复；STRM 大库默认
# 只修复挂载根目录，避免每次启动递归遍历数万文件。
chown -R "$puid:$pgid" /app/db
chown "$puid:$pgid" /data/strm
if is_enabled "${MEDIAFLUX_FIX_STRM_PERMISSIONS:-0}"; then
    chown -R "$puid:$pgid" /data/strm
fi

umask "${UMASK:-022}"
exec gosu "$puid:$pgid" "$@"
