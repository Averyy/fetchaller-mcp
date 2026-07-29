#!/bin/sh
# Verify the HTTP service and the headful-browser display it advertises.
set -eu

display="${DISPLAY:-:99}"
number="${display#:}"
number="${number%%.*}"
case "$number" in
    ""|*[!0-9]*) exit 1 ;;
esac

check_pid() {
    file=$1
    [ -r "$file" ] || exit 1
    pid=$(cat "$file")
    case "$pid" in
        ""|*[!0-9]*) exit 1 ;;
    esac
    kill -0 "$pid" 2>/dev/null || exit 1
}

check_pid /tmp/fetchaller-xvfb.pid
check_pid /tmp/fetchaller-jwm.pid
[ -S "/tmp/.X11-unix/X${number}" ] || exit 1
xprop -root _NET_SUPPORTING_WM_CHECK 2>/dev/null | grep -q 'WINDOW'
curl -fsS "http://localhost:${HTTP_PORT:-6000}/health" >/dev/null
