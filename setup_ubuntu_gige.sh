#!/usr/bin/env bash
# Ubuntu network tuning for multi-camera hardware-synchronized GigE capture.
#
# Only needed if you're grouping 2+ GigE cameras for synchronized capture
# (see the Group field in capture_cameras.py's GUI) — single-camera or
# sequential capture doesn't need any of this. Synchronized capture makes
# every camera in a group transmit its frame at roughly the same instant, a
# bandwidth burst that sequential capture naturally avoids by staggering.
#
# Two modes:
#   auto    - runs `PylonGigEConfigurator auto-all` (needs the pylon Camera
#             Software Suite installed; this is Basler's own official tool
#             and does the most, but requires that install).
#   manual  - applies the same class of tuning directly, without needing the
#             full pylon Suite: jumbo-frame MTU (session only — see the
#             printed netplan snippet for persistence), rmem_max, NIC ring
#             buffer size, interrupt coalescing, and (opt-in) rp_filter.
#
# Usage:
#   ./setup_ubuntu_gige.sh auto
#   ./setup_ubuntu_gige.sh manual --iface eth0
#   ./setup_ubuntu_gige.sh manual --iface eth0 --rp-filter
#   ./setup_ubuntu_gige.sh manual --iface eth0 --dry-run
#   ./setup_ubuntu_gige.sh manual --iface eth0 --persist-only
#
# Nothing runs without an explicit auto|manual subcommand, and nothing
# changes system state without printing the full plan first and (unless
# --yes) asking for confirmation.

set -euo pipefail

RMEM_MAX_DEFAULT=33554432   # matches PylonGigEConfigurator auto-opt's own
                             # default (32MB) — NOT the older pylon Linux
                             # README's manual-troubleshooting value of 2MB;
                             # rmem_max is a ceiling, not a forced
                             # allocation, so the larger value is strictly
                             # safe. Override with --rmem-max if you
                             # specifically want to match the older value.
RING_BUFFER_SIZE=4096
SYSCTL_DROPIN=/etc/sysctl.d/99-script-grabber-gige.conf
LIMITS_DROPIN=/etc/security/limits.d/99-script-grabber-rtprio.conf

MODE=""
IFACE=""
RMEM_MAX="$RMEM_MAX_DEFAULT"
DO_RP_FILTER=0
DRY_RUN=0
ASSUME_YES=0
PERSIST_ONLY=0

