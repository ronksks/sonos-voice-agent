import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import requests
from soco import SoCo, config as soco_config, discovery
from soco.data_structures import to_didl_string
from soco.plugins.sharelink import ShareLinkPlugin

logger = logging.getLogger("sonos_agent.sonos")

SOCO_HTTP_TIMEOUT = float(os.getenv("SOCO_HTTP_TIMEOUT", "5.0"))
soco_config.HTTP_TIMEOUT = SOCO_HTTP_TIMEOUT

DEFAULT_ROOM = os.getenv("DEFAULT_ROOM", "living_room").strip().lower().replace(" ", "_") or "living_room"
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "300"))
DISCOVERY_TIMEOUT = float(os.getenv("DISCOVERY_TIMEOUT", "2.0"))
APPLE_MUSIC_COUNTRY = os.getenv("APPLE_MUSIC_COUNTRY", "IL")

ALIASES: Dict[str, str] = {
    "galgalatz": "glglz",
    "גלגלצ": "glglz",
    "kan 88": "kan 88",
    "כאן 88": "kan 88",
    "white noise": "pink noise",
    "רעש לבן": "pink noise",
}


class SonosServiceError(Exception):
    """Base error for Sonos operations."""


class NoSpeakersError(SonosServiceError):
    """Raised when no Sonos speakers are configured or discovered."""


class RoomNotFoundError(SonosServiceError):
    """Raised when a requested room cannot be resolved."""


