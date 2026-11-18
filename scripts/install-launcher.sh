#!/usr/bin/env bash
# ==============================================================================
# Adds (or removes) a "Miracast Receiver (DeX)" entry in the desktop menu.
#
# The entry opens a terminal that runs scripts/miracast-sink.sh. The terminal is
# needed for the sudo password and shows the session report at the end; it
# waits for Enter before closing. Nothing outside your home folder is changed.
#
# Usage:  ./install-launcher.sh            install or update the menu entry
#         ./install-launcher.sh --remove   remove it
# ==============================================================================
set -euo pipefail

DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO="$(dirname "$DIR")"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ENTRY="$APPS/miracast-dex-receiver.desktop"

if [ "$EUID" -eq 0 ]; then
    echo "Run as your normal user, not root." >&2
    exit 1
fi

if [ "${1:-}" = "--remove" ]; then
    rm -f -- "$ENTRY"
    echo "Removed: $ENTRY"
    exit 0
fi

case "$REPO" in
    *[[:space:]\"\'\\\$\`%]*)
        echo "The project path contains spaces or special characters: $REPO" >&2
        echo "Move the project to a simpler path and run this again." >&2
        exit 1 ;;
esac

chmod +x "$DIR/miracast-sink.sh" "$DIR/miracast-player.sh" "$DIR/miracast-viewer.py"
mkdir -p "$APPS"
cat > "$ENTRY" <<DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=Miracast Receiver (DeX)
GenericName=Wireless display receiver
Comment=Receive Samsung DeX or screen mirroring from a phone over Miracast
Exec=$DIR/miracast-sink.sh --pause-at-exit
Path=$REPO
Icon=video-display
Terminal=true
Categories=AudioVideo;Video;Network;
Keywords=Miracast;DeX;Samsung;Smart View;Wireless display;Screen mirroring;
StartupNotify=false
DESKTOP
chmod 644 "$ENTRY"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS" >/dev/null 2>&1 || true
fi
echo "Installed: $ENTRY"
echo "Open the application menu and search for: Miracast"
