"""Centralized configuration and normalization helpers for the Sonos MCP & Voice Agent."""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("sonos_service.config")

DEFAULT_ROOM_SYNONYMS: Dict[str, str] = {
    "living room": "living_room",
    "living_room": "living_room",
    "living": "living_room",
    "סלון": "living_room",
    "בסלון": "living_room",
    "הסלון": "living_room",
    "kids room": "kids_room",
    "kids_room": "kids_room",
    "kids": "kids_room",
    "חדר ילדים": "kids_room",
    "בחדר ילדים": "kids_room",
    "חדר הילדים": "kids_room",
    "בחדר הילדים": "kids_room",
    "bedroom": "bedroom",
    "master bedroom": "bedroom",
    "חדר שינה": "bedroom",
    "בחדר שינה": "bedroom",
    "חדר השינה": "bedroom",
    "בחדר השינה": "bedroom",
}

DEFAULT_FAVORITE_ALIASES: Dict[str, str] = {
    "galgalatz": "glglz",
    "גלגלצ": "glglz",
    'גלגל"צ': "glglz",
    "kan 88": "kan 88",
    "כאן 88": "kan 88",
    "white noise": "pink noise",
    "רעש לבן": "pink noise",
    "רעש ורוד": "pink noise",
}


def normalize_room_name(name: str) -> str:
    """Normalize a human room name into a canonical snake_case identifier."""
    cleaned = re.sub(r"[\s\-]+", "_", name.strip().lower())
    return cleaned.strip("_")


def _validate_ip(raw_ip: str) -> Optional[str]:
    candidate = raw_ip.strip().strip('"').strip("'")
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def parse_speaker_ips(raw: str) -> Dict[str, str]:
    """Parse SPEAKER_IPS supporting both JSON object and comma-separated key:ip / key=ip formats."""
    text = (raw or "").strip()
    if not text:
        return {}

    parsed: Dict[str, str] = {}

    # 1. Try JSON object first (matches .env.example)
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                logger.warning("SPEAKER_IPS JSON must be an object; ignoring.")
                return {}
            for raw_room, raw_ip in data.items():
                room = normalize_room_name(str(raw_room))
                ip = _validate_ip(str(raw_ip))
                if room and ip:
                    parsed[room] = ip
                else:
                    logger.warning("Ignoring invalid SPEAKER_IPS entry: %r -> %r", raw_room, raw_ip)
            return parsed
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse SPEAKER_IPS as JSON (%s); trying delimited fallback.", exc)

    # 2. Fallback: comma-separated room:ip or room=ip
    for entry in text.split(","):
        item = entry.strip()
        if not item:
            continue
        sep = "=" if "=" in item else (":" if ":" in item else None)
        if not sep:
            logger.warning("Ignoring malformed SPEAKER_IPS entry: %r", item)
            continue
        raw_room, raw_ip = item.split(sep, 1)
        room = normalize_room_name(raw_room.strip().strip('"').strip("'"))
        ip = _validate_ip(raw_ip)
        if room and ip:
            parsed[room] = ip
        else:
            logger.warning("Ignoring invalid SPEAKER_IPS entry: %r", item)

    return parsed


def _parse_json_str_dict(raw: str) -> Dict[str, str]:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return {str(k).strip().lower(): str(v).strip() for k, v in data.items() if str(k).strip()}
    except Exception as exc:
        logger.warning("Ignoring invalid JSON mapping (%s): %r", exc, text[:80])
    return {}


