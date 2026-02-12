#!/bin/sh
# Support PUID/PGID for Unraid and other NAS platforms.
# Defaults match Unraid's nobody:users (99:100).

if [ "$(id -u)" = "0" ]; then
    PUID=${PUID:-99}
    PGID=${PGID:-100}

    # Adjust appuser UID/GID if overridden
    if [ "$(id -u appuser)" != "$PUID" ]; then
        usermod -o -u "$PUID" appuser 2>/dev/null
    fi
    if [ "$(id -g appuser)" != "$PGID" ]; then
        groupmod -o -g "$PGID" appuser 2>/dev/null
    fi

    # Fix data directory ownership
    chown appuser:appuser /app/data

    exec gosu appuser "$@"
else
    # Already non-root (e.g., forced by container runtime)
    exec "$@"
fi
