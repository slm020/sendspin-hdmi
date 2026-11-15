#!/usr/bin/env python3
"""Start the Sendspin client with options from the sendspin-hdmi config (systemd ExecStart)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as conf  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_BIN = os.path.dirname(sys.executable)


def main():
    cfg = conf.load()
    p = cfg["player"]
    os.makedirs(conf.SENDSPIN_SETTINGS_DIR, exist_ok=True)

    # The Sendspin client starts at 25 % software volume on a fresh install, which sounds
    # like "no audio" on an AV receiver. Start at 100 % and let the amp do the volume.
    settings = os.path.join(conf.SENDSPIN_SETTINGS_DIR, "settings-daemon.json")
    if not os.path.exists(settings):
        with open(settings, "w") as f:
            json.dump({"player_volume": 100, "player_muted": False}, f)

    hook = f"{sys.executable} {os.path.join(HERE, 'hook.py')}"
    args = [os.path.join(VENV_BIN, "sendspin"), "daemon",
            "--name", p["name"] or "HDMI Player",
            "--settings-dir", conf.SENDSPIN_SETTINGS_DIR,
            "--hardware-volume", "false",
            "--hook-start", f"{hook} start",
            "--hook-stop", f"{hook} stop",
            "--manufacturer", "sendspin-hdmi"]
    url = conf.sendspin_url(cfg)
    if url:
        args += ["--url", url]  # connect out to MA; without it the client waits for mDNS discovery
    device = str(p.get("audio_device") or "") or conf.detect_hdmi()
    if device:
        if not p.get("audio_device"):
            print(f"no audio device set: using the connected HDMI sink {device!r}", flush=True)
        args += ["--audio-device", device]
    if p.get("output_delay_ms"):
        args += ["--output-delay-ms", str(int(p["output_delay_ms"]))]
    if p.get("audio_format"):
        args += ["--audio-format", p["audio_format"]]
    print("starting:", " ".join(a if " " not in a else repr(a) for a in args), flush=True)
    os.execv(args[0], args)


if __name__ == "__main__":
    main()
