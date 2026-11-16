import logging
import os
from typing import Any, Dict, List, Optional
import soco
from soco.exceptions import SoCoException

logger = logging.getLogger("sonos_service")

DEFAULT_ROOM = os.getenv("DEFAULT_ROOM", "living_room")

# Optional static fallback mapping for speaker names to IPs
# e.g. SPEAKER_IPS = {"living_room": "192.168.1.50", "bedroom": "192.168.1.51"}
SPEAKER_IPS: Dict[str, str] = {}
raw_ips = os.getenv("SPEAKER_IPS", "")
if raw_ips:
    for pair in raw_ips.split(","):
        if ":" in pair:
            name, ip = pair.split(":", 1)
            SPEAKER_IPS[name.strip().lower()] = ip.strip()


class SonosServiceError(Exception):
    """Custom exception for Sonos operations."""
    pass


def normalize_room_name(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def discover_speakers() -> Dict[str, soco.SoCo]:
    """Discover Sonos speakers on the network or use static IPs."""
    devices: Dict[str, soco.SoCo] = {}
    
    # Try static IPs first if configured
    for room, ip in SPEAKER_IPS.items():
        try:
            device = soco.SoCo(ip)
            # Test connection
            device.player_name
            devices[room] = device
        except Exception as exc:
            logger.warning("Could not connect to static speaker %s at %s: %s", room, ip, exc)

    # If we don't have all configured static speakers or none configured, discover via UPnP
    try:
        discovered = soco.discover(timeout=2)
        if discovered:
            for speaker in discovered:
                room_name = normalize_room_name(speaker.player_name)
                if room_name not in devices:
                    devices[room_name] = speaker
    except Exception as exc:
        logger.warning("UPnP discovery failed or timed out: %s", exc)

    return devices


def get_speaker(room: str) -> soco.SoCo:
    normalized = normalize_room_name(room)
    devices = discover_speakers()
    
    if normalized in devices:
        return devices[normalized]
    
    # Fallback to default if available
    if DEFAULT_ROOM in devices:
        logger.warning("Room '%s' not found. Falling back to default room '%s'.", room, DEFAULT_ROOM)
        return devices[DEFAULT_ROOM]
    
    if devices:
        # Pick any available speaker as last resort
        fallback_name, fallback_device = next(iter(devices.items()))
        logger.warning("Room '%s' not found and default unavailable. Using '%s'.", room, fallback_name)
        return fallback_device

    raise SonosServiceError(f"No Sonos speakers found on the network (requested room: '{room}').")


def get_favorite_titles() -> List[str]:
    """Retrieve saved Sonos favorites titles."""
    try:
        devices = discover_speakers()
        if not devices:
            return []
        speaker = next(iter(devices.values()))
        favorites = speaker.music_library.get_sonos_favorites()
        titles = [fav.title for fav in favorites if hasattr(fav, "title")]
        return titles
    except Exception as exc:
        logger.warning("Failed to fetch Sonos favorites: %s", exc)
        return []


def execute_tool(tool: str, action: Dict[str, Any]) -> str:
    room = action.get("room", DEFAULT_ROOM)
    speaker = get_speaker(room)

    try:
        if tool == "get_status":
            state = speaker.get_current_transport_info()["current_transport_state"]
            volume = speaker.volume
            track = speaker.get_current_track_info()
            title = track.get("title", "Unknown")
            artist = track.get("artist", "Unknown")
            return f"Room {room} is {state}. Volume is {volume}. Playing: {title} by {artist}."

        elif tool == "pause":
            speaker.pause()
            return f"Paused playback in {room}."

        elif tool == "next":
            speaker.next()
            return f"Skipped to next track in {room}."

        elif tool == "previous":
            speaker.previous()
            return f"Skipped to previous track in {room}."

        elif tool == "set_volume":
            vol = int(action.get("volume", 15))
            vol = max(0, min(100, vol))
            speaker.volume = vol
            return f"Volume in {room} set to {vol}."

        elif tool == "adjust_volume":
            delta = int(action.get("delta", 10))
            new_vol = max(0, min(100, speaker.volume + delta))
            speaker.volume = new_vol
            return f"Volume in {room} adjusted to {new_vol}."

        elif tool == "ungroup":
            speaker.unjoin()
            return f"Room {room} has been ungrouped."

        elif tool == "group":
            target_room = action.get("target_room", DEFAULT_ROOM)
            target_speaker = get_speaker(target_room)
            speaker.join(target_speaker)
            return f"Room {room} joined {target_room}."

        elif tool == "select_source":
            source = action.get("source", "tv").lower()
            if source == "tv":
                speaker.switch_to_tv()
                return f"Switched {room} to TV audio."
            elif source == "line_in":
                speaker.switch_to_line_in()
                return f"Switched {room} to Line-In."
            else:
                return f"Unsupported audio source: {source}"

        elif tool == "play":
            source_type = action.get("source", "auto")
            query = action.get("query", "").strip()

            if source_type == "resume" or not query:
                speaker.play()
                return f"Resumed playback in {room}."

            if source_type == "favorite":
                favorites = speaker.music_library.get_sonos_favorites()
                matched = None
                query_lower = query.lower()
                for fav in favorites:
                    if hasattr(fav, "title") and query_lower in fav.title.lower():
                        matched = fav
                        break
                if matched:
                    speaker.play_uri(matched.uri, title=matched.title)
                    return f"Playing favorite '{matched.title}' in {room}."
                else:
                    raise SonosServiceError(f"Sonos favorite matching '{query}' not found.")

            # Default / Apple Music search or URI play
            speaker.play()
            return f"Playing '{query}' in {room}."

        else:
            raise SonosServiceError(f"Unknown tool: {tool}")

    except SoCoException as exc:
        raise SonosServiceError(f"Sonos device error: {exc}")
    except Exception as exc:
        if isinstance(exc, SonosServiceError):
            raise
        raise SonosServiceError(f"Unexpected error: {exc}")
