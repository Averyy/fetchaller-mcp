#!/bin/sh
# Support PUID/PGID for Unraid and other NAS platforms.
# Defaults match Unraid's nobody:users (99:100).

# Start a virtual X display for the browser solver.
#
# wafer's BrowserSolver launches Chrome HEADFUL on purpose ("headless = 16.7%
# bypass rate" per its source), so a container with no X server cannot solve
# challenges at all. Xvfb gives Chrome a display to attach to.
#
# DISPLAY comes from the image (ENV DISPLAY=:99) rather than being exported
# here, so every process in the container agrees on it — including `docker exec`
# sessions, which do NOT inherit anything this script exports. Serving that
# display is this script's job.
if command -v Xvfb >/dev/null 2>&1; then
    XVFB_DISPLAY="${DISPLAY:-:99}"
    if [ ! -e "/tmp/.X11-unix/X${XVFB_DISPLAY#:}" ]; then
        # -ac disables X access control: Xvfb starts as root here but Chrome
        # connects as appuser, and without it the connection is refused.
        Xvfb "$XVFB_DISPLAY" -screen 0 1920x1080x24 -ac -nolisten tcp >/dev/null 2>&1 &
        # Wait for the socket rather than sleeping blind.
        i=0
        while [ ! -e "/tmp/.X11-unix/X${XVFB_DISPLAY#:}" ] && [ $i -lt 50 ]; do
            i=$((i + 1))
            sleep 0.1
        done
    fi
    DISPLAY="$XVFB_DISPLAY"
    export DISPLAY
fi

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
