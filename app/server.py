#!/usr/bin/env python3
"""sendspin-hdmi web service.

- /            the now-playing screen shown by the kiosk on HDMI (also viewable on the LAN)
- /settings    password-protected settings page
- /api/...     JSON for both

Data comes from Music Assistant (ma_source.py). If MA is not reachable, or is not the
one playing on this player, it falls back to the Sendspin client's own MPRIS metadata.
"""
import asyncio
import base64
import os
import re
import socket
import subprocess
import sys
import time

import aiohttp
from aiohttp import web
from dbus_next import Message
from dbus_next.aio import MessageBus

import config as conf
import ma_source

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
VENV_BIN = os.path.dirname(sys.executable)
BUS = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/sendspin-hdmi/bus")
UNITS = {"player": "sendspin-hdmi-player", "web": "sendspin-hdmi-web",
         "kiosk": "sendspin-hdmi-kiosk", "bus": "sendspin-hdmi-bus"}

CFG = conf.load()
now = {"status": "Stopped", "title": "", "artist": "", "album": "", "art": "", "length": 0,
       "position": 0, "updated": time.time()}
art_cache: dict[str, tuple[bytes, str]] = {}
client_id = {"value": ""}

LRC = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def parse_lrc(text: str):
    out = []
    for line in (text or "").splitlines():
        stamps = LRC.findall(line)
        if not stamps:
            continue
        words = LRC.sub("", line).strip()
        for m, s in stamps:
            out.append({"t": int((int(m) * 60 + float(s)) * 1000), "text": words})
    out.sort(key=lambda x: x["t"])
    return out


# ---------------------------------------------------------------- Sendspin identity / MPRIS
async def load_client_id():
    """The Sendspin client id (= protocol id MA knows us by). Created by the player service."""
    path = os.path.join(conf.SENDSPIN_SETTINGS_DIR, "identity-daemon.json")
    while not os.path.exists(path):
        await asyncio.sleep(3)
    try:
        from sendspin.settings import get_client_identity
        ident = await get_client_identity("daemon", conf.SENDSPIN_SETTINGS_DIR)
        client_id["value"] = ident.peer_id
    except Exception as exc:
        print("could not read Sendspin identity:", repr(exc), flush=True)
    return client_id["value"]


def unwrap(v):
    return getattr(v, "value", v)


async def mpris_poll() -> None:
    bus = None
    while True:
        try:
            if bus is None:
                bus = await MessageBus(bus_address=BUS).connect()
            reply = await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                           interface="org.freedesktop.DBus", member="ListNames"))
            names = [n for n in reply.body[0] if n.startswith("org.mpris.MediaPlayer2.")]
            if not names:
                now.update(status="Stopped", title="", artist="", album="", length=0, position=0,
                           updated=time.time())
                await asyncio.sleep(2)
                continue
            if names[0] != now.get("_bus"):  # Sendspin client (re)started
                if now.get("_bus"):
                    ma_source.open_rejoin_window("player restarted")
                now["_bus"] = names[0]
            r = await bus.call(Message(destination=names[0], path="/org/mpris/MediaPlayer2",
                                       interface="org.freedesktop.DBus.Properties", member="GetAll",
                                       signature="s", body=["org.mpris.MediaPlayer2.Player"]))
            props = {k: unwrap(v) for k, v in r.body[0].items()}
            md = {k: unwrap(v) for k, v in (props.get("Metadata") or {}).items()}
            artist = md.get("xesam:artist", "")
            if isinstance(artist, list):
                artist = ", ".join(artist)
            now.update(status=props.get("PlaybackStatus", "Stopped"), title=md.get("xesam:title", "") or "",
                       artist=artist or "", album=md.get("xesam:album", "") or "",
                       length=int(md.get("mpris:length", 0) or 0))
            # MPRIS Position only changes on seek / track / state change: restamp only then.
            raw = (now["title"], now["status"], int(props.get("Position", 0) or 0))
            if raw != now.get("_raw"):
                now.update(position=raw[2], updated=time.time(), _raw=raw)
        except Exception as exc:
            print("MPRIS poll error:", repr(exc), flush=True)
            bus = None
            await asyncio.sleep(3)
            continue
        await asyncio.sleep(0.7)