def _parse_max_volumes(raw: str) -> Dict[str, int]:
    text = (raw or "").strip()
    if not text:
        return {}
    result: Dict[str, int] = {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for room_key, cap in data.items():
                room = normalize_room_name(str(room_key))
                if not room:
                    continue
                try:
                    result[room] = max(0, min(100, int(cap)))
                except (TypeError, ValueError):
                    logger.warning("Ignoring invalid volume cap for %s: %r", room, cap)
    except Exception as exc:
        logger.warning("Ignoring invalid MAX_VOLUME_BY_ROOM JSON (%s).", exc)
    return result


def _parse_optional_hour(raw: Optional[str]) -> Optional[int]:
    if raw is None or not str(raw).strip():
        return None
    try:
        hour = int(str(raw).strip())
        if 0 <= hour <= 23:
            return hour
    except ValueError:
        pass
    logger.warning("Ignoring invalid quiet hour value: %r (expected 0-23).", raw)
    return None


def _parse_csv_list(raw: str) -> List[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Runtime settings loaded from environment variables."""

    default_room: str = "living_room"
    speaker_ips: Dict[str, str] = field(default_factory=dict)
    room_synonyms: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ROOM_SYNONYMS))
    favorite_aliases: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_FAVORITE_ALIASES))

    # Network & caching
    soco_request_timeout: float = 5.0
    discovery_timeout: float = 2.0
    probe_timeout: float = 2.0
    cache_ttl_seconds: int = 180
    favorites_cache_ttl_seconds: int = 300
    apple_music_country: str = "IL"

    # Volume guardrails
    max_volume_by_room: Dict[str, int] = field(default_factory=dict)
    quiet_hours_start: Optional[int] = None
    quiet_hours_end: Optional[int] = None
    quiet_hours_max_volume: int = 35

    # Ollama voice router
    ollama_url: str = "http://host.docker.internal:11434/api/chat"
    ollama_model: str = "qwen2.5:3b"
    ollama_timeout: float = 30.0
    ollama_connect_timeout: float = 3.0
    ollama_num_ctx: int = 4096
    ollama_temperature: float = 0.1
    ollama_think: bool = False

    # Security & runtime
    sonos_api_key: Optional[str] = None
    allowed_origins: List[str] = field(default_factory=list)
    allowed_hosts: List[str] = field(default_factory=list)
    scenes_path: str = "scenes.json"
    log_level: str = "INFO"


def load_settings(env: Optional[Dict[str, str]] = None) -> Settings:
    """Build a Settings object from environment variables (defaults to os.environ)."""
    source = os.environ if env is None else env

    default_room = normalize_room_name(source.get("DEFAULT_ROOM", "living_room")) or "living_room"
    speaker_ips = parse_speaker_ips(source.get("SPEAKER_IPS", ""))

    room_synonyms = dict(DEFAULT_ROOM_SYNONYMS)
    for k, v in _parse_json_str_dict(source.get("ROOM_SYNONYMS", "")).items():
        room_synonyms[k] = normalize_room_name(v)

    favorite_aliases = dict(DEFAULT_FAVORITE_ALIASES)
    for k, v in _parse_json_str_dict(source.get("FAVORITE_ALIASES", "")).items():
        favorite_aliases[k] = v.lower()

    api_key = (source.get("SONOS_API_KEY") or "").strip() or None

    return Settings(
        default_room=default_room,
        speaker_ips=speaker_ips,
        room_synonyms=room_synonyms,
        favorite_aliases=favorite_aliases,
        soco_request_timeout=float(source.get("SOCO_REQUEST_TIMEOUT", source.get("SOCO_HTTP_TIMEOUT", "5.0"))),
        discovery_timeout=float(source.get("DISCOVERY_TIMEOUT", "2.0")),
        probe_timeout=float(source.get("PROBE_TIMEOUT", "2.0")),
        cache_ttl_seconds=int(source.get("CACHE_TTL_SECONDS", "180")),
        favorites_cache_ttl_seconds=int(source.get("FAVORITES_CACHE_TTL_SECONDS", "300")),
        apple_music_country=source.get("APPLE_MUSIC_COUNTRY", "IL").strip().upper() or "IL",
        max_volume_by_room=_parse_max_volumes(source.get("MAX_VOLUME_BY_ROOM", "")),
        quiet_hours_start=_parse_optional_hour(source.get("QUIET_HOURS_START")),
        quiet_hours_end=_parse_optional_hour(source.get("QUIET_HOURS_END")),
        quiet_hours_max_volume=max(0, min(100, int(source.get("QUIET_HOURS_MAX_VOLUME", "35")))),
        ollama_url=source.get("OLLAMA_URL", "http://host.docker.internal:11434/api/chat").strip(),
        ollama_model=source.get("OLLAMA_MODEL", "qwen2.5:3b").strip() or "qwen2.5:3b",
        ollama_timeout=float(source.get("OLLAMA_TIMEOUT", "30.0")),
        ollama_connect_timeout=float(source.get("OLLAMA_CONNECT_TIMEOUT", "3.0")),
        ollama_num_ctx=int(source.get("OLLAMA_NUM_CTX", "4096")),
        ollama_temperature=float(source.get("OLLAMA_TEMPERATURE", "0.1")),
        ollama_think=source.get("OLLAMA_THINK", "false").strip().lower() in ("1", "true", "yes"),
        sonos_api_key=api_key,
        allowed_origins=_parse_csv_list(source.get("ALLOWED_ORIGINS", "")),
        allowed_hosts=_parse_csv_list(source.get("ALLOWED_HOSTS", "")),
        scenes_path=source.get("SCENES_PATH", "scenes.json").strip() or "scenes.json",
        log_level=source.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
