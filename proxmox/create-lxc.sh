#!/usr/bin/env bash
# Create a sendspin-hdmi LXC on a Proxmox VE host whose own HDMI port is connected to a
# TV or AV receiver. Run as root on the Proxmox host:
#
#   bash create-lxc.sh [--ctid 140] [--hostname sendspin-hdmi] [--storage local-lvm]
#                      [--bridge vmbr0] [--ip dhcp | --ip 192.168.1.60/24 --gw 192.168.1.1]
#                      [--memory 1536] [--cores 2] [--disk 8] [--ref main] [--yes]
#                      [-- installer options: --ma-url URL --token TOKEN --name NAME ...]
#
# What it changes on the host (asks first unless --yes):
#   * /etc/udev/rules.d/90-sendspin-hdmi.rules: makes /dev/snd/* and /dev/dri/* mode 0666
#     so the unprivileged container can open them. That means every local user and every
#     container given these devices can use the host's sound cards and GPU.
#   * The container gets /dev/snd and /dev/dri bind-mounted and allowed (char 116, 226).
#   * The container drives the host's HDMI output directly, so the Proxmox text console on
#     that screen is replaced by the now-playing screen while it runs.
set -euo pipefail

REPO="${SENDSPIN_HDMI_REPO:-slm020/sendspin-hdmi}"
REF=main CTID="" HOSTNAME_=sendspin-hdmi STORAGE="" BRIDGE=vmbr0 IP=dhcp GW="" MEM=1536 CORES=2 DISK=8 YES=0
INSTALL_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --ctid) CTID="$2"; shift 2 ;;
    --hostname) HOSTNAME_="$2"; shift 2 ;;
    --storage) STORAGE="$2"; shift 2 ;;
    --bridge) BRIDGE="$2"; shift 2 ;;
    --ip) IP="$2"; shift 2 ;;
    --gw) GW="$2"; shift 2 ;;
    --memory) MEM="$2"; shift 2 ;;
    --cores) CORES="$2"; shift 2 ;;
    --disk) DISK="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --yes|-y) YES=1; shift ;;
    --) shift; INSTALL_ARGS=("$@"); break ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }
confirm() { [ "$YES" = 1 ] && return 0; read -r -p "$1 [y/N] " a; [[ "$a" =~ ^[Yy] ]]; }

[ "$(id -u)" = 0 ] || die "run as root on the Proxmox host"
command -v pct >/dev/null || die "pct not found: this must run on a Proxmox VE host"

# ---- checks
ls /dev/snd/pcmC*p >/dev/null 2>&1 || die "no sound playback devices in /dev/snd on this host"
[ -d /dev/dri ] || say "No /dev/dri on this host: the screen won't work, audio still will"
if [ -r /proc/asound/cards ]; then say "Sound cards on this host:"; sed 's/^/    /' /proc/asound/cards; fi

[ -n "$CTID" ] || CTID="$(pvesh get /cluster/nextid)"
pct status "$CTID" >/dev/null 2>&1 && die "container $CTID already exists"
if [ -z "$STORAGE" ]; then
  STORAGE="$(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3=="active"{print $1; exit}')"
  [ -n "$STORAGE" ] || die "no storage for containers found; pass --storage"
fi

# ---- template
say "Finding a Debian template"
pveam update >/dev/null || true
ARCH="$(dpkg --print-architecture)"
TPL="$(pveam available --section system | awk '{print $2}' | grep -E "^debian-13-standard_.*_${ARCH}\." | sort -V | tail -1)"
[ -n "$TPL" ] || TPL="$(pveam available --section system | awk '{print $2}' | grep -E "^debian-12-standard_.*_${ARCH}\." | sort -V | tail -1)"
[ -n "$TPL" ] || die "no Debian template available from pveam"
TSTORE="$(pvesm status -content vztmpl | awk 'NR>1 && $3=="active"{print $1; exit}')"
[ -n "$TSTORE" ] || die "no storage for templates found"
pveam list "$TSTORE" | grep -q "$TPL" || { say "Downloading $TPL"; pveam download "$TSTORE" "$TPL" >/dev/null; }

# ---- host device permissions
RULES=/etc/udev/rules.d/90-sendspin-hdmi.rules
if [ ! -f "$RULES" ]; then
  echo
  echo "The container is unprivileged, so the host's sound and GPU device files must be"
  echo "world-accessible (mode 0666) for it to use them. This writes $RULES."
  confirm "Allow that?" || die "cancelled"
  printf 'SUBSYSTEM=="sound", MODE="0666"\nSUBSYSTEM=="drm", MODE="0666"\n' > "$RULES"
  udevadm control --reload-rules
  udevadm trigger --subsystem-match=sound --subsystem-match=drm
fi

# ---- create
NET="name=eth0,bridge=$BRIDGE,ip=$IP"
[ -n "$GW" ] && NET="$NET,gw=$GW"
say "Creating container $CTID ($HOSTNAME_) on $STORAGE"
pct create "$CTID" "$TSTORE:vztmpl/$TPL" --hostname "$HOSTNAME_" --unprivileged 1 --features nesting=1 \
  --cores "$CORES" --memory "$MEM" --swap 0 --rootfs "$STORAGE:$DISK" --net0 "$NET" --onboot 1 \
  --ostype debian --description "sendspin-hdmi: Sendspin player + HDMI now-playing screen for Music Assistant" >/dev/null
cat >> "/etc/pve/lxc/$CTID.conf" <<EOF
lxc.cgroup2.devices.allow: c 116:* rwm
lxc.mount.entry: /dev/snd dev/snd none bind,optional,create=dir
lxc.cgroup2.devices.allow: c 226:* rwm
lxc.mount.entry: /dev/dri dev/dri none bind,optional,create=dir
EOF
pct start "$CTID"

say "Waiting for the network"
for _ in $(seq 1 60); do
  pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done

# ---- install inside
run_install() {  # interactive prompts need a terminal; otherwise run unattended
  if [ -t 0 ] && [ "$YES" != 1 ]; then pct exec "$CTID" -- bash "$1" --ref "$REF" "${INSTALL_ARGS[@]}" </dev/tty
  else pct exec "$CTID" -- bash "$1" --ref "$REF" --yes "${INSTALL_ARGS[@]}" </dev/null; fi
}
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." 2>/dev/null && pwd || true)"
if [ -n "$HERE" ] && [ -f "$HERE/install.sh" ] && [ -f "$HERE/app/server.py" ]; then
  say "Copying installer from this checkout"
  tar -C "$HERE" -cf /tmp/sendspin-hdmi-src.tar app systemd install.sh requirements.txt
  pct push "$CTID" /tmp/sendspin-hdmi-src.tar /root/sendspin-hdmi-src.tar
  rm -f /tmp/sendspin-hdmi-src.tar
  pct exec "$CTID" -- bash -c "mkdir -p /root/sendspin-hdmi-src && tar -C /root/sendspin-hdmi-src -xf /root/sendspin-hdmi-src.tar"
  run_install /root/sendspin-hdmi-src/install.sh
else
  pct exec "$CTID" -- bash -c "apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null"
  pct exec "$CTID" -- bash -c "curl -fsSL https://raw.githubusercontent.com/$REPO/$REF/install.sh -o /root/install.sh"
  run_install /root/install.sh
fi

say "Container $CTID is ready."
