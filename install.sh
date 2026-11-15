#!/usr/bin/env bash
# sendspin-hdmi installer: run as root inside a Debian 12/13 container, VM or PC that has
# the HDMI sound card (/dev/snd) and, for the screen, the GPU (/dev/dri).
#
#   bash install.sh [--ma-url http://MA_HOST:8095] [--token TOKEN] [--name "Living Room TV"]
#                   [--password PASS] [--port 8080] [--no-screen] [--ref main] [--yes]
#
# Re-running it upgrades the app and keeps your settings.
set -euo pipefail

REPO="${SENDSPIN_HDMI_REPO:-slm020/sendspin-hdmi}"
REF="main"
PREFIX=/opt/sendspin-hdmi
CONFIG=/etc/sendspin-hdmi/config.json
STATE=/var/lib/sendspin-hdmi
MA_URL="" TOKEN="" NAME="" PASSWORD="" PORT="" SCREEN=1 YES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --ma-url) MA_URL="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --password) PASSWORD="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --no-screen) SCREEN=0; shift ;;
    --ref) REF="$2"; shift 2 ;;
    --yes|-y) YES=1; shift ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
ask() {  # ask VAR "question" default
  local __v="$1" q="$2" d="${3:-}" a=""
  if [ "$YES" = 1 ] || [ ! -t 0 ]; then printf -v "$__v" '%s' "$d"; return; fi
  read -r -p "$q${d:+ [$d]}: " a || true
  printf -v "$__v" '%s' "${a:-$d}"
}

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
OS_ID="$(. /etc/os-release; echo "${ID:-}")"   # subshell: os-release defines NAME, which we use too
case "$OS_ID" in debian|ubuntu) ;; *) warn "untested OS: $OS_ID (made for Debian 12/13)";; esac

# ---- source files: this checkout, or download the release tarball
SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -z "$SRC" ] || [ ! -f "$SRC/app/server.py" ]; then
  say "Downloading sendspin-hdmi ($REPO@$REF)"
  command -v curl >/dev/null || { apt-get update -qq; apt-get install -y -qq curl ca-certificates; }
  TMP="$(mktemp -d)"
  curl -fsSL "https://github.com/$REPO/archive/$REF.tar.gz" | tar -xz -C "$TMP" --strip-components=1
  SRC="$TMP"
fi

# ---- packages
say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
PKGS="python3 python3-venv libportaudio2 alsa-utils dbus ca-certificates"
[ "$SCREEN" = 1 ] && PKGS="$PKGS sway grim chromium fonts-noto-core fonts-noto-color-emoji"
apt-get update -qq
# shellcheck disable=SC2086
apt-get install -y -qq --no-install-recommends $PKGS >/dev/null

# ---- app + Python environment
say "Installing app to $PREFIX"
mkdir -p "$PREFIX" "$STATE" "$(dirname "$CONFIG")"
chmod 700 "$(dirname "$CONFIG")"
rm -rf "$PREFIX/app.new"
cp -r "$SRC/app" "$PREFIX/app.new"
rm -rf "$PREFIX/app.old"; [ -d "$PREFIX/app" ] && mv "$PREFIX/app" "$PREFIX/app.old"
mv "$PREFIX/app.new" "$PREFIX/app"
[ -x "$PREFIX/venv/bin/python" ] || python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q --upgrade pip
"$PREFIX/venv/bin/pip" install -q -r "$SRC/requirements.txt"

# ---- settings (kept on upgrade)
if [ ! -f "$CONFIG" ]; then
  say "First-time settings (all can be changed later on the settings page)"
  [ -n "$MA_URL" ] || ask MA_URL "Music Assistant address, e.g. http://192.168.1.10:8095" ""
  [ -n "$TOKEN" ] || ask TOKEN "Music Assistant long-lived token (MA: profile > Long-lived access tokens)" ""
  [ -n "$NAME" ] || ask NAME "Player name" "HDMI Player"
  [ -n "$PORT" ] || PORT=8080
  GENERATED=0
  if [ -z "$PASSWORD" ]; then
    ask PASSWORD "Password for the settings page (blank = generate one)" ""
    [ -n "$PASSWORD" ] || { PASSWORD="$("$PREFIX/venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(9))')"; GENERATED=1; }
  fi
  MA_URL="$MA_URL" TOKEN="$TOKEN" NAME="$NAME" PASSWORD="$PASSWORD" PORT="$PORT" SCREEN="$SCREEN" \
  "$PREFIX/venv/bin/python" - <<'PY'
import os, sys
sys.path.insert(0, "/opt/sendspin-hdmi/app")
import config as c
cfg = c.load()
cfg["ma"]["url"] = os.environ["MA_URL"].strip().rstrip("/")
cfg["ma"]["token"] = os.environ["TOKEN"].strip()
cfg["player"]["name"] = os.environ["NAME"].strip() or "HDMI Player"
cfg["web"]["port"] = int(os.environ["PORT"])
cfg["web"]["password_hash"] = c.hash_password(os.environ["PASSWORD"])
cfg["screen"]["enabled"] = os.environ["SCREEN"] == "1"
c.save(cfg)
PY
else
  say "Keeping existing settings in $CONFIG"
  GENERATED=0
fi

# ---- services
say "Installing services"
cp "$SRC"/systemd/sendspin-hdmi-*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q sendspin-hdmi-bus sendspin-hdmi-player sendspin-hdmi-web
systemctl restart sendspin-hdmi-bus sendspin-hdmi-player sendspin-hdmi-web
if [ "$SCREEN" = 1 ] && [ -e /dev/dri ]; then
  systemctl enable -q sendspin-hdmi-kiosk
  systemctl restart sendspin-hdmi-kiosk
elif [ "$SCREEN" = 1 ]; then
  warn "No /dev/dri: the HDMI screen can't start. Pass the GPU through, then: systemctl enable --now sendspin-hdmi-kiosk"
fi
[ -e /dev/snd ] || warn "No /dev/snd: the player has no sound card. Pass the HDMI audio device through."

PORT_NOW="$("$PREFIX/venv/bin/python" -c 'import sys; sys.path.insert(0,"/opt/sendspin-hdmi/app"); import config; print(config.load()["web"]["port"])')"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
say "Done. Settings page: http://${IP:-this-host}:${PORT_NOW}/settings"
[ "$GENERATED" = 1 ] && say "Settings password: $PASSWORD  (user name: anything)"
say "The player appears in Music Assistant as a Sendspin player. Pick the HDMI output on the settings page and press 'Play test tone'."
