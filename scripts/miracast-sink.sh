#!/usr/bin/env bash
# ==============================================================================
# Miracast receiver experiment (Wi-Fi Direct sink via MiracleCast)
#
# EXPERIMENTAL. Turns this PC into a Miracast receiver so the phone's Smart View
# (and later Wireless DeX) can find it. On exit it prints a short report that
# says how far the attempt got (discovery, negotiation, group, stream).
#
# WARNING: Wi-Fi on the chosen adapter drops while this runs. The adapter is
# released from NetworkManager and the system wpa_supplicant service is stopped,
# because miracle-wifid starts its own wpa_supplicant. Both are restored on exit
# (viewer closed, "quit", error, terminal close). Nothing is changed permanently:
# a reboot also restores everything.
#
# Requires: miraclecast-git (AUR), networkmanager, iw, wpa_supplicant (wpa_cli).
# Run as a normal user; sudo is used where root is needed.
#
# Usage:  ./miracast-sink.sh [--check] [--iface wlan0] [--name Arch-Miracast]
#                            [--no-audio] [--no-input] [--pause-at-exit]
#   --check          read-only preflight: prints facts, changes nothing, Wi-Fi stays up
#   --no-audio       do not play the phone's audio on the laptop
#   --no-input       plain MiracleCast player: no full screen viewer, no mouse/keyboard
#   --res MODE       resolution offered to the phone: auto (default, fits this
#                    screen), WxH (for example 1280x720), or all (MiracleCast default)
#   --max-fps N      highest frame rate offered: 60 (default) or 30 (lighter on the CPU)
#   --decoder NAME   H.264 decoder for the viewer: auto (default: hardware if
#                    available), vah264dec, vaapih264dec or avdec_h264 (software)
#   --mouse-speed X  multiply mouse movement sent to the phone (default 1.0)
#   --windowed       start the viewer in a window instead of full screen
#   --pause-at-exit  wait for Enter before the window closes (used by the menu launcher)
#
# Stop:   close the viewer (Ctrl+Alt to release the mouse, then Ctrl+Q), or type
#         quit in this terminal and press Enter.
#
# Files:  ./miracast-sink.log          raw log, contains MAC addresses: do not publish
#         ./miracast-sink-report.log   the report, MAC/IP redacted
# ==============================================================================
set -uo pipefail

IFACE=""
NAME="Arch-Miracast"
AUDIO=1
INPUT=1
PAUSE_AT_EXIT=0
RES_MODE="auto"
MAX_FPS=60
VIEWER_OPTS=()
RES_INFO="MiracleCast default"
CHECK_ONLY=0
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
CONF_HOME=""
LOG="$PWD/miracast-sink.log"
REPORT="$PWD/miracast-sink-report.log"
CTRL_DIR="/run/miracle/wifi"
MAC_RE='([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}'

STARTED=0          # set once something was changed and cleanup is needed
CLEANED=0
NM_CHANGED=0
WPA_STOPPED=0
WPA_MASKED=0
FW_PORT_ADDED=0
FW_DHCP_ADDED=0
FW_STATE="not running"
STALE_P2P="n/a"
SAMPLER_PID=""
LINK_ID=""
IFINDEX=""
GLOBAL=""
WDEV=""
START_EPOCH="$(date +%s)"

while [ $# -gt 0 ]; do
    case "$1" in
        --iface) IFACE="${2:-}"; shift 2 ;;
        --name)  NAME="${2:-}"; shift 2 ;;
        --audio) AUDIO=1; shift ;;
        --no-audio) AUDIO=0; shift ;;
        --no-input) INPUT=0; shift ;;
        --res) RES_MODE="${2:-}"; shift 2 ;;
        --max-fps) MAX_FPS="${2:-}"; shift 2 ;;
        --decoder) VIEWER_OPTS+=(--decoder "${2:-auto}"); shift 2 ;;
        --mouse-speed) VIEWER_OPTS+=(--mouse-speed "${2:-1.0}"); shift 2 ;;
        --windowed) VIEWER_OPTS+=(--windowed); shift ;;
        --pause-at-exit) PAUSE_AT_EXIT=1; shift ;;
        --check) CHECK_ONLY=1; shift ;;
        -h|--help) sed -n '2,37p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

