"""Music Assistant as the data source for the now-playing screen.

Talks to MA's websocket API with a long-lived token and keeps, for this player's active
queue item: title / artist / album / cover / position, MA's own lyrics
(metadata.lrc_lyrics / metadata.lyrics) and MA's real waveform (audio_analysis/wave_form,
1800 RMS bins).
"""
import asyncio
import time
import urllib.parse

import aiohttp

state = {
    "ok": False, "seen": 0.0, "now": None, "error": "",
    "player_id": "", "player_name": "", "server_version": "",
}
tracks: dict[str, dict] = {}  # queue_item_id -> {"lyrics": {...}|None, "wave": [...]|None}

_client = None  # the running MAClient


def find_player(players, client_id, name):
    """MA player for this Sendspin client: by protocol id, then by player id, then by name."""
    if client_id:
        for p in players:
            for proto in p.get("output_protocols") or []:
                if proto.get("output_protocol_id") == client_id:
                    return p
        for p in players:
            if p.get("player_id") == client_id:
                return p
    for p in players:
        if name and name in (p.get("name"), p.get("display_name")) and p.get("type") != "group":
            return p
    return None


class MAError(RuntimeError):
    pass


class MAClient:
    def __init__(self, cfg, client_id, parse_lrc):
        global _client
        _client = self
        self.cfg = cfg
        self.client_id = client_id
        self.parse_lrc = parse_lrc
        self.ws = None
        self.session = None
        self.n = 0
        self.lock = asyncio.Lock()
        self.reconnect = asyncio.Event()

    @property
    def base(self):
        return self.cfg["ma"]["url"].rstrip("/")

    async def call(self, cmd, **args):
        if self.ws is None or self.ws.closed:
            raise MAError("not connected to Music Assistant")
        async with self.lock:
            self.n += 1
            mid = str(self.n)
            await self.ws.send_json({"message_id": mid, "command": cmd, "args": args})
            while True:
                r = await asyncio.wait_for(self.ws.receive_json(), 15)
                if r.get("message_id") == mid:
                    if r.get("error_code"):
                        raise MAError(f"{cmd}: {r.get('details')}")
                    return r.get("result")

    async def connect(self):
        if not self.base or not self.cfg["ma"]["token"]:
            raise MAError("Music Assistant URL or token not set")
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(f"{self.base}/ws", max_msg_size=0, heartbeat=30)
        info = (await self.ws.receive()).json()
        state["server_version"] = info.get("server_version", "")
        await self.call("auth", token=self.cfg["ma"]["token"])

    async def close(self):
        try:
            if self.ws:
                await self.ws.close()
            if self.session:
                await self.session.close()
        except Exception:
            pass
        self.ws = self.session = None

    def restart(self, cfg):
        """Called by the web layer after the settings changed."""
        self.cfg = cfg
        self.reconnect.set()

    def image_url(self, img):
        if not img:
            return ""
        if img.get("proxy_id"):
            return f"{self.base}/imageproxy/{img['proxy_id']}?size=512&fmt=jpg"
        if img.get("path"):
            q = urllib.parse.urlencode({"path": img["path"], "provider": img.get("provider", ""), "size": 512})
            return f"{self.base}/imageproxy?{q}"
        return ""

    async def locate_player(self):
        players = await self.call("players/all") or []
        p = find_player(players, self.client_id, self.cfg["player"]["name"])
        state["player_id"] = p["player_id"] if p else ""
        state["player_name"] = (p.get("display_name") or p.get("name")) if p else ""
        return players, p

    async def fetch_track_extras(self, key, ci):
        info = tracks[key]
        mi = ci.get("media_item") or {}
        sd = ci.get("streamdetails") or {}
        for attempt in range(40):
            if key not in tracks:
                return
            try:
                if info["lyrics"] is None:
                    md = mi.get("metadata") or {}
                    if not (md.get("lrc_lyrics") or md.get("lyrics")) and attempt and mi.get("item_id"):
                        try:
                            item = await self.call("music/item", media_type=mi.get("media_type") or "track",
                                                   item_id=mi["item_id"],
                                                   provider_instance_id_or_domain=mi.get("provider"))
                            md = (item or {}).get("metadata") or {}
                        except MAError:  # radio stream etc.
                            md = {}
                    if md.get("lrc_lyrics") or md.get("lyrics") or attempt >= 3:
                        info["lyrics"] = {"synced": self.parse_lrc(md.get("lrc_lyrics") or ""),
                                          "plain": md.get("lyrics") or ""}
                if info["wave"] is None and sd.get("item_id"):
                    # MA's own UI asks with the streamdetails ids (the provider that streams it)
                    bins = await self.call("audio_analysis/wave_form", item_id=sd["item_id"],
                                           provider_instance_id_or_domain=sd.get("provider"))
                    if bins:
                        info["wave"] = [round(float(b), 3) for b in bins]
            except Exception as exc:
                print("MA track extras error:", repr(exc), flush=True)
            if info["lyrics"] is not None and (info["wave"] is not None or not sd.get("item_id")):
                return
            await asyncio.sleep(15)

    async def poll_once(self):
        pid = state["player_id"]
        if not pid:
            return
        q = await self.call("player_queues/get_active_queue", player_id=pid) or {}
        ci = q.get("current_item") or {}
        mi = ci.get("media_item") or {}
        st = {"playing": "Playing", "paused": "Paused"}.get(q.get("state"), "Stopped")
        key = ci.get("queue_item_id") or ""
        if key and key not in tracks:
            tracks[key] = {"lyrics": None, "wave": None}
            while len(tracks) > 30:
                tracks.pop(next(iter(tracks)))
            asyncio.create_task(self.fetch_track_extras(key, ci))
        artists = mi.get("artists") or []
        album = mi.get("album") if isinstance(mi.get("album"), dict) else {}
        state["now"] = {
            "source": "ma", "key": key, "status": st if key else "Stopped",
            "title": mi.get("name") or ci.get("name") or "",
            "artist": ", ".join(a.get("name", "") for a in artists if a.get("name")),
            "album": (album or {}).get("name", ""),
            "art": self.image_url(ci.get("image") or mi.get("image")),
            "length": int(float(ci.get("duration") or mi.get("duration") or 0) * 1e6),
            "position": int(float(q.get("elapsed_time") or 0) * 1e6),
            "updated": float(q.get("elapsed_time_last_updated") or time.time()),
        }
        state["ok"] = True
        state["seen"] = time.time()

    async def saved_members(self, group_id):
        cfg = await self.call("config/players/get", player_id=group_id) or {}
        return ((cfg.get("values") or {}).get("group_members") or {}).get("value") or []

    async def groups(self):
        """Sync groups and whether this player is a saved member (settings page)."""
        players, me = await self.locate_player()
        out = []
        for g in players:
            if g.get("type") != "group":
                continue
            members = await self.saved_members(g["player_id"])
            out.append({"id": g["player_id"], "name": g.get("display_name") or g.get("name"),
                        "member": bool(me and me["player_id"] in members)})
        return sorted(out, key=lambda x: (x["name"] or "").lower())

    async def set_group_member(self, group_id, member):
        _, me = await self.locate_player()
        if not me:
            raise MAError("this player is not known to Music Assistant yet")
        members = list(await self.saved_members(group_id))
        if member and me["player_id"] not in members:
            members.append(me["player_id"])
        elif not member and me["player_id"] in members:
            members.remove(me["player_id"])
        else:
            return
        await self.call("config/players/save", player_id=group_id, values={"group_members": members})

    async def run(self):
        while True:
            self.reconnect.clear()
            try:
                await self.connect()
                await self.locate_player()
                state["error"] = "" if state["player_id"] else "connected, but this player is not in Music Assistant yet"
                print(f"MA connected ({state['server_version']}), player {state['player_id'] or '?'}", flush=True)
                while not self.reconnect.is_set():
                    if not state["player_id"]:
                        await self.locate_player()
                        if state["player_id"]:
                            state["error"] = ""
                    await self.poll_once()
                    try:
                        await asyncio.wait_for(self.reconnect.wait(), 1)
                    except asyncio.TimeoutError:
                        pass
            except Exception as exc:
                state["error"] = str(exc) or repr(exc)
                print("MA error:", repr(exc), flush=True)
            state["ok"] = False
            await self.close()
            if not self.reconnect.is_set():
                try:
                    await asyncio.wait_for(self.reconnect.wait(), 10)
                except asyncio.TimeoutError:
                    pass


def current():
    """MA's view if it is fresh and actually playing/paused on this player, else None."""
    n = state.get("now")
    if state["ok"] and n and time.time() - state["seen"] < 5 and n["status"] in ("Playing", "Paused"):
        return n
    return None


async def test_connection(url, token):
    """One-off check for the settings page's Test button."""
    url = url.rstrip("/")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.ws_connect(f"{url}/ws", max_msg_size=0) as ws:
                info = (await asyncio.wait_for(ws.receive(), 5)).json()
                await ws.send_json({"message_id": "1", "command": "auth", "args": {"token": token}})
                r = await asyncio.wait_for(ws.receive_json(), 5)
                if r.get("error_code"):
                    return {"ok": False, "error": r.get("details") or "authentication failed"}
                return {"ok": True, "version": info.get("server_version", ""), "name": info.get("name", "")}
    except Exception as exc:
        return {"ok": False, "error": f"cannot reach {url}: {exc!r}"}