usage() {
    cat <<'EOF'
Usage: setup_ubuntu_gige.sh <auto|manual> [options]

  auto              Run `PylonGigEConfigurator auto-all` (requires pylon
                     Camera Software Suite installed).
  manual            Apply the manual fallback tuning directly (works
                     without the pylon Suite).

Options:
  --iface IFACE     Interface to tune (required for manual mode; auto mode
                     tunes whatever PylonGigEConfigurator finds itself).
  --rmem-max N      Override rmem_max for manual mode (default: 33554432 /
                     32MB, matching PylonGigEConfigurator's own default).
  --rp-filter       Also disable rp_filter (manual mode only; off by
                     default — only needed for a dedicated-NIC-per-camera
                     topology; weakens spoofed-packet filtering, so it's
                     opt-in, not automatic).
  --persist-only    Manual mode only: write/refresh the sysctl.d and
                     limits.d drop-in files without touching runtime state
                     via sysctl -w/ethtool — for machines where auto-all
                     already ran but this machine uses systemd-networkd
                     (see README's persistence note; auto-all's dispatcher
                     script only fires under NetworkManager).
  --dry-run         Print exactly what would run; makes no changes, invokes
                     no sudo at all.
  --yes             Skip the confirmation prompt (the plan is still printed
                     first).
  -h, --help        Show this usage and exit.

No arguments -> same as --help. Nothing runs without an explicit
auto|manual subcommand.
EOF
}

log() { printf '%s\n' "$*"; }

detect_renderer() {
    if command -v networkctl >/dev/null 2>&1 && systemctl is-active --quiet systemd-networkd 2>/dev/null; then
        echo "systemd-networkd"
    elif [ -d /etc/netplan ] && grep -rq "renderer:\s*NetworkManager" /etc/netplan/*.yaml 2>/dev/null; then
        echo "NetworkManager"
    elif systemctl is-active --quiet NetworkManager 2>/dev/null; then
        echo "NetworkManager"
    else
        echo "unknown"
    fi
}

if [ "$(uname -s)" != "Linux" ]; then
    echo "This script is Ubuntu/Linux-only." >&2
    exit 1
fi

if [ $# -eq 0 ]; then
    usage
    exit 0
fi

MODE="$1"; shift || true
case "$MODE" in
    -h|--help) usage; exit 0 ;;
    auto|manual) ;;
    *) echo "Unknown mode: $MODE" >&2; usage; exit 1 ;;
esac

while [ $# -gt 0 ]; do
    case "$1" in
        --iface) IFACE="$2"; shift 2 ;;
        --rmem-max) RMEM_MAX="$2"; shift 2 ;;
        --rp-filter) DO_RP_FILTER=1; shift ;;
        --persist-only) PERSIST_ONLY=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --yes) ASSUME_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
done

RENDERER="$(detect_renderer)"

if [ "$MODE" = "manual" ] && [ -z "$IFACE" ]; then
    echo "manual mode requires --iface. Candidate interfaces on this machine:" >&2
    ip -brief link show | awk '$1 != "lo" {print "  " $1}' >&2
    exit 1
fi

if [ "$MODE" = "auto" ]; then
    PYLON_CONFIGURATOR="$(command -v PylonGigEConfigurator || true)"
    if [ -z "$PYLON_CONFIGURATOR" ]; then
        for candidate in /opt/pylon*/bin/PylonGigEConfigurator; do
            if [ -x "$candidate" ]; then
                PYLON_CONFIGURATOR="$candidate"
                break
            fi
        done
    fi
    if [ -z "$PYLON_CONFIGURATOR" ]; then
        echo "ERROR: PylonGigEConfigurator not found. Install the pylon Camera" >&2
        echo "Software Suite, or use 'manual' mode instead." >&2
        exit 1
    fi
fi

log "=== Plan ==="
log "Detected network renderer: $RENDERER"
if [ "$MODE" = "auto" ]; then
    log "Mode: auto — will run: sudo $PYLON_CONFIGURATOR auto-all"
    log "This applies (per Basler's own tool): MTU (max supported), rmem_max=33554432,"
    log "NIC ring buffer size 4096, interrupt moderation tuning, rtprio 99 in"
    log "/etc/security/limits.conf, and rp_filter=0 on every interface it touches —"
    log "flagging rp_filter and the limits.conf edit as the most system-wide-reaching"
    log "of these. It also installs a NetworkManager dispatcher script so these"
    log "persist across reboot — but ONLY if NetworkManager is this machine's"
    log "renderer."
    if [ "$RENDERER" != "NetworkManager" ]; then
        log ""
        log "!! Detected renderer is '$RENDERER', not NetworkManager — auto-all's own"
        log "!! persistence mechanism will NOT fire. Its runtime changes will still"
        log "!! apply now, but MTU/ring-buffer/coalescing will revert on reboot unless"
        log "!! you also run 'manual --persist-only' and/or add the netplan MTU"
        log "!! snippet this script prints in manual mode."
    fi
else
    log "Mode: manual on interface '$IFACE'"
    [ "$PERSIST_ONLY" -eq 1 ] && log "  (--persist-only: writing drop-in config files only, no runtime changes)"
    log "  - MTU: read current, set to 9000 if lower (session only — not persisted by this script)"
    log "  - rmem_max: sysctl target $RMEM_MAX (persisted via $SYSCTL_DROPIN)"
    log "  - NIC ring buffer: rx/tx $RING_BUFFER_SIZE (ethtool -G, session only)"
    log "  - Interrupt coalescing: adaptive-rx/tx off (ethtool -C, session only)"
    log "  - rtprio 99 for all users (persisted via $LIMITS_DROPIN)"
    if [ "$DO_RP_FILTER" -eq 1 ]; then
        log "  - rp_filter: disabling on '$IFACE' and 'all' (requested via --rp-filter;"
        log "    only needed for dedicated-NIC-per-camera topologies — weakens"
        log "    spoofed-packet filtering)"
    else
        log "  - rp_filter: not requested (pass --rp-filter to include)"
    fi
fi
log "============"

if [ "$DRY_RUN" -eq 1 ]; then
    log "(--dry-run: stopping here, no changes made, no sudo invoked)"
    exit 0
fi

if [ "$ASSUME_YES" -ne 1 ]; then
    read -r -p "Proceed? [y/N] " reply
    case "$reply" in
        y|Y|yes|YES) ;;
        *) echo "Aborted, no changes made."; exit 0 ;;
    esac
fi

if [ "$MODE" = "auto" ]; then
    if sudo "$PYLON_CONFIGURATOR" auto-all; then
        log "[OK] PylonGigEConfigurator auto-all completed"
    else
        log "[FAILED] PylonGigEConfigurator auto-all exited non-zero"
        exit 1
    fi
    exit 0
fi