say()    { printf '[miracast] %s\n' "$*"; }
die()    { printf '[miracast][ERROR] %s\n' "$*" >&2; exit 1; }
have()   { command -v "$1" >/dev/null 2>&1; }
redact() { sed -E -e "s/$MAC_RE/<MAC>/g" -e 's/([0-9]{1,3}\.){3}[0-9]{1,3}/<IP>/g'; }
yesno()  { if "$@" >/dev/null 2>&1; then echo yes; else echo no; fi; }

# wpa_cli against the wpa_supplicant that miracle-wifid spawned.
wpa_dev()  { sudo --non-interactive wpa_cli -p "$CTRL_DIR" -i "$WDEV" "$@" 2>/dev/null; }
wpa_glob() { sudo --non-interactive wpa_cli -g "$GLOBAL" "$@" 2>/dev/null; }
p2p_wdev_present() { iw dev 2>/dev/null | grep -q 'type P2P-device'; }
miracle_wpa_alive() { pgrep -f 'wpa_supplicant.*[/]run/miracle' >/dev/null 2>&1; }
# The NetworkManager-owned wpa_supplicant runs in D-Bus mode (-u) and is not under /run/miracle.
system_wpa_alive() { pgrep -f '^[^ ]*wpa_supplicant .*-u' >/dev/null 2>&1; }
step() { echo "STEP $(date +%s) $*" >> "$LOG"; }

[ "$EUID" -ne 0 ] || die "Run as a normal user, not root (the video window needs your desktop session)."

for bin in miracle-wifid miracle-wifictl miracle-sinkctl nmcli iw wpa_cli; do
    have "$bin" || die "'$bin' not found. Install: yay -S miraclecast-git   and   sudo pacman -S networkmanager iw wpa_supplicant"
done

if [ -z "$IFACE" ]; then
    IFACE="$(iw dev | awk '$1=="Interface" && $2 !~ /^p2p/ {print $2; exit}')"
fi
[ -n "$IFACE" ] || die "No Wi-Fi interface found. Pass one with --iface <name>."
[[ "$IFACE" =~ ^[A-Za-z0-9_.-]+$ ]] || die "Unexpected interface name: $IFACE"
[[ "$NAME" =~ ^[A-Za-z0-9_.-]+$ ]] || die "Use only letters, digits, '-', '_' and '.' in --name."
[ -d "/sys/class/net/$IFACE" ] || die "Interface $IFACE does not exist."
IFINDEX="$(cat "/sys/class/net/$IFACE/ifindex")"
GLOBAL="$CTRL_DIR/$IFACE-$IFINDEX.global"
WDEV="$IFACE"

# --- Resolution advertised to the phone ---------------------------------------
# Wi-Fi Display resolution tables (WFD 1.0, as in MiracleCast src/ctl/wfd.c):
# table index width height fps interlaced. Table 0 = CEA, 1 = VESA, 2 = HH.
WFD_MODES='0 0 640 480 60 0
0 1 720 480 60 0
0 2 720 480 60 1
0 3 720 576 50 0
0 4 720 576 50 1
0 5 1280 720 30 0
0 6 1280 720 60 0
0 7 1920 1080 30 0
0 8 1920 1080 60 0
0 9 1920 1080 60 1
0 10 1280 720 25 0
0 11 1280 720 50 0
0 12 1920 1080 25 0
0 13 1920 1080 50 0
0 14 1920 1080 50 1
0 15 1280 720 24 0
0 16 1920 1080 24 0
1 0 800 600 30 0
1 1 800 600 60 0
1 2 1024 768 30 0
1 3 1024 768 60 0
1 4 1152 854 30 0
1 5 1152 854 60 0
1 6 1280 768 30 0
1 7 1280 768 60 0
1 8 1280 800 30 0
1 9 1280 800 60 0
1 10 1360 768 30 0
1 11 1360 768 60 0
1 12 1366 768 30 0
1 13 1366 768 60 0
1 14 1280 1024 30 0
1 15 1280 1024 60 0
1 16 1440 1050 30 0
1 17 1440 1050 60 0
1 18 1440 900 30 0
1 19 1440 900 60 0
1 20 1600 900 30 0
1 21 1600 900 60 0
1 22 1600 1200 30 0
1 23 1600 1200 60 0
1 24 1680 1024 30 0
1 25 1680 1024 60 0
1 26 1680 1050 30 0
1 27 1680 1050 60 0
1 28 1920 1200 30 0
2 0 800 480 30 0
2 1 800 480 60 0
2 2 854 480 30 0
2 3 854 480 60 0
2 4 864 480 30 0
2 5 864 480 60 0
2 6 640 360 30 0
2 7 640 360 60 0
2 8 960 540 30 0
2 9 960 540 60 0
2 10 848 480 30 0
2 11 848 480 60 0'

