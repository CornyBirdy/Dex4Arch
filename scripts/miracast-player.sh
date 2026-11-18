#!/usr/bin/env bash
# ==============================================================================
# External player for miracle-sinkctl (started by it, as root).
#
# Drops root and runs scripts/miracast-viewer.py as the desktop user who started
# miracast-sink.sh, so the window, audio and notifications use that user's
# session. All arguments from miracle-sinkctl are passed through unchanged.
#
# If the user closes the viewer, this script asks miracle-sinkctl to exit, so
# miracast-sink.sh ends the session and restores Wi-Fi. If miracle-sinkctl
# stops the player itself (phone disconnected), the session is left running.
# ==============================================================================
set -u

DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
VIEWER="$DIR/miracast-viewer.py"
TARGET_USER="${MIRACAST_USER:-${SUDO_USER:-}}"

log() { printf 'player: %s\n' "$*"; }

if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = root ]; then
    log "Cannot tell which desktop user to run the viewer as (MIRACAST_USER/SUDO_USER unset)."
    exit 1
fi

uid="$(id -u "$TARGET_USER")" || exit 1
gid="$(id -g "$TARGET_USER")" || exit 1
home="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
runtime="/run/user/$uid"

env_args=(
    "DISPLAY=${DISPLAY:-:0}"
    "XAUTHORITY=${XAUTHORITY:-$home/.Xauthority}"
    "HOME=$home"
    "USER=$TARGET_USER"
    "LOGNAME=$TARGET_USER"
    "PATH=/usr/bin:/bin:/usr/local/bin"
    "LANG=${LANG:-C.UTF-8}"
)
if [ -d "$runtime" ]; then
    env_args+=("XDG_RUNTIME_DIR=$runtime" "DBUS_SESSION_BUS_ADDRESS=unix:path=$runtime/bus")
fi

# The distribution's Python has the GTK/GStreamer bindings; a custom python3 in
# /usr/local usually does not.
PYTHON="${MIRACAST_PYTHON:-}"
if [ -z "$PYTHON" ]; then
    if [ -x /usr/bin/python3 ]; then PYTHON=/usr/bin/python3; else PYTHON=python3; fi
fi

# Viewer options chosen in miracast-sink.sh (--decoder, --mouse-speed, --windowed).
extra=()
if [ -n "${MIRACAST_VIEWER_OPTS:-}" ]; then
    read -r -a extra <<< "$MIRACAST_VIEWER_OPTS"
fi

stopping=0
child=""
on_signal() {
    stopping=1
    if [ -n "$child" ]; then kill -TERM "$child" 2>/dev/null; fi
}
trap on_signal TERM INT HUP

log "starting viewer as $TARGET_USER with: $* ${extra[*]}"
setpriv --reuid="$uid" --regid="$gid" --init-groups \
    env -i "${env_args[@]}" "$PYTHON" "$VIEWER" "$@" "${extra[@]}" &
child=$!

rc=0
while :; do
    wait "$child"
    rc=$?
    kill -0 "$child" 2>/dev/null || break
done
log "viewer exited with code $rc"

if [ "$stopping" -eq 0 ] && [ "$rc" -eq 0 ]; then
    log "viewer closed by the user: asking miracle-sinkctl (pid $PPID) to end the session"
    kill -TERM "$PPID" 2>/dev/null
fi
exit "$rc"