# ---------------------------------------------------------------- screen API (no auth)
async def page_screen(_req):
    return web.FileResponse(os.path.join(STATIC, "screen.html"), headers={"Cache-Control": "no-cache"})


async def api_now(_req):
    n = ma_source.current()
    if n is None:
        n = {**{k: v for k, v in now.items() if not k.startswith("_")}, "source": "mpris", "key": ""}
    return web.json_response({**n, "age": time.time() - n["updated"]})


async def api_screen(_req):
    s = CFG["screen"]
    return web.json_response({**s, "brand": s["brand"] or CFG["player"]["name"],
                              "player": CFG["player"]["name"]})


async def api_lyrics(req):
    t = ma_source.tracks.get(req.query.get("key", ""))
    if t is None:
        return web.json_response({"synced": [], "plain": ""})
    if t["lyrics"] is None:
        return web.json_response({"synced": [], "plain": "", "pending": True})
    return web.json_response(t["lyrics"])


async def api_wave(req):
    t = ma_source.tracks.get(req.query.get("key", ""))
    return web.json_response({"wave": t["wave"] if t else None})


async def api_art(req):
    """Cover proxy. Only fetches from the configured Music Assistant server."""
    url = req.query.get("u", "")
    base = CFG["ma"]["url"].rstrip("/")
    if not url or not base or not url.startswith(base + "/imageproxy"):
        raise web.HTTPNotFound()
    if url in art_cache:
        body, ctype = art_cache[url]
    else:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    raise web.HTTPNotFound()
                body, ctype = await r.read(), r.content_type or "image/jpeg"
        art_cache[url] = (body, ctype)
        while len(art_cache) > 30:
            art_cache.pop(next(iter(art_cache)))
    return web.Response(body=body, content_type=ctype, headers={"Cache-Control": "max-age=3600"})


# ---------------------------------------------------------------- settings (auth)
PROTECTED = ("/settings", "/api/config", "/api/ma/", "/api/audio/", "/api/status", "/api/hooks/", "/api/restart")


@web.middleware
async def auth_mw(req, handler):
    if req.path.startswith(PROTECTED):
        stored = CFG["web"].get("password_hash")
        if stored:
            ok = False
            hdr = req.headers.get("Authorization", "")
            if hdr.startswith("Basic "):
                try:
                    _, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
                    ok = conf.check_password(pw, stored)
                except Exception:
                    ok = False
            if not ok:
                return web.Response(status=401, text="Login required",
                                    headers={"WWW-Authenticate": 'Basic realm="sendspin-hdmi"'})
    return await handler(req)


async def page_settings(_req):
    return web.FileResponse(os.path.join(STATIC, "settings.html"), headers={"Cache-Control": "no-cache"})


def systemctl(*args, timeout=30):
    try:
        return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return subprocess.CompletedProcess(args, 1, "", str(exc))


def restart_later(*units, delay=0.5):
    async def go():
        await asyncio.sleep(delay)
        await asyncio.to_thread(systemctl, "restart", *units)
    asyncio.create_task(go())


async def api_config_get(_req):
    return web.json_response(conf.public(CFG))


def _changed(old, new, section, keys=None):
    a, b = old.get(section, {}), new.get(section, {})
    if keys is None:
        return a != b
    return any(a.get(k) != b.get(k) for k in keys)