# Prints the current screen size as WxH: the primary output, else the first
# active one, else the whole X screen.
detect_screen() {
    local s=""
    if have xrandr; then
        s="$(xrandr --current 2>/dev/null | awk '/ connected primary [0-9]+x[0-9]+/ {for (i=1;i<=NF;i++) if ($i ~ /^[0-9]+x[0-9]+\+/) {split($i, a, "+"); print a[1]; exit}}')"
        [ -n "$s" ] || s="$(xrandr --current 2>/dev/null | awk '/ connected [0-9]+x[0-9]+\+/ || / connected [a-z]+ [0-9]+x[0-9]+\+/ {for (i=1;i<=NF;i++) if ($i ~ /^[0-9]+x[0-9]+\+/) {split($i, a, "+"); print a[1]; exit}}')"
    fi
    if [ -z "$s" ] && have xdpyinfo; then
        s="$(xdpyinfo 2>/dev/null | awk '/dimensions:/ {print $2; exit}')"
    fi
    echo "$s"
}

# Prints "native cea vesa hh" (hex) for a screen of W x H and a frame rate limit:
# every progressive mode that fits is offered, and the one with the most pixels
# per second is marked as the native mode the phone should prefer.
# CEA 640x480p60 is always offered because the specification makes it mandatory.
wfd_formats_for() {
    echo "$WFD_MODES" | awk -v W="$1" -v H="$2" -v F="$3" '
        BEGIN { use[0, 0] = 1; best = -1 }
        $6 == 0 && $3 <= W && $4 <= H && $5 <= F {
            use[$1, $2] = 1
            score = $3 * $4 * $5          # pixels per second: 1080p60 beats 1200p30
            if (score > best) { best = score; nt = $1; ni = $2 }
        }
        END {
            for (t = 0; t < 3; t++) { m[t] = 0; for (i = 0; i < 32; i++) if ((t, i) in use) m[t] += 2 ^ i }
            native = (best < 0) ? 0 : ni * 8 + nt
            printf "%02x %08x %08x %08x\n", native, m[0], m[1], m[2]
        }'
}

# Describes a native byte, for example "VESA 1366x768p60".
wfd_native_name() {
    local n=$((16#$1))
    echo "$WFD_MODES" | awk -v t=$((n % 8)) -v i=$((n / 8)) '
        $1 == t && $2 == i { split("CEA VESA HH", name, " "); printf "%s %dx%dp%d\n", name[t + 1], $3, $4, $5 }'
}

# Sets VIDEO_FORMATS (empty = MiracleCast default) and RES_INFO from --res.
VIDEO_FORMATS=""
resolve_formats() {
    local w h screen f native cea vesa hh
    case "$RES_MODE" in
        all) RES_INFO="all modes up to 1920x1080 (MiracleCast default)"; return ;;
        auto)
            screen="$(detect_screen)"
            if [[ ! "$screen" =~ ^[0-9]+x[0-9]+$ ]]; then
                RES_INFO="screen size not detected, using MiracleCast default"
                return
            fi
            w="${screen%x*}"; h="${screen#*x}" ;;
        *) w="${RES_MODE%x*}"; h="${RES_MODE#*x}"; screen="$RES_MODE" ;;
    esac
    read -r native cea vesa hh <<< "$(wfd_formats_for "$w" "$h" "$MAX_FPS")"
    VIDEO_FORMATS="$native 00 03 10 $cea $vesa $hh 00 0000 0000 10 none none"
    RES_INFO="screen $screen, up to ${MAX_FPS} fps, preferred $(wfd_native_name "$native") (cea=$cea vesa=$vesa hh=$hh)"
}

[[ "$MAX_FPS" =~ ^[0-9]+$ ]] || die "--max-fps needs a number, for example 30."
for opt in "${VIEWER_OPTS[@]}"; do
    [[ "$opt" =~ ^[A-Za-z0-9_.-]+$ ]] || die "Unexpected viewer option value: $opt"
done
[[ "$RES_MODE" =~ ^(auto|all|[0-9]+x[0-9]+)$ ]] || die "--res must be auto, all, or WxH (for example 1280x720)."

