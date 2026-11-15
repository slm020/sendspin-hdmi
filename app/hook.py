#!/usr/bin/env python3
"""Playback start/stop hooks (called by the Sendspin client: --hook-start / --hook-stop).

    hook.py start          run the "start" action now, cancel any pending stop
    hook.py stop           run the "stop" action after hooks.stop_delay_s, unless playback
                           starts again first (so pauses / track changes don't turn the amp off)
    hook.py stop --now     run the "stop" action immediately (settings page Test button)

Action types (config "hooks.start" / "hooks.stop"):
    none      do nothing
    command   run a shell command (gets SENDSPIN_* env vars from the player)
    webhook   HTTP request, e.g. a Home Assistant webhook automation
    denon     Denon / Marantz AV receiver over telnet (port 23): power, input, sound mode
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as conf  # noqa: E402

RUN_DIR = "/run/sendspin-hdmi"
TOKEN_FILE = os.path.join(RUN_DIR, "stop-token")


LOG_FILE = os.path.join(conf.STATE_DIR, "hooks.log")


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(f"[hook] {msg}", flush=True)
    try:  # Sendspin only shows hook output when a hook fails, so keep our own short log
        lines = open(LOG_FILE).read().splitlines()[-199:] if os.path.exists(LOG_FILE) else []
        with open(LOG_FILE, "w") as f:
            f.write("\n".join(lines + [line]) + "\n")
    except OSError:
        pass


def denon(action, which):
    host = action.get("denon_host", "").strip()
    if not host:
        raise ValueError("Denon host not set")
    cmds = []
    if which == "start":
        if action.get("denon_power", True):
            cmds.append("PWON")
        if action.get("denon_input"):
            cmds.append("SI" + action["denon_input"].strip().upper())
        if action.get("denon_mode"):
            cmds.append("MS" + action["denon_mode"].strip().upper())
    else:
        if action.get("denon_power", True):
            cmds.append("PWSTANDBY")
    with socket.create_connection((host, 23), timeout=5) as s:
        for i, c in enumerate(cmds):
            s.sendall((c + "\r").encode())
            time.sleep(2.5 if c == "PWON" else 0.6)  # receivers ignore commands while waking
    log(f"denon {host}: {' '.join(cmds) or 'nothing to send'}")


def run_action(which):
    cfg = conf.load()
    action = cfg["hooks"][which]
    kind = action.get("type", "none")
    if kind == "none":
        log(f"{which}: no action configured")
    elif kind == "command":
        if action.get("command", "").strip():
            r = subprocess.run(action["command"], shell=True, capture_output=True, text=True, timeout=60)
            log(f"{which}: command exit {r.returncode} {r.stdout.strip()} {r.stderr.strip()}".strip())
            if r.returncode:
                sys.exit(r.returncode)
    elif kind == "webhook":
        url = action.get("url", "").strip()
        if not url:
            raise ValueError("webhook URL not set")
        method = (action.get("method") or "POST").upper()
        data = action.get("body", "").encode() if method != "GET" else None
        req = urllib.request.Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json" if data.strip().startswith(b"{") else "text/plain")
        with urllib.request.urlopen(req, timeout=10) as r:
            log(f"{which}: webhook {method} {url} -> HTTP {r.status}")
    elif kind == "denon":
        denon(action, which)
    else:
        log(f"{which}: unknown action type {kind!r}")


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else ""
    os.makedirs(RUN_DIR, exist_ok=True)
    if which == "start":
        log("start: playback started")
        with open(TOKEN_FILE, "w") as f:
            f.write("started")
        run_action("start")
    elif which == "stop" and "--now" in sys.argv:
        run_action("stop")
    elif which == "stop":
        token = f"{time.time():.6f}"
        with open(TOKEN_FILE, "w") as f:
            f.write(token)
        # detach so the player is not blocked while we wait
        # Detach completely (no shared stdout/stderr): Sendspin waits for the hook's
        # output pipes to close, so an inherited pipe would block it for the whole delay.
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "_stop_later", token],
                         start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log(f"stop: action in {conf.load()['hooks']['stop_delay_s']}s unless playback resumes")
    elif which == "_stop_later":
        token = sys.argv[2]
        time.sleep(conf.load()["hooks"]["stop_delay_s"])
        try:
            current = open(TOKEN_FILE).read().strip()
        except FileNotFoundError:
            current = ""
        if current == token:
            run_action("stop")
        else:
            log("stop cancelled: playback started again")
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"error: {exc}")
        sys.exit(1)
