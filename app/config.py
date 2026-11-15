"""Single config file shared by every part of sendspin-hdmi.

/etc/sendspin-hdmi/config.json (mode 600). Missing keys fall back to DEFAULTS, so old
config files keep working when new options are added.
"""
import copy
import hashlib
import hmac
import json
import os
import secrets
import urllib.parse

CONFIG_PATH = os.environ.get("SENDSPIN_HDMI_CONFIG", "/etc/sendspin-hdmi/config.json")
STATE_DIR = os.environ.get("SENDSPIN_HDMI_STATE", "/var/lib/sendspin-hdmi")
SENDSPIN_SETTINGS_DIR = os.path.join(STATE_DIR, "sendspin")

DEFAULTS = {
    "ma": {
        "url": "",            # e.g. http://192.168.1.10:8095
        "token": "",          # Music Assistant long-lived token
        "sendspin_url": "",   # blank = ws://<MA host>:8927/sendspin
    },
    "player": {
        "name": "HDMI Player",
        "audio_device": "",   # PortAudio name prefix or index; blank = default device
        "output_delay_ms": 0,
        "audio_format": "",   # e.g. flac:48000:24:2; blank = automatic
    },
    "screen": {
        "enabled": True,
        "resolution": "1920x1080@60Hz",
        "brand": "",          # big title top left; blank = player name
        "show_lyrics": True,
        "show_waveform": True,
        "show_clock": True,
        "clock_24h": True,
        "idle": "clock",      # clock | logo | black
        "lyric_offset_ms": 0, # + shows lyrics later, - earlier
        "accent": "#5ac8fa",
    },
    "groups": {
        "auto_rejoin": True,
        "rejoin_window_s": 120,
    },
    "hooks": {
        "stop_delay_s": 60,
        "start": {"type": "none", "command": "", "url": "", "method": "POST", "body": "",
                  "denon_host": "", "denon_input": "", "denon_mode": "", "denon_power": True},
        "stop": {"type": "none", "command": "", "url": "", "method": "POST", "body": "",
                 "denon_host": "", "denon_input": "", "denon_mode": "", "denon_power": True},
    },
    "web": {
        "port": 8080,
        "password_hash": "",
    },
}

SECRET_KEYS = {("ma", "token"), ("web", "password_hash")}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    return _merge(DEFAULTS, data)


def save(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def public(cfg):
    """Config safe to send to a browser: secrets replaced by set/not-set flags."""
    out = copy.deepcopy(cfg)
    out["ma"]["token"] = ""
    out["ma"]["token_set"] = bool(cfg["ma"].get("token"))
    out["web"]["password_hash"] = ""
    out["web"]["password_set"] = bool(cfg["web"].get("password_hash"))
    return out


def ma_host(cfg):
    return urllib.parse.urlparse(cfg["ma"]["url"]).hostname or ""


def sendspin_url(cfg):
    if cfg["ma"].get("sendspin_url"):
        return cfg["ma"]["sendspin_url"]
    host = ma_host(cfg)
    return f"ws://{host}:8927/sendspin" if host else ""


def detect_hdmi():
    """Audio device name of a connected HDMI sink, from the sound card's ELD data.

    PortAudio names an HDMI output after the connected TV/receiver, e.g.
    "HDA Intel PCH: DENON-AVR", so "<card long name>: <monitor_name>" matches it.
    Returns "" if nothing is connected (or no ELD info is available).
    """
    import glob
    import re
    longnames = {}
    try:
        for line in open("/proc/asound/cards"):
            m = re.match(r"\s*(\d+)\s+\[.*?\]:\s+.*? - (.+)$", line)
            if m:
                longnames[m.group(1)] = m.group(2).strip()
    except OSError:
        return ""
    for eld in sorted(glob.glob("/proc/asound/card*/eld#*")):
        try:
            info = dict(l.split(None, 1) for l in open(eld).read().splitlines() if " " in l.strip() or "\t" in l)
        except OSError:
            continue
        if info.get("monitor_present", "").strip() == "1" and info.get("monitor_name", "").strip():
            card = re.search(r"card(\d+)", eld).group(1)
            if card in longnames:
                return f"{longnames[card]}: {info['monitor_name'].strip()}"
    return ""


def hash_password(pw):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${dk.hex()}"


def check_password(pw, stored):
    try:
        _, salt, dk = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1)
        return hmac.compare_digest(got.hex(), dk)
    except Exception:
        return False