# --- Facts that can be collected without changing anything ---------------------
missing_gst() {
    local el out=""
    have gst-inspect-1.0 || { echo "gst-inspect-1.0 not installed"; return; }
    for el in udpsrc rtpjitterbuffer rtpmp2tdepay tsdemux h264parse avdec_h264 videoconvert autovideosink; do
        gst-inspect-1.0 --exists "$el" >/dev/null 2>&1 || out="$out $el"
    done
    echo "${out:- none}"
}

viewer_check() {
    local out
    have setpriv || { echo "setpriv missing (util-linux)"; return; }
    out="$("${MIRACAST_PYTHON:-/usr/bin/python3}" - <<'PYCHECK' 2>&1 | tail -n 1
import gi
gi.require_version("Gtk", "3.0"); gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0"); gi.require_version("GdkX11", "3.0")
from gi.repository import Gtk, Gst, GstVideo, GdkX11
Gst.init(None)
sinks = [n for n in ("xvimagesink", "gtksink", "ximagesink") if Gst.ElementFactory.find(n)]
print("ok, video sinks: " + (", ".join(sinks) or "none"))
PYCHECK
)"
    echo "${out:-python3 check failed}"
}

decoder_check() {
    local d out=""
    have gst-inspect-1.0 || { echo "gst-inspect-1.0 missing"; return; }
    for d in vah264dec vaapih264dec avdec_h264; do
        if gst-inspect-1.0 --exists "$d" >/dev/null 2>&1; then out="$out $d"; fi
    done
    case "$out" in
        *vah264dec*|*vaapih264dec*) echo "${out# } (hardware decoding available)" ;;
        *) echo "${out# } (software only: see the guide for hardware decoding packages)" ;;
    esac
}

static_facts() {
    local driver regdom pkg
    driver="$(basename "$(readlink -f "/sys/class/net/$IFACE/device/driver" 2>/dev/null)" 2>/dev/null)"
    regdom="$(iw reg get 2>/dev/null | awk '/^country/ {gsub(":", "", $2); print $2; exit}')"
    pkg="$(pacman --query miraclecast-git 2>/dev/null || echo 'miraclecast: unknown version')"
    echo "kernel $(uname -r) | $pkg | $(wpa_supplicant -v 2>&1 | head -n 1)"
    echo "iface $IFACE (ifindex $IFINDEX) driver ${driver:-unknown} regdom ${regdom:-unknown}"
    echo "P2P modes: $(iw list 2>/dev/null | grep -oE '\* P2P-(client|GO|device)$' | sort -u | tr -d '*' | tr '\n' ' ')"
}

if [ "$CHECK_ONLY" -eq 1 ]; then
    echo "=========== MIRACAST PREFLIGHT (read-only) ==========="
    static_facts
    echo "NetworkManager devices:"
    nmcli --terse --fields DEVICE,TYPE,STATE device status 2>/dev/null | grep -E "^($IFACE|p2p-)" | sed 's/^/  /'
    echo "services: NetworkManager=$(systemctl is-active NetworkManager 2>/dev/null)" \
         "wpa_supplicant=$(systemctl is-active wpa_supplicant 2>/dev/null)" \
         "iwd=$(systemctl is-active iwd 2>/dev/null)" \
         "firewalld=$(systemctl is-active firewalld 2>/dev/null)" \
         "ufw=$(systemctl is-active ufw 2>/dev/null)"
    echo "wpa_supplicant processes:"
    pgrep --list-full --exact wpa_supplicant 2>/dev/null | cut -c1-110 | sed 's/^/  /' || true
    echo "D-Bus mode wpa_supplicant (NetworkManager) running: $(yesno system_wpa_alive)"
    echo "P2P-device wdev present now: $(yesno p2p_wdev_present)"
    echo "miracle-wifid already running: $(yesno pgrep --exact miracle-wifid)"
    echo "missing GStreamer elements:$(missing_gst)"
    echo "viewer: $(viewer_check)"
    echo "H.264 decoders: $(decoder_check)"
    resolve_formats
    echo "resolution offered: $RES_INFO"
    echo "DISPLAY=${DISPLAY:-unset} Xauthority file found: $(yesno test -r "${XAUTHORITY:-$HOME/.Xauthority}")"
    echo "rfkill: $(rfkill list wifi 2>/dev/null | grep -ciE 'blocked: yes') blocked entries"
    echo "A real run would: unmanage $IFACE in NetworkManager, stop and runtime-mask wpa_supplicant.service,"
    echo "start miracle-wifid, open UDP 7236 + DHCP in firewalld if it is running, and undo all of it on exit."
    echo "======================================================"
    exit 0
fi