# --- manual mode ---

if [ "$PERSIST_ONLY" -ne 1 ]; then
    current_mtu="$(ip -o link show "$IFACE" | grep -oP 'mtu \K[0-9]+' || echo "")"
    if [ "$current_mtu" = "9000" ]; then
        log "[OK] MTU already 9000 on $IFACE (skipped)"
    else
        if sudo ip link set dev "$IFACE" mtu 9000; then
            log "[APPLIED] MTU: ${current_mtu:-unknown} -> 9000 on $IFACE (session only, see netplan note below)"
        else
            log "[FAILED] could not set MTU on $IFACE — does this NIC/switch support jumbo frames?"
        fi
    fi

    if sudo ethtool -G "$IFACE" rx "$RING_BUFFER_SIZE" tx "$RING_BUFFER_SIZE" 2>/dev/null; then
        log "[APPLIED] ring buffer: rx/tx $RING_BUFFER_SIZE on $IFACE"
    else
        log "[FAILED] ethtool -G not supported on $IFACE by this driver — skipping"
    fi

    if sudo ethtool -C "$IFACE" adaptive-rx off adaptive-tx off 2>/dev/null; then
        log "[APPLIED] interrupt coalescing: adaptive-rx/tx off on $IFACE"
    else
        log "[FAILED] ethtool -C not supported on $IFACE by this driver — skipping"
    fi

    current_rmem="$(sysctl -n net.core.rmem_max 2>/dev/null || echo "")"
    if [ "$current_rmem" = "$RMEM_MAX" ]; then
        log "[OK] net.core.rmem_max already $RMEM_MAX (skipped)"
    else
        sudo sysctl -w net.core.rmem_max="$RMEM_MAX" >/dev/null
        log "[APPLIED] net.core.rmem_max: ${current_rmem:-unknown} -> $RMEM_MAX (runtime)"
    fi

    if [ "$DO_RP_FILTER" -eq 1 ]; then
        sudo sysctl -w "net.ipv4.conf.all.rp_filter=0" >/dev/null
        sudo sysctl -w "net.ipv4.conf.${IFACE}.rp_filter=0" >/dev/null
        log "[APPLIED] rp_filter=0 on all + $IFACE (runtime)"
    fi
fi

# Persisted config: own small drop-in files, fully regenerated and compared
# against what's on disk each run — never append/edit shared system files.
sysctl_content="net.core.rmem_max=$RMEM_MAX"
if [ "$DO_RP_FILTER" -eq 1 ]; then
    sysctl_content="$sysctl_content
net.ipv4.conf.all.rp_filter=0
net.ipv4.conf.${IFACE}.rp_filter=0"
fi
if [ -f "$SYSCTL_DROPIN" ] && [ "$(cat "$SYSCTL_DROPIN" 2>/dev/null)" = "$sysctl_content" ]; then
    log "[OK] $SYSCTL_DROPIN already current (skipped)"
else
    printf '%s\n' "$sysctl_content" | sudo tee "$SYSCTL_DROPIN" >/dev/null
    log "[APPLIED] wrote $SYSCTL_DROPIN"
fi

limits_content="* - rtprio 99"
if [ -f "$LIMITS_DROPIN" ] && [ "$(cat "$LIMITS_DROPIN" 2>/dev/null)" = "$limits_content" ]; then
    log "[OK] $LIMITS_DROPIN already current (skipped)"
else
    printf '%s\n' "$limits_content" | sudo tee "$LIMITS_DROPIN" >/dev/null
    log "[APPLIED] wrote $LIMITS_DROPIN (takes effect on next login session)"
fi

log ""
log "=== MTU persistence (not automated — add this yourself) ==="
log "This script does not edit netplan (guessing the right place to inject config"
log "into an existing machine's network setup risks breaking unrelated config)."
log "Add this to the relevant /etc/netplan/*.yaml file under your '$IFACE' entry,"
log "then run: sudo netplan apply"
log ""
log "    network:"
log "      ethernets:"
log "        $IFACE:"
log "          mtu: 9000"
log ""
log "=== Summary ==="
log "Renderer: $RENDERER"
if [ "$RENDERER" != "NetworkManager" ]; then
    log "Reminder: ring buffer/coalescing settings above are session-only on this"
    log "renderer — re-run this script (or a systemd unit that does) after reboot if"
    log "you need them to persist too."
fi
log "Reboot, then re-verify: ip link show $IFACE | grep mtu; sysctl net.core.rmem_max;"
log "ethtool -g $IFACE; ethtool -c $IFACE"
log "Ultimate check: a real synchronized capture with no buffer-underrun warnings."
