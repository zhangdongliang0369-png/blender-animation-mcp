#!/bin/sh
set -eu
if [ "$(id -u)" = 0 ]; then
    if [ -n "${BLENDER_V2_DIR:-}" ]; then
        case "$BLENDER_V2_DIR" in
            /data|/data/*) ;;
            *) echo "BLENDER_V2_DIR must be below /data" >&2; exit 1 ;;
        esac
        if [ "${BLENDER_V2_REQUIRE_MOUNT:-1}" = 1 ] && ! mountpoint -q /data; then
            echo "Mount a persistent volume at /data before enabling V2" >&2
            exit 1
        fi
        install -d -m 0700 -o blenderapi -g blenderapi "$BLENDER_V2_DIR"
    fi
    exec setpriv --reuid=blenderapi --regid=blenderapi --init-groups "$@"
fi
exec "$@"