# --- Report --------------------------------------------------------------------
count_log() { grep -cE "$1" "$LOG" 2>/dev/null || true; }

live_state() {
    # Collected while miracle-wifid is still running.
    local peers n=0 wfd=0 mac info name line
    LIVE_WIFID="$(yesno pgrep --exact miracle-wifid)"
    LIVE_SYSWPA="$(yesno system_wpa_alive)"
    LIVE_WPA="$(yesno miracle_wpa_alive)"
    LIVE_WDEV="$(yesno p2p_wdev_present)"
    LIVE_P2P_STATE="$(wpa_glob status | sed -n 's/^p2p_state=//p')"
    LIVE_WFD="$(wpa_glob status | sed -n 's/^wifi_display=//p')"
    LIVE_SUBELEM="$(wpa_dev wfd_subelem_get 0 | tail -n 1)"
    LIVE_PEER_LINES=""
    peers="$(wpa_dev p2p_peers | grep -E "^$MAC_RE$" || true)"
    for mac in $peers; do
        n=$((n + 1))
        info="$(wpa_dev p2p_peer "$mac")"
        name="$(echo "$info" | sed -n 's/^device_name=//p')"
        if echo "$info" | grep -q '^wfd_subelems='; then wfd=$((wfd + 1)); line="wfd=yes"; else line="wfd=no"; fi
        if [ "$n" -le 6 ]; then LIVE_PEER_LINES="$LIVE_PEER_LINES  peer: \"${name:-?}\" $line"$'\n'; fi
    done
    LIVE_PEERS="$n"
    LIVE_PEERS_WFD="$wfd"
    LIVE_LIST="$(sudo --non-interactive miracle-wifictl list 2>&1 | head -n 12)"
}

append_journals() {
    {
        echo "===== wpa_supplicant (journal: miracle-wifid-$IFACE-$IFINDEX) ====="
        sudo journalctl --since "@$START_EPOCH" --identifier="miracle-wifid-$IFACE-$IFINDEX" --output=cat --no-pager 2>&1
        echo "===== player (journal: miracle-sinkctl-gst) ====="
        sudo journalctl --since "@$START_EPOCH" --identifier=miracle-sinkctl-gst --output=cat --no-pager 2>&1 | tail -n 200
        echo "===== kernel ====="
        sudo journalctl --dmesg --since "@$START_EPOCH" --output=cat --no-pager 2>&1 | grep -iE 'ath|wlan|cfg80211|p2p|firmware' || true
        echo "===== miracle-wifictl list at exit ====="
        echo "${LIVE_LIST:-}"
    } >> "$LOG"
}