async def api_config_post(req):
    global CFG
    body = await req.json()
    new = conf._merge(CFG, {k: v for k, v in body.items() if k in conf.DEFAULTS})
    # secrets: only replaced when a new value was typed
    new["ma"]["token"] = (body.get("ma") or {}).get("token") or CFG["ma"]["token"]
    new_pw = (body.get("web") or {}).get("new_password")
    new["web"]["password_hash"] = conf.hash_password(new_pw) if new_pw else CFG["web"]["password_hash"]
    new["web"].pop("new_password", None)
    for sect in ("ma", "web"):
        for k in ("token_set", "password_set"):
            new[sect].pop(k, None)
    try:
        new["player"]["output_delay_ms"] = int(new["player"]["output_delay_ms"] or 0)
        new["screen"]["lyric_offset_ms"] = int(new["screen"]["lyric_offset_ms"] or 0)
        new["groups"]["rejoin_window_s"] = max(10, int(new["groups"]["rejoin_window_s"] or 120))
        new["hooks"]["stop_delay_s"] = max(0, int(new["hooks"]["stop_delay_s"] or 0))
        new["web"]["port"] = int(new["web"]["port"] or 8080)
    except (TypeError, ValueError) as exc:
        return web.json_response({"ok": False, "error": f"invalid number: {exc}"}, status=400)
    if not re.fullmatch(r"\d{3,5}x\d{3,5}(@\d+(\.\d+)?Hz)?", new["screen"]["resolution"] or ""):
        return web.json_response({"ok": False, "error": "resolution must look like 1920x1080@60Hz"}, status=400)

    old = CFG
    conf.save(new)
    CFG = new
    actions = []
    if (_changed(old, new, "player") or old["ma"]["sendspin_url"] != new["ma"]["sendspin_url"]
            or conf.sendspin_url(old) != conf.sendspin_url(new)):
        restart_later(UNITS["player"])
        actions.append("player restarted (it rejoins its group by itself)")
    if _changed(old, new, "screen", ["resolution", "enabled"]):
        restart_later(UNITS["kiosk"])
        actions.append("screen restarted")
    if _changed(old, new, "ma", ["url", "token"]) or _changed(old, new, "player", ["name"]) or _changed(old, new, "groups"):
        if ma_source._client:
            ma_source._client.restart(new)
        actions.append("reconnecting to Music Assistant")
    elif ma_source._client:
        ma_source._client.cfg = new
    if old["web"]["port"] != new["web"]["port"]:
        restart_later(UNITS["web"], delay=1.5)
        actions.append(f"settings page moving to port {new['web']['port']}")
    return web.json_response({"ok": True, "actions": actions})


async def api_ma_test(req):
    body = await req.json()
    url = body.get("url") or CFG["ma"]["url"]
    token = body.get("token") or CFG["ma"]["token"]
    if not url or not token:
        return web.json_response({"ok": False, "error": "enter the Music Assistant URL and a token"})
    return web.json_response(await ma_source.test_connection(url, token))


async def api_ma_groups(req):
    c = ma_source._client
    if c is None or not ma_source.state["ok"] and not (c.ws and not c.ws.closed):
        return web.json_response({"ok": False, "error": ma_source.state["error"] or "not connected", "groups": []})
    try:
        if req.method == "POST":
            body = await req.json()
            await c.set_group_member(body["id"], bool(body["member"]))
        return web.json_response({"ok": True, "groups": await c.groups()})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc), "groups": []})


DEV_RE = re.compile(r"^\s*\[(\d+)\]\s+(.+?)\s*$")


def list_devices():
    r = subprocess.run([os.path.join(VENV_BIN, "sendspin"), "audio-devices", "list"],
                       capture_output=True, text=True, timeout=30)
    out = []
    for line in r.stdout.splitlines():
        m = DEV_RE.match(line)
        if m:
            full = m.group(2)
            name = re.sub(r"\s*\(hw:\d+,\d+\)$", "", full)
            out.append({"index": int(m.group(1)), "name": name, "label": full})
    return out


async def api_audio_devices(req):
    rescan = req.query.get("rescan") == "1"
    if rescan:
        await asyncio.to_thread(systemctl, "stop", UNITS["player"])
    try:
        devs = await asyncio.to_thread(list_devices)
    finally:
        if rescan:
            await asyncio.to_thread(systemctl, "start", UNITS["player"])
    return web.json_response({"devices": devs, "current": CFG["player"]["audio_device"], "rescanned": rescan,
                              "auto": conf.detect_hdmi()})


TONE = r"""
import sys, numpy as np, sounddevice as sd
dev = sys.argv[1]
try:
    dev = int(dev)
except ValueError:
    dev = next((i for i, d in enumerate(sd.query_devices()) if d["max_output_channels"] > 0 and d["name"].startswith(dev)), dev or None)
info = sd.query_devices(dev, "output")
sr = int(info["default_samplerate"])
t = np.arange(int(sr * 2.0)) / sr
x = 0.25 * np.sin(2 * np.pi * 440 * t) * np.minimum(1, np.minimum(t, 2.0 - t) * 20)
sd.play(np.column_stack([x, x]).astype("float32"), sr, device=dev, blocking=True)
print("played on", info["name"])
"""


