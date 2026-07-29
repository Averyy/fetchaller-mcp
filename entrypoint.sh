#!/bin/sh
# Support PUID/PGID for Unraid and other NAS platforms.
# Defaults match Unraid's nobody:users (99:100).
set -eu

HTTP_STARTUP_TIMEOUT=${HTTP_STARTUP_TIMEOUT:-180}
APP_SHUTDOWN_TIMEOUT=${APP_SHUTDOWN_TIMEOUT:-60}
for timeout_value in "$HTTP_STARTUP_TIMEOUT" "$APP_SHUTDOWN_TIMEOUT"; do
    case "$timeout_value" in
    ""|*[!0-9]*|0|0*)
        echo "HTTP_STARTUP_TIMEOUT and APP_SHUTDOWN_TIMEOUT must be positive decimal integers without leading zeros" >&2
        exit 1
        ;;
    esac
    if [ "$timeout_value" -gt 3600 ]; then
        echo "HTTP_STARTUP_TIMEOUT and APP_SHUTDOWN_TIMEOUT must not exceed 3600 seconds" >&2
        exit 1
    fi
done

if [ "$(id -u)" = "0" ]; then
    PUID=${PUID:-99}
    PGID=${PGID:-100}
    for identity in "$PUID" "$PGID"; do
        case "$identity" in
        ""|*[!0-9]*|0*)
            echo "PUID and PGID must be positive decimal integers without leading zeros" >&2
            exit 1
            ;;
        esac
        if [ "$identity" -gt 2147483647 ]; then
            echo "PUID and PGID must be between 1 and 2147483647" >&2
            exit 1
        fi
    done

    # Adjust appuser UID/GID if overridden
    if [ "$(id -u appuser)" != "$PUID" ]; then
        usermod -o -u "$PUID" appuser >/dev/null 2>&1
    fi
    if [ "$(id -g appuser)" != "$PGID" ]; then
        groupmod -o -g "$PGID" appuser >/dev/null 2>&1
        # groupmod changes /etc/group but does not portably rewrite the primary
        # GID stored for appuser in /etc/passwd.
        usermod -g "$PGID" appuser >/dev/null 2>&1
    fi

    # Fix data directory ownership
    chown -R appuser:appuser /app/data

    # Re-enter as the final UID before starting Xvfb so neither its socket nor
    # process is root-owned. tini remains PID 1 and reaps the Xvfb child.
    exec gosu appuser "$0" "$@"
fi

# wafer's BrowserSolver launches headful Chrome, so create a fresh virtual
# display. Container restarts can leave a stale socket/lock in /tmp; remove the
# two exact files for our private display before launch.
XVFB_DISPLAY="${DISPLAY:-:99}"
XVFB_NUMBER="${XVFB_DISPLAY#:}"
XVFB_NUMBER="${XVFB_NUMBER%%.*}"
case "$XVFB_NUMBER" in
    ""|*[!0-9]*)
        echo "DISPLAY must be a local numeric display such as :99" >&2
        exit 1
        ;;
esac
XVFB_SOCKET="/tmp/.X11-unix/X${XVFB_NUMBER}"
XVFB_LOCK="/tmp/.X${XVFB_NUMBER}-lock"
rm -f "$XVFB_SOCKET" "$XVFB_LOCK"
Xvfb "$XVFB_DISPLAY" -screen 0 1920x1080x24 -ac -nolisten tcp >/dev/null 2>&1 &
XVFB_PID=$!
JWM_PID=
printf '%s\n' "$XVFB_PID" > /tmp/fetchaller-xvfb.pid

cleanup_display() {
    if [ -n "$JWM_PID" ]; then
        kill -TERM "$JWM_PID" 2>/dev/null || true
    fi
    kill -TERM "$XVFB_PID" 2>/dev/null || true
    cleanup_elapsed=0
    while { { [ -n "$JWM_PID" ] && kill -0 "$JWM_PID" 2>/dev/null; } \
        || kill -0 "$XVFB_PID" 2>/dev/null; } \
        && [ "$cleanup_elapsed" -lt 5 ]; do
        sleep 1
        cleanup_elapsed=$((cleanup_elapsed + 1))
    done
    if [ -n "$JWM_PID" ]; then
        kill -KILL "$JWM_PID" 2>/dev/null || true
        wait "$JWM_PID" 2>/dev/null || true
    fi
    kill -KILL "$XVFB_PID" 2>/dev/null || true
    wait "$XVFB_PID" 2>/dev/null || true
}

i=0
while [ ! -S "$XVFB_SOCKET" ] && [ "$i" -lt 50 ]; do
    if ! kill -0 "$XVFB_PID" 2>/dev/null; then
        echo "Xvfb exited before creating $XVFB_SOCKET" >&2
        cleanup_display
        exit 1
    fi
    i=$((i + 1))
    sleep 0.1
done
if [ ! -S "$XVFB_SOCKET" ]; then
    echo "Xvfb failed to create display socket for $XVFB_DISPLAY" >&2
    cleanup_display
    exit 1
fi

# Chrome's headful window is part of the browser fingerprint.  Xvfb provides
# the display but deliberately does not manage windows, which leaves Chrome
# with implausible outer/inner-window metrics.  JWM is a small X11 window
# manager; verify its configuration before launching so a missing dependency
# cannot silently downgrade the browser environment.
if ! jwm -p >/dev/null 2>&1; then
    echo "JWM configuration validation failed" >&2
    cleanup_display
    exit 1