print_report() {
    local fw peak errs kern syswpa steps vlines
    fw="$(sudo journalctl --dmesg --boot --output=cat --no-pager 2>/dev/null | grep -m 1 -oE 'firmware ver [^ ]+' || true)"
    syswpa="$(grep -c 'sys_wpa=yes' "$LOG" 2>/dev/null || true)"
    steps="$(grep -oE '^STEP [0-9]+ [a-z0-9-]+' "$LOG" 2>/dev/null | awk '{print $3}' | tr '\n' ' ')"
    peak="$(grep -oE 'peers=[0-9]+' "$LOG" 2>/dev/null | cut -d= -f2 | sort -n | tail -n 1)"
    errs="$(sed -n '/^===== wpa_supplicant/,/^===== player/p' "$LOG" | grep -iE 'fail|error|not supported|could not|unable|denied|busy' | sort -u)"
    kern="$(sed -n '/^===== kernel/,/^===== miracle-wifictl/p' "$LOG" | grep -vE '^=====' || true)"
    echo "================ MIRACAST SINK REPORT ================"
    echo "run $(( $(date +%s) - START_EPOCH )) s as \"$NAME\" | ${fw:-firmware ver unknown}"
    static_facts
    echo "steps reached: ${steps:-none}"
    echo "resolution offered: $RES_INFO"
    echo "before start: system wpa_supplicant stopped=$([ "$WPA_STOPPED" -eq 1 ] && echo yes || echo no)" \
         "masked=$([ "$WPA_MASKED" -eq 1 ] && echo yes || echo no)" \
         "| stale P2P-device wdev=$STALE_P2P | firewalld=$FW_STATE"
    echo "at exit: wifid alive=${LIVE_WIFID:-?} | its wpa_supplicant alive=${LIVE_WPA:-?} | link=${LINK_ID:-none}"
    echo "system (NetworkManager) wpa_supplicant: alive at exit=${LIVE_SYSWPA:-?} | samples where it was alive=${syswpa:-0}"
    echo "at exit: P2P-device wdev=${LIVE_WDEV:-?} | p2p_state=${LIVE_P2P_STATE:-none}" \
         "| wifi_display=${LIVE_WFD:-none} | wfd_subelem0=${LIVE_SUBELEM:-none}"
    echo "P2P peers seen by laptop: at exit=${LIVE_PEERS:-?} (with WFD info=${LIVE_PEERS_WFD:-?}) | peak=${peak:-0}"
    printf '%s' "${LIVE_PEER_LINES:-}"
    echo "log lines: device-found=$(count_log 'P2P-DEVICE-FOUND') prov-disc=$(count_log 'P2P-PROV-DISC')" \
         "go-neg-request=$(count_log 'P2P-GO-NEG-REQUEST') go-neg-success=$(count_log 'P2P-GO-NEG-SUCCESS')" \
         "go-neg-failure=$(count_log 'P2P-GO-NEG-FAILURE')"
    echo "log lines: group-started=$(count_log 'P2P-GROUP-STARTED') formation-failure=$(count_log 'P2P-GROUP-FORMATION-FAILURE')" \
         "group-removed=$(count_log 'P2P-GROUP-REMOVED') find-failed=$(count_log 'P2P_FIND failed')"
    echo "wpa_supplicant problem lines: $(printf '%s' "$errs" | grep -c . || true) distinct; first 6:"
    printf '%s\n' "$errs" | head -n 6 | cut -c1-110 | sed 's/^/  /'
    vlines="$(sed -n '/^===== player/,/^===== kernel/p' "$LOG" | grep -E '^(viewer|player):' || true)"
    echo "viewer: $(printf '%s' "$vlines" | grep -c . || true) lines; key lines:"
    printf '%s\n' "$vlines" | grep -vE 'stats:|Controlling|Released|Click the picture' | tail -n 6 | cut -c1-110 | sed 's/^/  /'
    echo "viewer performance (first and last full interval):"
    printf '%s\n' "$vlines" | grep 'stats:' | sed -n '2p;$p' | sed 's/^viewer: stats: //' | cut -c1-110 | sed 's/^/  /'
    echo "kernel Wi-Fi lines during run: $(printf '%s' "$kern" | grep -c . || true); last 3:"
    printf '%s\n' "$kern" | tail -n 3 | cut -c1-110 | sed 's/^/  /'
    echo "network after restore: $IFACE = $(nmcli --get-values GENERAL.STATE device show "$IFACE" 2>/dev/null)"
    echo "======================================================"
}

# --- Restore everything on exit ------------------------------------------------
cleanup() {
    [ "$CLEANED" -eq 0 ] || return 0
    CLEANED=1
    trap '' INT TERM HUP          # do not let a second Ctrl+C interrupt the restore
    [ "$STARTED" -eq 1 ] || return 0
    echo
    say "Stopping and restoring Wi-Fi (please wait, do not close this window)..."
    live_state
    if [ -n "$SAMPLER_PID" ]; then kill "$SAMPLER_PID" 2>/dev/null || true; fi
    sudo pkill --exact miracle-sinkctl 2>/dev/null || true
    sudo pkill -f 'miracast-player[.]sh' 2>/dev/null || true
    pkill -f 'miracast-viewer[.]py' 2>/dev/null || true
    sudo pkill --exact miracle-wifid 2>/dev/null || true
    for _ in 1 2 3 4 5 6; do pgrep --exact miracle-wifid >/dev/null 2>&1 || break; sleep 0.5; done
    sudo pkill --signal KILL --exact miracle-wifid 2>/dev/null || true
    sudo pkill -f 'wpa_supplicant.*[/]run/miracle' 2>/dev/null || true
    if [ "$FW_PORT_ADDED" -eq 1 ]; then sudo firewall-cmd --quiet --remove-port=7236/udp 2>/dev/null || true; fi
    if [ "$FW_DHCP_ADDED" -eq 1 ]; then sudo firewall-cmd --quiet --remove-service=dhcp 2>/dev/null || true; fi
    if [ "$WPA_MASKED" -eq 1 ]; then sudo systemctl unmask --runtime --quiet wpa_supplicant.service 2>/dev/null || true; fi
    if [ "$WPA_STOPPED" -eq 1 ]; then sudo systemctl start wpa_supplicant.service 2>/dev/null || true; fi
    if [ "$NM_CHANGED" -eq 1 ]; then
        nmcli device set "$IFACE" managed yes 2>/dev/null || true
        nmcli radio wifi on >/dev/null 2>&1 || true
        for i in $(seq 1 25); do
            case "$(nmcli --get-values GENERAL.STATE device show "$IFACE" 2>/dev/null)" in 100*) break ;; esac
            if [ "$i" -eq 8 ]; then nmcli device connect "$IFACE" >/dev/null 2>&1 || true; fi
            sleep 1
        done
    fi
    append_journals
    print_report | redact | tee "$REPORT"
    if [ -n "$CONF_HOME" ]; then rm -rf "$CONF_HOME" 2>/dev/null || true; fi
    say "Photograph the report above. Raw log: $LOG (has MAC addresses, do not publish)."
    say "If Wi-Fi is not back within a minute, run: sudo systemctl restart NetworkManager"
    if [ "$PAUSE_AT_EXIT" -eq 1 ]; then
        read -r -p "[miracast] Press Enter to close this window. " _ || true
    fi
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM HUP

