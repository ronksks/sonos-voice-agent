import json
import os
import socket
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import requests
from soco import SoCo, config
from soco.data_structures import to_didl_string
from soco.plugins.sharelink import ShareLinkPlugin

config.HTTP_TIMEOUT = 3.0
socket.setdefaulttimeout(3.0)

app = FastAPI()

DEFAULT_SPEAKERS = '{"living_room": "192.168.1.100", "kids_room": "192.168.1.101", "bedroom": "192.168.1.102"}'
SPEAKER_IPS = json.loads(os.getenv("SPEAKER_IPS", DEFAULT_SPEAKERS))
DEFAULT_ROOM = os.getenv("DEFAULT_ROOM", "living_room")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")

ALIASES = {
    "galgalatz": "glglz",
    "גלגלצ": "glglz",
    "kan 88": "kan 88",
    "כאן 88": "kan 88",
    "white noise": "pink noise",
    "רעש לבן": "pink noise"
}

def get_seed_ip():
    return SPEAKER_IPS.get(DEFAULT_ROOM) or next(iter(SPEAKER_IPS.values()))

def get_soco_device(room: str):
    try:
        zones = SoCo(get_seed_ip()).all_zones
        for zone in zones:
            normalized = zone.player_name.lower().replace(" ", "_")
            if normalized == room:
                return zone
    except Exception:
        pass

    ip = SPEAKER_IPS.get(room)
    if ip:
        return SoCo(ip)

    raise HTTPException(status_code=404, detail=f"Unknown room: {room}")

def get_favorite_titles():
    try:
        favs = SoCo(get_seed_ip()).music_library.get_sonos_favorites()
        return [f.title for f in favs if getattr(f, "title", None)]
    except Exception:
        return []

def play_favorite(device, query: str):
    coord = device.group.coordinator
    favs = device.music_library.get_sonos_favorites()
    q = query.lower().strip()
    q = ALIASES.get(q, q)
    match = next((f for f in favs if q in f.title.lower()), None)
    if not match:
        return None
    target = getattr(match, "reference", None) or match
    try:
        coord.clear_queue()
        coord.add_to_queue(target)
        coord.play_from_queue(0)
    except Exception:
        uri = target.get_uri()
        meta = getattr(match, "resource_meta_data", "")
        if not meta:
            try:
                meta = to_didl_string(target)
            except Exception:
                meta = ""
        coord.play_uri(uri, meta=meta)
    return match.title

def play_apple_music(device, query: str):
    coord = device.group.coordinator
    res = requests.get(
        "https://itunes.apple.com/search",
        params={"term": query, "media": "music", "entity": "song", "limit": 5, "country": "IL"},
        timeout=5
    )
    res.raise_for_status()
    results = res.json().get("results", [])
    urls = [r.get("trackViewUrl") for r in results if r.get("trackViewUrl")]
    if not urls:
        return None
    plugin = ShareLinkPlugin(coord)
    coord.clear_queue()
    added = 0
    for url in urls:
        try:
            plugin.add_share_link_to_queue(url)
            added += 1
        except Exception:
            continue
    if added == 0:
        return None
    coord.play_from_queue(0)
    first = results[0]
    return f"{first.get('trackName', query)} by {first.get('artistName', 'Unknown')}"

def try_favorite_safe(device, query: str):
    try:
        return play_favorite(device, query)
    except Exception as e:
        print(f"==> Favorite playback fallback triggered for '{query}': {repr(e)}", flush=True)
        return None

def execute_tool(name: str, args: dict):
    room = args.get("room", DEFAULT_ROOM)
    device = get_soco_device(room)

    if name == "get_status":
        state = device.group.coordinator.get_current_transport_info()["current_transport_state"]
        vol = device.volume
        return f"Room {room}: state={state}, volume={vol}"

    if name == "pause":
        device.group.coordinator.pause()
        return f"Room {room}: playback paused"

    if name == "set_volume":
        vol = max(0, min(100, int(args.get("volume", 15))))
        device.volume = vol
        return f"Room {room}: volume set to {vol}"

    if name == "play":
        query = str(args.get("query", "")).strip()
        source = str(args.get("source", "auto")).strip().lower()

        if not query or source == "resume":
            device.group.coordinator.play()
            return f"Room {room}: resumed playback"

        if source == "favorite":
            title = try_favorite_safe(device, query)
            if title:
                return f"Room {room}: playing favorite {title}"
            am_title = play_apple_music(device, query)
            if am_title:
                return f"Room {room}: playing {am_title} on Apple Music"
            return f"Room {room}: could not find {query}"

        if source == "apple_music":
            am_title = play_apple_music(device, query)
            if am_title:
                return f"Room {room}: playing {am_title} on Apple Music"
            title = try_favorite_safe(device, query)
            if title:
                return f"Room {room}: playing favorite {title}"
            return f"Room {room}: could not find {query}"

        title = try_favorite_safe(device, query)
        if title:
            return f"Room {room}: playing favorite {title}"
        am_title = play_apple_music(device, query)
        if am_title:
            return f"Room {room}: playing {am_title} on Apple Music"
        return f"Room {room}: could not find {query}"

    return f"Unknown tool: {name}"

class CommandRequest(BaseModel):
    command: str

@app.post("/command")
def handle_command(req: CommandRequest):
    print(f"==> Received command: {req.command}", flush=True)
    try:
        favorites = get_favorite_titles()
        fav_list = ", ".join(favorites) if favorites else "Pink Noise, GLGLZ, KAN 88"

        system_prompt = (
            "You translate voice commands in English or Hebrew into Sonos JSON actions. "
            "Valid rooms: living_room (living room, סלון), kids_room (kids room, חדר ילדים), bedroom (bedroom, חדר שינה). Default room is living_room. "
            "Valid tools: get_status, play, pause, set_volume. "
            f"Saved Sonos favorites and radio stations: [{fav_list}]. "
            "Rules for play: "
            "1. If the user asks for white noise or רעש לבן, set source to favorite and query to Pink Noise. "
            "2. If the user asks for Galgalatz or גלגלצ, set source to favorite and query to GLGLZ. "
            "3. If the user asks for any radio station or saved favorite, match the closest name from the saved favorites list and set source to favorite. "
            "4. If the user asks for an artist, band, song, or album like The Eagles or Led Zeppelin, set source to apple_music and query to the artist or song name. "
            "5. If the user only says play or resume without naming content, set source to resume and query to empty string. "
            "Reply ONLY with valid JSON matching: "
            '{"tool": "get_status|play|pause|set_volume", "room": "living_room|kids_room|bedroom", "volume": 0, "source": "favorite|apple_music|resume", "query": ""}'
        )

        payload = {
            "model": OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": req.command}
            ],
            "format": "json",
            "options": {"temperature": 0},
            "stream": False,
            "keep_alive": -1
        }

        res = requests.post(OLLAMA_URL, json=payload, timeout=45)
        res.raise_for_status()
        content = res.json().get("message", {}).get("content", "{}")
        print(f"==> Qwen JSON: {content}", flush=True)

        parsed = json.loads(content)
        tool_name = parsed.get("tool")
        if not tool_name:
            return {"reply": "Could not determine action from command.", "model_used": OLLAMA_MODEL}

        result = execute_tool(tool_name, parsed)
        return {"reply": result, "model_used": OLLAMA_MODEL}

    except Exception as e:
        print(f"==> Error in handle_command: {repr(e)}", flush=True)
        return {"reply": f"Error: {str(e)}", "model_used": "error-handler"}