def normalize_room_name(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def _load_speaker_ips() -> Dict[str, str]:
    raw = os.getenv("SPEAKER_IPS", "{}")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            logger.warning("SPEAKER_IPS must be a JSON object; ignoring invalid value.")
            return {}
        return {
            normalize_room_name(str(k)): str(v).strip()
            for k, v in data.items()
            if str(k).strip() and str(v).strip()
        }
    except Exception as exc:
        logger.warning("Failed to parse SPEAKER_IPS JSON (%s); relying on UPnP discovery.", exc)
        return {}


SPEAKER_IPS: Dict[str, str] = _load_speaker_ips()

_http_session = requests.Session()
_speakers_lock = threading.Lock()
_speakers_cache: Dict[str, SoCo] = {}
_speakers_cache_ts: float = 0.0

_favorites_lock = threading.Lock()
_favorites_cache: List[Any] = []
_favorites_cache_ts: float = 0.0


def reset_caches() -> None:
    """Clear speaker and favorites caches (useful for tests or manual refresh)."""
    global _speakers_cache, _speakers_cache_ts, _favorites_cache, _favorites_cache_ts
    with _speakers_lock:
        _speakers_cache = {}
        _speakers_cache_ts = 0.0
    with _favorites_lock:
        _favorites_cache = []
        _favorites_cache_ts = 0.0


def discover_speakers(force_refresh: bool = False) -> Dict[str, SoCo]:
    """Return a room->SoCo map using static IPs, unicast zone expansion, and UPnP multicast."""
    global _speakers_cache, _speakers_cache_ts

    now = time.monotonic()
    with _speakers_lock:
        if (
            not force_refresh
            and _speakers_cache
            and (now - _speakers_cache_ts) < CACHE_TTL_SECONDS
        ):
            return dict(_speakers_cache)

    devices: Dict[str, SoCo] = {}

    # 1. Seed from static SPEAKER_IPS (works reliably across Docker bridge NAT).
    for room, ip in SPEAKER_IPS.items():
        try:
            devices[room] = SoCo(ip)
        except Exception as exc:
            logger.warning("Failed to initialize SoCo for %s (%s): %s", room, ip, exc)

    # 2. Expand topology via unicast HTTP from a configured seed IP.
    if devices:
        seed = devices.get(DEFAULT_ROOM) or next(iter(devices.values()))
        try:
            for zone in seed.all_zones:
                try:
                    name = normalize_room_name(zone.player_name)
                    devices.setdefault(name, zone)
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("Unicast zone expansion from %s skipped: %s", getattr(seed, "ip_address", "?"), exc)

    # 3. Multicast UPnP discovery (works on host/LAN; harmless no-op in bridge mode).
    if force_refresh or not devices:
        try:
            found = discovery.discover(timeout=DISCOVERY_TIMEOUT)
            if found:
                for zone in found:
                    try:
                        name = normalize_room_name(zone.player_name)
                        devices.setdefault(name, zone)
                    except Exception:
                        continue
        except Exception as exc:
            logger.debug("Multicast UPnP discovery failed: %s", exc)

    with _speakers_lock:
        if devices:
            _speakers_cache = devices
            _speakers_cache_ts = time.monotonic()
        return dict(_speakers_cache if _speakers_cache else devices)


def get_seed_device() -> SoCo:
    devices = discover_speakers()
    if DEFAULT_ROOM in devices:
        return devices[DEFAULT_ROOM]
    if devices:
        return next(iter(devices.values()))
    devices = discover_speakers(force_refresh=True)
    if DEFAULT_ROOM in devices:
        return devices[DEFAULT_ROOM]
    if devices:
        return next(iter(devices.values()))
    raise NoSpeakersError(
        "No Sonos speakers found. Configure SPEAKER_IPS in .env or ensure UPnP multicast is reachable."
    )


def get_soco_device(room: Optional[str]) -> SoCo:
    target_room = normalize_room_name(room) if room and str(room).strip() else DEFAULT_ROOM
    devices = discover_speakers()
    if target_room in devices:
        return devices[target_room]

    # Retry once with forced refresh in case a speaker booted or renewed DHCP.
    devices = discover_speakers(force_refresh=True)
    if target_room in devices:
        return devices[target_room]

    if not devices:
        raise NoSpeakersError(
            "No Sonos speakers found. Configure SPEAKER_IPS in .env or ensure UPnP multicast is reachable."
        )

    available = ", ".join(sorted(devices.keys()))
    raise RoomNotFoundError(f"Unknown room '{target_room}'. Available rooms: {available}")


def get_favorites(force_refresh: bool = False) -> List[Any]:
    """Fetch and cache Sonos favorites to avoid duplicate UPnP round-trips per command."""
    global _favorites_cache, _favorites_cache_ts

    now = time.monotonic()
    with _favorites_lock:
        if (
            not force_refresh
            and _favorites_cache
            and (now - _favorites_cache_ts) < CACHE_TTL_SECONDS
        ):
            return list(_favorites_cache)

    try:
        seed = get_seed_device()
        favs = list(seed.music_library.get_sonos_favorites())
    except Exception as exc:
        logger.warning("Failed to fetch Sonos favorites: %s", exc)
        with _favorites_lock:
            return list(_favorites_cache)

    with _favorites_lock:
        _favorites_cache = favs
        _favorites_cache_ts = time.monotonic()
        return list(favs)


def get_favorite_titles() -> List[str]:
    return [f.title for f in get_favorites() if getattr(f, "title", None)]


def play_favorite(device: SoCo, query: str) -> Optional[str]:
    coord = device.group.coordinator
    q = ALIASES.get(query.lower().strip(), query.lower().strip())
    if not q:
        return None

    favs = get_favorites()
    match = next((f for f in favs if getattr(f, "title", None) and q in f.title.lower()), None)
    if not match:
        favs = get_favorites(force_refresh=True)
        match = next((f for f in favs if getattr(f, "title", None) and q in f.title.lower()), None)
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


def try_favorite_safe(device: SoCo, query: str) -> Optional[str]:
    try:
        return play_favorite(device, query)
    except Exception as exc:
        logger.warning("Favorite playback fallback triggered for '%s': %r", query, exc)
        return None


def play_apple_music(device: SoCo, query: str) -> Optional[str]:
    coord = device.group.coordinator
    try:
        res = _http_session.get(
            "https://itunes.apple.com/search",
            params={
                "term": query,
                "media": "music",
                "entity": "song",
                "limit": 5,
                "country": APPLE_MUSIC_COUNTRY,
            },
            timeout=5,
        )
        res.raise_for_status()
        results = res.json().get("results", [])
    except requests.RequestException as exc:
        logger.warning("iTunes Search API request failed for '%s': %s", query, exc)
        return None
    except ValueError as exc:
        logger.warning("iTunes Search API returned invalid JSON for '%s': %s", query, exc)
        return None

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
        except Exception as exc:
            logger.debug("ShareLinkPlugin failed for URL %s: %s", url, exc)
            continue
    if added == 0:
        return None

    coord.play_from_queue(0)
    first = results[0]
    return f"{first.get('trackName', query)} by {first.get('artistName', 'Unknown')}"


def _safe_int(value: Any, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().rstrip("%")
    if not text:
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def execute_tool(name: str, args: Dict[str, Any]) -> str:
    room = args.get("room") or DEFAULT_ROOM
    device = get_soco_device(room)
    resolved_room = normalize_room_name(str(room))

    if name == "get_status":
        info = device.group.coordinator.get_current_transport_info()
        state = info.get("current_transport_state", "UNKNOWN")
        vol = device.volume
        return f"Room {resolved_room}: state={state}, volume={vol}"

    if name == "pause":
        device.group.coordinator.pause()
        return f"Room {resolved_room}: playback paused"

    if name == "next":
        device.group.coordinator.next()
        return f"Room {resolved_room}: skipped to next track"

    if name == "previous":
        device.group.coordinator.previous()
        return f"Room {resolved_room}: returned to previous track"

    if name == "set_volume":
        vol = max(0, min(100, _safe_int(args.get("volume"), 15)))
        device.volume = vol
        return f"Room {resolved_room}: volume set to {vol}"

    if name == "adjust_volume":
        delta = _safe_int(args.get("delta"), 5)
        current = int(device.volume)
        new_vol = max(0, min(100, current + delta))
        device.volume = new_vol
        return f"Room {resolved_room}: volume adjusted to {new_vol}"

    if name == "group":
        target_room = args.get("target_room") or DEFAULT_ROOM
        target_device = get_soco_device(target_room)
        target_coord = target_device.group.coordinator
        if device.ip_address == target_coord.ip_address:
            return f"Room {resolved_room} is already the coordinator of {normalize_room_name(str(target_room))}"
        device.join(target_coord)
        return f"Room {resolved_room}: joined {normalize_room_name(str(target_room))}"

    if name == "ungroup":
        device.unjoin()
        return f"Room {resolved_room}: ungrouped"

    if name == "select_source":
        source = str(args.get("source") or "tv").strip().lower()
        coord = device.group.coordinator
        if source in ("tv", "hdmi", "spdif"):
            coord.switch_to_tv()
            return f"Room {resolved_room}: switched source to TV"
        if source in ("line_in", "line-in", "aux"):
            coord.switch_to_line_in()
            return f"Room {resolved_room}: switched source to Line-In"
        return execute_tool("play", args)

    if name == "play":
        query = str(args.get("query") or "").strip()
        source = str(args.get("source") or "auto").strip().lower()

        if not query or source == "resume":
            device.group.coordinator.play()
            return f"Room {resolved_room}: resumed playback"

        if source == "apple_music":
            am_title = play_apple_music(device, query)
            if am_title:
                return f"Room {resolved_room}: playing {am_title} on Apple Music"
            title = try_favorite_safe(device, query)
            if title:
                return f"Room {resolved_room}: playing favorite {title}"
            return f"Room {resolved_room}: could not find {query}"

        # Both "favorite" and "auto" try favorites first, then Apple Music.
        title = try_favorite_safe(device, query)
        if title:
            return f"Room {resolved_room}: playing favorite {title}"
        am_title = play_apple_music(device, query)
        if am_title:
            return f"Room {resolved_room}: playing {am_title} on Apple Music"
        return f"Room {resolved_room}: could not find {query}"

    return f"Unknown tool: {name}"