# --- Take the adapter ----------------------------------------------------------
say "Using Wi-Fi interface: $IFACE. Wi-Fi will drop now and come back when this script ends."
sudo --validate || die "sudo is required."
: > "$LOG"
STARTED=1
step start
say "Report will be saved to: $REPORT"

if pgrep --exact miracle-wifid >/dev/null 2>&1; then
    say "A miracle-wifid from an earlier run is still active. Stopping it."
    sudo pkill --exact miracle-wifid || true
    sleep 1
fi

NM_CHANGED=1
nmcli device set "$IFACE" managed no || die "Could not release $IFACE from NetworkManager."

step nm-released
# miracle-wifid starts its own wpa_supplicant on the interface. The system one is
# stopped so that it cannot keep the P2P device of this adapter. NetworkManager
# starts it again on demand through D-Bus, so it is also masked with --runtime
# (lives in /run, gone after a reboot) and unmasked on exit.
if systemctl is-active --quiet wpa_supplicant.service || system_wpa_alive; then
    say "Stopping the system wpa_supplicant service (restored on exit)."
    if sudo systemctl mask --runtime --quiet wpa_supplicant.service; then WPA_MASKED=1; fi
    if sudo systemctl stop wpa_supplicant.service; then WPA_STOPPED=1; fi
    sleep 1
    if system_wpa_alive; then
        say "The system wpa_supplicant is still running. Stopping that process."
        sudo pkill -f '^[^ ]*wpa_supplicant .*-u' || true
    fi
fi
step wpa-stopped
sleep 1
STALE_P2P="$(yesno p2p_wdev_present)"
if [ "$STALE_P2P" = yes ]; then
    say "Note: a P2P-device wdev still exists before miracle-wifid starts (another process may hold it)."
fi

if have firewall-cmd && sudo firewall-cmd --state >/dev/null 2>&1; then
    FW_STATE="running"
    if ! sudo firewall-cmd --quiet --query-port=7236/udp; then
        if sudo firewall-cmd --quiet --add-port=7236/udp; then FW_PORT_ADDED=1; fi
    fi
    if ! sudo firewall-cmd --quiet --query-service=dhcp; then
        if sudo firewall-cmd --quiet --add-service=dhcp; then FW_DHCP_ADDED=1; fi
    fi
    FW_STATE="running(udp7236 added=$FW_PORT_ADDED dhcp added=$FW_DHCP_ADDED)"
    say "firewalld is running: UDP 7236 and DHCP are allowed until this script ends."
fi

# --- Start miracle-wifid -------------------------------------------------------
# setsid keeps the daemon out of this terminal's Ctrl+C so the report can still
# query it. wpa_supplicant output goes to the journal (collected on exit).
say "Starting miracle-wifid..."
step wifid-start
sudo env MC_LOG="$LOG" MC_IFACE="$IFACE" sh -c \
    'exec setsid miracle-wifid --interface "$MC_IFACE" --log-level debug --log-date-time --wpa-loglevel info >> "$MC_LOG" 2>&1' &

for _ in $(seq 1 24); do
    LINK_ID="$(sudo miracle-wifictl list 2>/dev/null | awk -v i="$IFACE" '$2 == i && $1 ~ /^[0-9]+$/ {print $1; exit}')"
    [ -z "$LINK_ID" ] || break
    sleep 0.5