async def api_audio_test(req):
    body = await req.json()
    dev = str(body.get("device", CFG["player"]["audio_device"]) or "") or conf.detect_hdmi()
    await asyncio.to_thread(systemctl, "stop", UNITS["player"])
    try:
        r = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", TONE, dev],
                                    capture_output=True, text=True, timeout=20)
        ok = r.returncode == 0
        msg = (r.stdout.strip() or r.stderr.strip().splitlines()[-1:] or ["?"])
        msg = msg if isinstance(msg, str) else msg[0]
    except Exception as exc:
        ok, msg = False, repr(exc)
    finally:
        await asyncio.to_thread(systemctl, "start", UNITS["player"])
    return web.json_response({"ok": ok, "message": msg})


async def api_hooks_test(req):
    body = await req.json()
    which = "start" if body.get("which") == "start" else "stop"
    r = await asyncio.to_thread(subprocess.run, [sys.executable, os.path.join(HERE, "hook.py"), which, "--now"],
                                capture_output=True, text=True, timeout=30)
    return web.json_response({"ok": r.returncode == 0, "output": (r.stdout + r.stderr).strip()[-2000:]})


def local_ips():
    ips = []
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout
        ips = [i for i in out.split() if ":" not in i]
    except Exception:
        pass
    return ips


async def api_status(_req):
    units = {}
    for k, u in UNITS.items():
        r = await asyncio.to_thread(systemctl, "is-active", u)
        units[k] = r.stdout.strip() or "unknown"
    st = ma_source.state
    n = ma_source.current()
    return web.json_response({
        "units": units, "hostname": socket.gethostname(), "ips": local_ips(),
        "ma": {"connected": st["ok"] or bool(ma_source._client and ma_source._client.ws and not ma_source._client.ws.closed),
               "error": st["error"], "version": st["server_version"],
               "player_id": st["player_id"], "player_name": st["player_name"]},
        "client_id": client_id["value"], "sendspin_url": conf.sendspin_url(CFG),
        "now": {"source": "ma" if n else "mpris", "title": (n or now).get("title"), "status": (n or now).get("status")},
    })


async def api_restart(req):
    body = await req.json()
    which = body.get("which")
    if which not in UNITS:
        return web.json_response({"ok": False, "error": "unknown service"}, status=400)
    restart_later(UNITS[which], delay=1.0 if which == "web" else 0.2)
    return web.json_response({"ok": True})


# ---------------------------------------------------------------- app
async def on_start(app):
    app["mpris"] = asyncio.create_task(mpris_poll())

    async def start_ma():
        cid = await load_client_id()
        await ma_source.MAClient(CFG, cid, parse_lrc).run()
    app["ma"] = asyncio.create_task(start_ma())


def main():
    app = web.Application(middlewares=[auth_mw])
    app.add_routes([
        web.get("/", page_screen),
        web.get("/api/now", api_now),
        web.get("/api/screen", api_screen),
        web.get("/api/lyrics", api_lyrics),
        web.get("/api/wave", api_wave),
        web.get("/art", api_art),
        web.get("/settings", page_settings),
        web.get("/api/config", api_config_get),
        web.post("/api/config", api_config_post),
        web.post("/api/ma/test", api_ma_test),
        web.get("/api/ma/groups", api_ma_groups),
        web.post("/api/ma/groups", api_ma_groups),
        web.get("/api/audio/devices", api_audio_devices),
        web.post("/api/audio/test", api_audio_test),
        web.post("/api/hooks/test", api_hooks_test),
        web.get("/api/status", api_status),
        web.post("/api/restart", api_restart),
        web.static("/static", STATIC),
    ])
    app.on_startup.append(on_start)
    web.run_app(app, host="0.0.0.0", port=int(CFG["web"]["port"]), print=None)


if __name__ == "__main__":
    main()