fi
jwm >/dev/null 2>&1 &
JWM_PID=$!
printf '%s\n' "$JWM_PID" > /tmp/fetchaller-jwm.pid
claimed=0
i=0
while [ "$i" -lt 50 ]; do
    if ! kill -0 "$JWM_PID" 2>/dev/null; then
        echo "JWM exited before managing $XVFB_DISPLAY" >&2
        cleanup_display
        exit 1
    fi
    # PID liveness only proves JWM started. EWMH ownership proves it claimed
    # the display before Chrome creates a window. Cold emulated CI can take
    # longer than a fixed sleep to publish that property.
    if xprop -root _NET_SUPPORTING_WM_CHECK 2>/dev/null | grep -q 'WINDOW'; then
        claimed=1
        break
    fi
    i=$((i + 1))
    sleep 0.1
done
if [ "$claimed" -ne 1 ]; then
    echo "JWM did not claim the window-manager selection on $XVFB_DISPLAY" >&2
    cleanup_display
    exit 1
fi
echo "JWM claimed the window-manager selection on $XVFB_DISPLAY" >&2

# Keep the MCP process as the supervised workload instead of replacing this
# shell. Docker's restart policy reacts to a container exit, not to an
# unhealthy healthcheck. If either required display process dies, terminate
# the MCP process cleanly and exit non-zero so `restart: unless-stopped` can
# restore the browser-complete service. On an operator stop, leave Xvfb/JWM
# alive until the MCP process has closed BrowserSolver, then reap everything.
APP_PID=
STOPPING=0
STARTUP_FAILED=0
APP_FORCE_KILLED=0

HTTP_MODE=0
for arg in "$@"; do
    if [ "$arg" = "--http" ]; then
        HTTP_MODE=1
        break
    fi
done
STARTUP_DEADLINE=$(($(date +%s) + HTTP_STARTUP_TIMEOUT))
STARTUP_READY=0

forward_shutdown() {
    STOPPING=1
    if [ -n "$APP_PID" ]; then
        kill -TERM "$APP_PID" 2>/dev/null || true
    fi
}

trap forward_shutdown TERM INT HUP

reap_app_bounded() {
    if kill -0 "$APP_PID" 2>/dev/null; then
        kill -TERM "$APP_PID" 2>/dev/null || true
        shutdown_elapsed=0
        while kill -0 "$APP_PID" 2>/dev/null \
            && [ "$shutdown_elapsed" -lt "$APP_SHUTDOWN_TIMEOUT" ]; do
            sleep 1
            shutdown_elapsed=$((shutdown_elapsed + 1))
        done
        if kill -0 "$APP_PID" 2>/dev/null; then
            echo "Application ignored TERM for ${APP_SHUTDOWN_TIMEOUT}s; forcing exit" >&2
            APP_FORCE_KILLED=1
            kill -KILL "$APP_PID" 2>/dev/null || true
        fi
    fi
    wait "$APP_PID" || APP_STATUS=$?
}

# POSIX shells may attach /dev/null to fd 0 of an asynchronous command before
# applying its redirections. Save Docker stdin on fd 3 while still foreground,
# then restore that saved descriptor for the supervised stdio MCP child.
exec 3<&0
"$@" <&3 &
APP_PID=$!
exec 3<&-
APP_STATUS=0
DISPLAY_FAILED=0

while kill -0 "$APP_PID" 2>/dev/null; do
    if ! kill -0 "$XVFB_PID" 2>/dev/null \
        || ! kill -0 "$JWM_PID" 2>/dev/null; then
        echo "Required browser display process exited; restarting service" >&2
        DISPLAY_FAILED=1
        kill -TERM "$APP_PID" 2>/dev/null || true
        break
    fi
    if [ "$HTTP_MODE" -eq 1 ] && [ "$STARTUP_READY" -eq 0 ]; then
        startup_remaining=$((STARTUP_DEADLINE - $(date +%s)))
        if [ "$startup_remaining" -le 0 ]; then
            echo "HTTP startup exceeded ${HTTP_STARTUP_TIMEOUT}s; restarting service" >&2
            STARTUP_FAILED=1
            kill -TERM "$APP_PID" 2>/dev/null || true
            break
        fi
        probe_timeout=2
        if [ "$startup_remaining" -lt "$probe_timeout" ]; then
            probe_timeout=$startup_remaining
        fi
        if curl -fsS \
            --connect-timeout "$probe_timeout" \
            --max-time "$probe_timeout" \
            "http://localhost:${HTTP_PORT:-6000}/health" >/dev/null 2>&1; then
            STARTUP_READY=1
            echo "HTTP readiness confirmed" >&2
        fi
    fi
    sleep 1 &
    SLEEP_PID=$!
    wait "$SLEEP_PID" 2>/dev/null || true
    if [ "$STOPPING" -eq 1 ]; then
        break
    fi
done

reap_app_bounded
cleanup_display

if [ "$DISPLAY_FAILED" -eq 1 ] || [ "$STARTUP_FAILED" -eq 1 ] \
    || [ "$APP_FORCE_KILLED" -eq 1 ]; then
    exit 1
fi
if [ "$STOPPING" -eq 1 ]; then
    # A supervised child commonly reports 128+SIGTERM even after Uvicorn has
    # run its lifespan cleanup. The operator requested this stop; after wait()
    # and display reaping above, expose it as the clean container shutdown it
    # is. Dependency/startup failures keep their non-zero paths above.
    exit 0
fi
exit "$APP_STATUS"