done
if [ -z "$LINK_ID" ]; then
    tail -n 15 "$LOG" | redact >&2
    die "miracle-wifid did not list $IFACE as a link."
fi
say "Link id: $LINK_ID"
step link-found

sudo miracle-wifictl set-managed "$LINK_ID" yes || say "set-managed reported an error (continuing)."

READY=no
for _ in $(seq 1 30); do
    if [ -n "$(wpa_glob status | sed -n 's/^p2p_state=//p')" ]; then READY=yes; break; fi
    sleep 0.5
done
if sudo test -S "$CTRL_DIR/p2p-dev-$IFACE"; then WDEV="p2p-dev-$IFACE"; fi
if [ "$READY" = yes ]; then
    step p2p-ready
    say "wpa_supplicant is up with P2P (control interface: $WDEV)."
else
    say "wpa_supplicant did not report a P2P state within 15 s. Continuing so the report shows why."
fi

sudo miracle-wifictl set-friendly-name "$LINK_ID" "$NAME" || say "Could not set the friendly name (continuing)."

# Background sampler: records what the laptop itself sees, every 5 s.
(
    trap '' INT
    while :; do
        peers="$(wpa_dev p2p_peers | grep -cE "^$MAC_RE$" || true)"
        state="$(wpa_glob status | sed -n 's/^p2p_state=//p')"
        sys="$(yesno system_wpa_alive)"
        echo "SAMPLE $(date +%s) p2p_state=${state:-none} peers=${peers:-0} sys_wpa=$sys" >> "$LOG"
        sleep 5
    done
) &
SAMPLER_PID=$!

cat <<MSG

[miracast] Receiver "$NAME" is ready.
  On the phone: Settings > Connected devices > Samsung DeX > Connect wirelessly
  (or Smart View), then pick "$NAME".
  In the viewer: click the picture to use your mouse and keyboard on the phone.
    Ctrl+Alt gives them back to the laptop. Then F11 = full screen, Ctrl+Q = quit.
  To stop: close the viewer, or type  quit  here and press Enter.
  (Ctrl+C here only clears the input line of miracle-sinkctl.)

MSG

# miracle-sinkctl reads its configuration from $HOME, so a private HOME holds a
# temporary config: the full screen viewer as external player, and a UIBC
# capability that offers USB HID keyboard and mouse (HIDC) to the phone.
SINK_ARGS=(--audio "$AUDIO")
SINK_ENV=("DISPLAY=${DISPLAY:-:0}" "XAUTHORITY=${XAUTHORITY:-$HOME/.Xauthority}" "MIRACAST_USER=$USER")
if [ -n "${MIRACAST_PYTHON:-}" ]; then SINK_ENV+=("MIRACAST_PYTHON=$MIRACAST_PYTHON"); fi
if [ "${#VIEWER_OPTS[@]}" -gt 0 ]; then SINK_ENV+=("MIRACAST_VIEWER_OPTS=${VIEWER_OPTS[*]}"); fi
resolve_formats
CONF_LINES=()
if [ -n "$VIDEO_FORMATS" ]; then
    # Same layout as MiracleCast's default wfd_video_formats; only the native
    # mode and the resolution masks differ.
    CONF_LINES+=("extends.wfd_video_formats=$VIDEO_FORMATS")
fi
if [ "$INPUT" -eq 1 ]; then
    CONF_LINES+=("external-player=$SCRIPT_DIR/miracast-player.sh")
    CONF_LINES+=("extends.wfd_uibc_capability=input_category_list=HIDC;generic_cap_list=none;hidc_cap_list=Keyboard/USB, Mouse/USB;port=none")
    SINK_ARGS+=(--uibc)
fi
if [ "${#CONF_LINES[@]}" -gt 0 ]; then
    CONF_HOME="$(mktemp -d "${TMPDIR:-/tmp}/miracast-conf.XXXXXX")"
    { echo "[sinkctl]"; printf '%s\n' "${CONF_LINES[@]}"; } > "$CONF_HOME/.miraclecastrc"
    chmod 755 "$CONF_HOME"
    chmod 644 "$CONF_HOME/.miraclecastrc"
    SINK_ENV+=("HOME=$CONF_HOME")
fi
say "Resolution offered to the phone: $RES_INFO"
echo "RESOLUTION $RES_INFO" >> "$LOG"

step sink-run
sudo env "${SINK_ENV[@]}" miracle-sinkctl "${SINK_ARGS[@]}" run "$LINK_ID"
say "miracle-sinkctl exited with code $?."
