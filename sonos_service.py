"""Core Sonos discovery, topology caching, coordinator-aware control, and media service."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import difflib
import logging
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, TypeVar

import requests
import soco
from soco import config as soco_config, discovery
from soco.data_structures import to_didl_string
from soco.exceptions import NotSupportedException, SoCoException, SoCoSlaveException, SoCoUPnPException
from soco.plugins.sharelink import ShareLinkPlugin

from config import Settings, load_settings, normalize_room_name
from models import (
    ActionResult,
    FavoriteItem,
    FavoritesList,
    GroupResult,
    GroupTopology,
    HouseholdState,
    InputSource,
    MediaSource,
    PlaybackAction,
    QueueInsertMode,
    RoomState,
    SourceKind,
    TrackInfo,
    TransportState,
    TvAudioSettings,
    VolumeResult,
)

logger = logging.getLogger("sonos_service")

T = TypeVar("T")

_NETWORK_EXCEPTIONS = (requests.RequestException, ConnectionError, TimeoutError, OSError)


class SonosServiceError(Exception):
    """Base structured exception for Sonos service operations."""

    def __init__(self, message: str, *, code: str = "sonos_error", details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class NoSpeakersError(SonosServiceError):
    """Raised when no Sonos speakers can be discovered or reached."""

    def __init__(self, message: str, *, details: Optional[Dict[str, Any]] = None):
        super().__init__(message, code="no_speakers", details=details)


class RoomNotFoundError(SonosServiceError):
    """Raised when a requested room does not match any visible speaker."""

    def __init__(
        self,
        requested_room: str,
        available_rooms: List[str],
        suggestion: Optional[str] = None,
    ):
        hint = f" Did you mean '{suggestion}'?" if suggestion else ""
        avail_str = ", ".join(available_rooms) if available_rooms else "none"
        message = f"Unknown room '{requested_room}'. Available rooms: [{avail_str}].{hint}"
        super().__init__(
            message,
            code="room_not_found",
            details={
                "requested_room": requested_room,
                "available_rooms": available_rooms,
                "suggestion": suggestion,
            },
        )
        self.requested_room = requested_room
        self.available_rooms = available_rooms
        self.suggestion = suggestion


class UnsupportedOperationError(SonosServiceError):
    """Raised when an operation is unsupported on the target speaker or current stream."""

    def __init__(self, message: str, *, details: Optional[Dict[str, Any]] = None):
        super().__init__(message, code="unsupported_operation", details=details)


class MediaNotFoundError(SonosServiceError):
    """Raised when requested favorite or catalog content cannot be found."""

    def __init__(self, message: str, *, details: Optional[Dict[str, Any]] = None):
        super().__init__(message, code="media_not_found", details=details)


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


def _is_visible_speaker(device: Any) -> bool:
    """Return False for bonded surround satellites and Sub units sharing a room name."""
    try:
        return bool(getattr(device, "is_visible", True))
    except Exception:
        return True


def _is_soundbar(device: Any) -> bool:
    """Detect whether a speaker is a Sonos home-theater soundbar (Arc, Beam, Ray, Playbar, Playbase)."""
    try:
        if getattr(device, "is_soundbar", False):
            return True
    except Exception:
        pass
    try:
        info = device.get_speaker_info() or {}
        model = str(info.get("model_name", "")).lower()
        return any(k in model for k in ("arc", "beam", "ray", "playbar", "playbase"))
    except Exception:
        return False


def _upnp_error_code(exc: Exception) -> Optional[str]:
    code = getattr(exc, "error_code", None)
    if code is not None:
        return str(code)
    text = str(exc)
    for known in ("701", "711", "714", "800"):
        if f"Error {known}" in text or f"UPnP Error {known}" in text:
            return known
    return None


def _get_coordinator(speaker: Any) -> Any:
    """Safely return the group coordinator for a speaker, falling back to speaker itself."""
    try:
        group = getattr(speaker, "group", None)
        if group is not None and getattr(group, "coordinator", None) is not None:
            return group.coordinator
    except Exception as exc:
        logger.debug("Could not resolve coordinator for %s: %s", getattr(speaker, "ip_address", "?"), exc)
    return speaker


def _should_replace_room_entry(existing: Any, candidate: Any) -> bool:
    """Resolve room-name collisions (e.g., bonded satellites) by preferring visible soundbars/coordinators."""
    existing_vis = _is_visible_speaker(existing)
    cand_vis = _is_visible_speaker(candidate)
    if cand_vis and not existing_vis:
        return True
    if existing_vis and not cand_vis:
        return False
    if _is_soundbar(candidate) and not _is_soundbar(existing):
        return True
    try:
        if getattr(candidate, "is_coordinator", False) and not getattr(existing, "is_coordinator", False):
            return True
    except Exception:
        pass
    return False


class SpeakerRegistry:
    """Thread-safe Sonos speaker discovery, unicast topology expansion, and TTL caching."""

    def __init__(
        self,
        settings: Settings,
        *,
        soco_factory: Callable[[str], Any] = soco.SoCo,
        discover_fn: Callable[..., Optional[Iterable[Any]]] = discovery.discover,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self._soco_factory = soco_factory
        self._discover_fn = discover_fn
        self._clock = clock

        self._lock = threading.RLock()
        self._by_room: Dict[str, Any] = {}
        self._by_uid: Dict[str, Any] = {}
        self._cache_ts: float = 0.0
        self._last_discovery_source: str = "uninitialized"

    def resolve_room_alias(self, raw_room: Optional[str]) -> str:
        """Resolve an English/Hebrew alias or raw string to a canonical snake_case room ID."""
        if raw_room is None or not str(raw_room).strip():
            return self.settings.default_room
        cleaned = str(raw_room).strip().lower()
        if cleaned in self.settings.room_synonyms:
            return self.settings.room_synonyms[cleaned]
        normalized = normalize_room_name(cleaned)
        if normalized in self.settings.room_synonyms:
            return self.settings.room_synonyms[normalized]
        return normalized

    def invalidate(self) -> None:
        """Clear cached speakers so the next lookup performs fresh discovery."""
        with self._lock:
            self._by_room = {}
            self._by_uid = {}
            self._cache_ts = 0.0
            self._last_discovery_source = "invalidated"

    def cache_info(self) -> Dict[str, Any]:
        """Return cache telemetry for health diagnostics."""
        with self._lock:
            age = round(self._clock() - self._cache_ts, 2) if self._cache_ts > 0 else None
            return {
                "cached_rooms": sorted(self._by_room.keys()),
                "cache_age_seconds": age,
                "cache_ttl_seconds": self.settings.cache_ttl_seconds,
                "last_discovery_source": self._last_discovery_source,
            }

    def _probe_static_ip(self, configured_room: str, ip: str) -> Optional[Tuple[str, Any]]:
        try:
            device = self._soco_factory(ip)
            # Trigger a fast lightweight property fetch to verify reachability
            _ = device.player_name
            return configured_room, device
        except Exception as exc:
            logger.warning("Static speaker '%s' at %s unreachable: %s", configured_room, ip, exc)
            return None

    def _register_candidate(self, devices: Dict[str, Any], room_key: str, speaker: Any) -> None:
        if not room_key:
            return
        if room_key not in devices or _should_replace_room_entry(devices[room_key], speaker):
            devices[room_key] = speaker

    def discover(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Discover visible Sonos speakers using static seeds, unicast visible_zones, and SSDP fallback."""
        now = self._clock()
        with self._lock:
            if (
                not force_refresh
                and self._by_room
                and (now - self._cache_ts) < self.settings.cache_ttl_seconds
            ):
                return dict(self._by_room)

        devices: Dict[str, Any] = {}
        source_tags: List[str] = []
        healthy_seeds: List[Tuple[str, Any]] = []

        # 1. Parallel probe of configured static seed IPs (works across Docker bridge NAT)
        if self.settings.speaker_ips:
            max_workers = min(8, max(1, len(self.settings.speaker_ips)))
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="sonos-probe") as pool:
                futures = {
                    pool.submit(self._probe_static_ip, room, ip): (room, ip)
                    for room, ip in self.settings.speaker_ips.items()
                }
                for fut in as_completed(futures):
                    res = fut.result()
                    if res is not None:
                        healthy_seeds.append(res)

        # 2. Unicast topology expansion via visible_zones (gets live DHCP IPs & filters bonded satellites)
        if healthy_seeds:
            source_tags.append("static_seed")
            expanded = False
            # Prefer default room seed if healthy
            ordered_seeds = sorted(
                healthy_seeds,
                key=lambda item: 0 if item[0] == self.settings.default_room else 1,
            )
            for _, seed in ordered_seeds:
                try:
                    zones = getattr(seed, "visible_zones", None)
                    if zones is None:
                        zones = getattr(seed, "all_zones", None)
                    if zones:
                        for zone in zones:
                            if not _is_visible_speaker(zone):
                                continue
                            try:
                                room_name = normalize_room_name(zone.player_name)
                                self._register_candidate(devices, room_name, zone)
                                expanded = True
                            except Exception:
                                continue
                        if expanded:
                            source_tags.append("visible_zones")
                            break
                except Exception as exc:
                    logger.debug(
                        "Unicast visible_zones expansion failed on %s: %s",
                        getattr(seed, "ip_address", "?"),
                        exc,
                    )

            # Register any healthy static seeds not yet mapped (respecting visibility)
            for configured_room, seed in healthy_seeds:
                if not _is_visible_speaker(seed):
                    continue
                if configured_room not in devices:
                    self._register_candidate(devices, configured_room, seed)

        # 3. Multicast UPnP SSDP discovery only if no devices found or forced refresh missed configured rooms
        missing_configured = set(self.settings.speaker_ips.keys()) - set(devices.keys())
        if not devices or (force_refresh and missing_configured):
            try:
                found = self._discover_fn(timeout=self.settings.discovery_timeout)
                if found:
                    source_tags.append("ssdp")
                    for zone in found:
                        if not _is_visible_speaker(zone):
                            continue
                        try:
                            room_name = normalize_room_name(zone.player_name)
                            self._register_candidate(devices, room_name, zone)
                        except Exception:
                            continue
            except Exception as exc:
                logger.debug("SSDP multicast discovery failed or timed out: %s", exc)

        by_uid: Dict[str, Any] = {}
        for spk in devices.values():
            uid = getattr(spk, "uid", None)
            if uid:
                by_uid[str(uid)] = spk

        with self._lock:
            if devices:
                self._by_room = devices
                self._by_uid = by_uid
                self._cache_ts = self._clock()
                self._last_discovery_source = "+".join(source_tags) or "unknown"
            elif force_refresh:
                self._by_room = {}
                self._by_uid = {}
                self._cache_ts = 0.0
                self._last_discovery_source = "empty"
            return dict(self._by_room)

    def get_speaker(self, room: Optional[str]) -> Tuple[str, Any]:
        """Resolve a room name strictly to a visible SoCo speaker (never silently falls back)."""
        canonical = self.resolve_room_alias(room)
        devices = self.discover(force_refresh=False)
        if canonical in devices:
            return canonical, devices[canonical]

        # Retry once with forced refresh in case a speaker rebooted or renewed DHCP
        devices = self.discover(force_refresh=True)
        if canonical in devices:
            return canonical, devices[canonical]

        if not devices:
            raise NoSpeakersError(
                "No Sonos speakers found. Configure SPEAKER_IPS in .env or ensure UPnP multicast is reachable.",
                details={"requested_room": canonical},
            )

        available = sorted(devices.keys())
        matches = difflib.get_close_matches(canonical, available, n=1, cutoff=0.5)
        raise RoomNotFoundError(canonical, available, suggestion=matches[0] if matches else None)


class SonosService:
    """High-level, coordinator-aware, typed service over SoCo speakers."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        registry: Optional[SpeakerRegistry] = None,
        http_get: Optional[Callable[..., Any]] = None,
        sharelink_factory: Callable[[Any], Any] = ShareLinkPlugin,
        clock: Callable[[], float] = time.monotonic,
        now_fn: Callable[[], datetime] = datetime.now,
    ):
        self.settings = settings or load_settings()
        # Configure real SoCo timeout attribute (soco.config.REQUEST_TIMEOUT)
        soco_config.REQUEST_TIMEOUT = self.settings.soco_request_timeout

        self.registry = registry or SpeakerRegistry(self.settings, clock=clock)
        self._http_get = http_get
        self._sharelink_factory = sharelink_factory
        self._clock = clock
        self._now_fn = now_fn

        self._thread_local = threading.local()
        self._favorites_lock = threading.RLock()
        self._favorites_cache: List[Any] = []
        self._favorites_ts: float = 0.0

    def _get_requests_session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            self._thread_local.session = session
        return session

    def reset_caches(self) -> None:
        """Clear both speaker topology and favorites caches."""
        self.registry.invalidate()
        with self._favorites_lock:
            self._favorites_cache = []
            self._favorites_ts = 0.0

    def _run_with_recovery(
        self,
        room: Optional[str],
        op: Callable[[str, Any], T],
        *,
        retry_on_network_error: bool = True,
    ) -> T:
        """Execute a speaker operation with automatic one-shot topology refresh on IP drift/drop."""
        resolved_room, speaker = self.registry.get_speaker(room)
        try:
            return op(resolved_room, speaker)
        except _NETWORK_EXCEPTIONS as exc:
            if not retry_on_network_error:
                raise SonosServiceError(
                    f"Speaker '{resolved_room}' is unreachable: {exc}",
                    code="speaker_unreachable",
                    details={"room": resolved_room},
                ) from exc
            logger.warning(
                "Network error communicating with '%s' (%s): %s. Refreshing Sonos topology...",
                resolved_room,
                getattr(speaker, "ip_address", "?"),
                exc,
            )
            self.registry.discover(force_refresh=True)
            resolved_room, fresh_speaker = self.registry.get_speaker(resolved_room)
            try:
                return op(resolved_room, fresh_speaker)
            except _NETWORK_EXCEPTIONS as retry_exc:
                raise SonosServiceError(
                    f"Speaker '{resolved_room}' remains unreachable after rediscovery: {retry_exc}",
                    code="speaker_unreachable",
                    details={"room": resolved_room},
                ) from retry_exc
        except SonosServiceError:
            raise
        except SoCoSlaveException as exc:
            raise SonosServiceError(
                f"Operation on '{resolved_room}' must target its group coordinator: {exc}",
                code="slave_exception",
                details={"room": resolved_room},
            ) from exc
        except NotSupportedException as exc:
            raise UnsupportedOperationError(
                f"Operation not supported on '{resolved_room}': {exc}",
                details={"room": resolved_room},
            ) from exc
        except SoCoException as exc:
            raise SonosServiceError(
                f"Sonos device error on '{resolved_room}': {exc}",
                code="soco_error",
                details={"room": resolved_room, "upnp_code": _upnp_error_code(exc)},
            ) from exc

    # ------------------------------------------------------------------
    # Discovery & State Inspection
    # ------------------------------------------------------------------

    def list_rooms(self, force_refresh: bool = False) -> List[str]:
        return sorted(self.registry.discover(force_refresh=force_refresh).keys())

    def _detect_source_kind(self, speaker: Any, coord: Any, track_info: Dict[str, Any], state: str) -> SourceKind:
        try:
            if getattr(coord, "is_playing_tv", False) or getattr(speaker, "is_playing_tv", False):
                return "tv"
        except Exception:
            pass
        try:
            if getattr(coord, "is_playing_line_in", False) or getattr(speaker, "is_playing_line_in", False):
                return "line_in"
        except Exception:
            pass

        uri = str(track_info.get("uri") or "")
        if uri.startswith("x-sonos-htastream:"):
            return "tv"
        if uri.startswith("x-rincon-stream:"):
            return "line_in"
        if any(
            uri.startswith(prefix)
            for prefix in ("x-sonosapi-stream:", "x-sonosapi-radio:", "x-rincon-mp3radio:", "aac://", "hls-radio://")
        ):
            return "radio"
        if uri or state in ("PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING"):
            return "music"
        return "idle"

    def _build_room_state(self, canonical_room: str, speaker: Any) -> RoomState:
        coord = _get_coordinator(speaker)
        display_name = str(getattr(speaker, "player_name", canonical_room))
        uid = getattr(speaker, "uid", None)
        ip = getattr(speaker, "ip_address", None)

        model_name: Optional[str] = None
        try:
            info = speaker.get_speaker_info() or {}
            model_name = info.get("model_name")
        except Exception:
            model_name = None

        soundbar = _is_soundbar(speaker)

        try:
            transport = coord.get_current_transport_info() or {}
            raw_state = str(transport.get("current_transport_state", "UNKNOWN")).upper()
        except Exception:
            raw_state = "UNKNOWN"

        valid_states = ("PLAYING", "PAUSED_PLAYBACK", "STOPPED", "TRANSITIONING", "UNKNOWN")
        state: TransportState = raw_state if raw_state in valid_states else "UNKNOWN"  # type: ignore[assignment]

        try:
            volume = max(0, min(100, int(speaker.volume)))
        except Exception:
            volume = 0

        try:
            muted = bool(speaker.mute)
        except Exception:
            muted = False

        # Group topology
        coord_name = normalize_room_name(str(getattr(coord, "player_name", canonical_room)))
        members: List[str] = [canonical_room]
        try:
            group = getattr(speaker, "group", None)
            if group is not None and getattr(group, "members", None):
                visible_members = [
                    normalize_room_name(str(m.player_name))
                    for m in group.members
                    if _is_visible_speaker(m)
                ]
                if visible_members:
                    members = sorted(set(visible_members))
        except Exception:
            pass

        try:
            raw_track = coord.get_current_track_info() or {}
        except Exception:
            raw_track = {}

        source_type = self._detect_source_kind(speaker, coord, raw_track, state)
        track: Optional[TrackInfo] = None
        title = (raw_track.get("title") or "").strip() or None
        artist = (raw_track.get("artist") or "").strip() or None
        album = (raw_track.get("album") or "").strip() or None
        uri = (raw_track.get("uri") or "").strip() or None
        if title or artist or uri:
            track = TrackInfo(
                title=title,
                artist=artist,
                album=album,
                position=(raw_track.get("position") or "").strip() or None,
                duration=(raw_track.get("duration") or "").strip() or None,
                uri=uri,
            )

        sleep_timer_seconds: Optional[int] = None
        try:
            remaining = coord.get_sleep_timer()
            if remaining is not None:
                sleep_timer_seconds = int(remaining)
        except Exception:
            pass

        tv_audio: Optional[TvAudioSettings] = None
        if soundbar:
            try:
                tv_audio = TvAudioSettings(
                    night_mode=getattr(speaker, "night_mode", None),
                    speech_enhancement=getattr(speaker, "dialog_mode", None),
                    sub_gain=getattr(speaker, "sub_gain", None),
                    surround_level=getattr(speaker, "surround_volume_tv", None),
                )
            except Exception:
                tv_audio = None

        return RoomState(
            room=canonical_room,
            display_name=display_name,
            uid=str(uid) if uid else None,
            ip=str(ip) if ip else None,
            model_name=model_name,
            online=True,
            state=state,
            volume=volume,
            muted=muted,
            coordinator=coord_name,
            is_coordinator=(coord_name == canonical_room),
            group_members=members,
            source_type=source_type,
            track=track,
            is_soundbar=soundbar,
            sleep_timer_seconds=sleep_timer_seconds,
            tv_audio=tv_audio,
        )

    def get_room_state(self, room: Optional[str] = None) -> RoomState:
        """Fetch structured playback, volume, group, and capability state for a single room."""
        return self._run_with_recovery(room, self._build_room_state)

    def get_household_state(self, room: Optional[str] = None) -> HouseholdState:
        """Inspect one room or all visible Sonos rooms in parallel."""
        if room is not None and str(room).strip():
            single = self.get_room_state(room)
            groups = [GroupTopology(coordinator=single.coordinator, members=single.group_members, state=single.state)]
            return HouseholdState(default_room=self.settings.default_room, rooms=[single], groups=groups)

        devices = self.registry.discover(force_refresh=False)
        if not devices:
            devices = self.registry.discover(force_refresh=True)
        if not devices:
            raise NoSpeakersError(
                "No Sonos speakers found. Configure SPEAKER_IPS in .env or ensure UPnP multicast is reachable."
            )

        states: List[RoomState] = []

        def _safe_inspect(room_name: str, spk: Any) -> RoomState:
            try:
                return self._build_room_state(room_name, spk)
            except Exception as exc:
                logger.warning("Failed to inspect room '%s': %s", room_name, exc)
                return RoomState(
                    room=room_name,
                    display_name=str(getattr(spk, "player_name", room_name)),
                    ip=str(getattr(spk, "ip_address", "")) or None,
                    online=False,
                    coordinator=room_name,
                    group_members=[room_name],
                )

        max_workers = min(8, max(1, len(devices)))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="sonos-status") as pool:
            futs = {pool.submit(_safe_inspect, r, spk): r for r, spk in sorted(devices.items())}
            for fut in as_completed(futs):
                states.append(fut.result())

        states.sort(key=lambda s: s.room)

        seen_coords: Dict[str, GroupTopology] = {}
        for st in states:
            if st.coordinator not in seen_coords:
                seen_coords[st.coordinator] = GroupTopology(
                    coordinator=st.coordinator,
                    members=st.group_members or [st.room],
                    state=st.state,
                )

        return HouseholdState(
            default_room=self.settings.default_room,
            rooms=states,
            groups=sorted(seen_coords.values(), key=lambda g: g.coordinator),
        )

    # ------------------------------------------------------------------
    # Favorites & Apple Music Search
    # ------------------------------------------------------------------

    def _get_seed_speaker(self) -> Any:
        devices = self.registry.discover(force_refresh=False)
        if not devices:
            devices = self.registry.discover(force_refresh=True)
        if not devices:
            raise NoSpeakersError(
                "No Sonos speakers found. Configure SPEAKER_IPS in .env or ensure UPnP multicast is reachable."
            )
        return devices.get(self.settings.default_room) or next(iter(devices.values()))

    def get_favorites(self, force_refresh: bool = False) -> List[Any]:
        """Fetch and TTL-cache Sonos Favorites to avoid repeated UPnP round-trips."""
        now = self._clock()
        with self._favorites_lock:
            if (
                not force_refresh
                and self._favorites_cache
                and (now - self._favorites_ts) < self.settings.favorites_cache_ttl_seconds
            ):
                return list(self._favorites_cache)

        try:
            seed = self._get_seed_speaker()
            favs = list(seed.music_library.get_sonos_favorites())
        except Exception as exc:
            logger.warning("Failed to fetch Sonos favorites: %s", exc)
            with self._favorites_lock:
                return list(self._favorites_cache)

        with self._favorites_lock:
            self._favorites_cache = favs
            self._favorites_ts = self._clock()
            return list(favs)

    def get_favorite_titles(self, force_refresh: bool = False) -> List[str]:
        return [str(f.title) for f in self.get_favorites(force_refresh=force_refresh) if getattr(f, "title", None)]

    def list_favorites(self, force_refresh: bool = False) -> FavoritesList:
        favs = self.get_favorites(force_refresh=force_refresh)
        items: List[FavoriteItem] = []
        for fav in favs:
            title = getattr(fav, "title", None)
            if not title:
                continue
            ref = getattr(fav, "reference", None) or fav
            uri: Optional[str] = None
            try:
                uri = ref.get_uri() if hasattr(ref, "get_uri") else getattr(ref, "uri", None)
            except Exception:
                uri = None
            items.append(
                FavoriteItem(
                    title=str(title),
                    description=getattr(fav, "description", None),
                    uri=str(uri) if uri else None,
                )
            )
        return FavoritesList(count=len(items), favorites=items)

    def _find_favorite_match(self, query: str) -> Optional[Any]:
        normalized = query.strip().lower()
        aliased = self.settings.favorite_aliases.get(normalized, normalized)
        if not aliased:
            return None

        def _search(favs: List[Any]) -> Optional[Any]:
            # Exact title match first
            for fav in favs:
                title = getattr(fav, "title", None)
                if title and title.strip().lower() == aliased:
                    return fav
            # Substring match next
            for fav in favs:
                title = getattr(fav, "title", None)
                if title and aliased in title.lower():
                    return fav
            # Fuzzy fallback
            titles_map = {str(f.title).lower(): f for f in favs if getattr(f, "title", None)}
            close = difflib.get_close_matches(aliased, list(titles_map.keys()), n=1, cutoff=0.72)
            if close:
                return titles_map[close[0]]
            return None

        match = _search(self.get_favorites(force_refresh=False))
        if match is not None:
            return match
        return _search(self.get_favorites(force_refresh=True))

    def _play_favorite_on_coordinator(self, coord: Any, query: str, mode: QueueInsertMode = "replace") -> Optional[str]:
        match = self._find_favorite_match(query)
        if match is None:
            return None

        target = getattr(match, "reference", None) or match

        # Prefer queue insertion for playlists/albums/tracks, with play_uri fallback for live streams
        try:
            if mode == "replace":
                coord.clear_queue()
                coord.add_to_queue(target)
                coord.play_from_queue(0)
            elif mode == "play_next":
                pos = self._next_queue_position(coord)
                coord.add_to_queue(target, position=pos)
            else:  # append
                coord.add_to_queue(target)
            return str(match.title)
        except Exception as queue_exc:
            if mode != "replace":
                raise UnsupportedOperationError(
                    f"Favorite '{match.title}' is a live stream and cannot be queued with mode='{mode}'."
                ) from queue_exc
            logger.debug("Queue playback failed for favorite '%s' (%s); falling back to play_uri.", match.title, queue_exc)
            uri = target.get_uri() if hasattr(target, "get_uri") else getattr(target, "uri", None)
            if not uri:
                raise SonosServiceError(f"Favorite '{match.title}' has no playable URI.", code="invalid_favorite")
            meta = getattr(match, "resource_meta_data", "") or ""
            if not meta:
                try:
                    meta = to_didl_string(target)
                except Exception:
                    meta = ""
            coord.play_uri(uri, meta=meta, title=str(match.title))
            return str(match.title)

    def _next_queue_position(self, coord: Any) -> int:
        """Compute 1-based SoCo queue insertion index immediately after the currently playing track."""
        try:
            track = coord.get_current_track_info() or {}
            pos = int(track.get("playlist_position", 0))
            return max(1, pos + 1)
        except Exception:
            return 1

    def _search_apple_music_tracks(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        getter = self._http_get or self._get_requests_session().get
        try:
            res = getter(
                "https://itunes.apple.com/search",
                params={
                    "term": query,
                    "media": "music",
                    "entity": "song",
                    "limit": limit,
                    "country": self.settings.apple_music_country,
                },
                timeout=5.0,
            )
            res.raise_for_status()
            return list(res.json().get("results", []))
        except requests.RequestException as exc:
            logger.warning("iTunes Search API request failed for '%s': %s", query, exc)
            return []
        except ValueError as exc:
            logger.warning("iTunes Search API returned invalid JSON for '%s': %s", query, exc)
            return []

    def _play_apple_music_on_coordinator(
        self,
        coord: Any,
        query: str,
        mode: QueueInsertMode = "replace",
        limit: int = 5,
    ) -> Optional[Dict[str, Any]]:
        results = self._search_apple_music_tracks(query, limit=limit)
        tracks = [r for r in results if r.get("trackViewUrl")]
        if not tracks:
            return None

        plugin = self._sharelink_factory(coord)
        if mode == "replace":
            coord.clear_queue()

        added = 0
        insert_pos = self._next_queue_position(coord) if mode == "play_next" else 0
        for idx, track in enumerate(tracks):
            url = track["trackViewUrl"]
            try:
                if mode == "play_next":
                    plugin.add_share_link_to_queue(url, position=insert_pos + idx)
                else:
                    plugin.add_share_link_to_queue(url)
                added += 1
            except TypeError:
                # Some ShareLinkPlugin versions/mocks don't accept position kwarg
                try:
                    plugin.add_share_link_to_queue(url)
                    added += 1
                except Exception as inner_exc:
                    logger.debug("ShareLinkPlugin failed for URL %s: %s", url, inner_exc)
            except Exception as exc:
                logger.debug("ShareLinkPlugin failed for URL %s: %s", url, exc)

        if added == 0:
            return None

        if mode == "replace":
            coord.play_from_queue(0)

        first = tracks[0]
        return {
            "title": first.get("trackName", query),
            "artist": first.get("artistName", "Unknown"),
            "tracks_added": added,
        }

    # ------------------------------------------------------------------
    # Playback, Volume, Grouping & Source Controls
    # ------------------------------------------------------------------

    def control_playback(self, action: PlaybackAction, room: Optional[str] = None) -> ActionResult:
        """Execute coordinator-aware playback transport control (resume/pause/stop/next/previous)."""

        def _op(resolved_room: str, speaker: Any) -> ActionResult:
            coord = _get_coordinator(speaker)
            if action == "resume":
                try:
                    coord.play()
                except SoCoUPnPException as exc:
                    if _upnp_error_code(exc) in ("701", "714"):
                        raise UnsupportedOperationError(
                            f"Nothing queued or resumable in '{resolved_room}' (UPnP {_upnp_error_code(exc)})."
                        ) from exc
                    raise
                msg = f"Resumed playback in {resolved_room}."

            elif action == "pause":
                try:
                    transport = coord.get_current_transport_info() or {}
                    cur_state = str(transport.get("current_transport_state", "")).upper()
                except Exception:
                    cur_state = ""

                if cur_state in ("PAUSED_PLAYBACK", "STOPPED"):
                    msg = f"Playback in {resolved_room} is already paused/stopped."
                else:
                    try:
                        coord.pause()
                        msg = f"Paused playback in {resolved_room}."
                    except SoCoUPnPException as exc:
                        if _upnp_error_code(exc) == "701":
                            # Live radio or TV stream cannot pause cleanly; try stop()
                            try:
                                coord.stop()
                                msg = f"Stopped stream playback in {resolved_room}."
                            except SoCoUPnPException:
                                msg = f"Playback in {resolved_room} is already stopped."
                        else:
                            raise

            elif action == "stop":
                try:
                    coord.stop()
                except SoCoUPnPException as exc:
                    if _upnp_error_code(exc) != "701":
                        raise
                msg = f"Stopped playback in {resolved_room}."

            elif action in ("next", "previous"):
                try:
                    if action == "next":
                        coord.next()
                        msg = f"Skipped to next track in {resolved_room}."
                    else:
                        coord.previous()
                        msg = f"Returned to previous track in {resolved_room}."
                except SoCoUPnPException as exc:
                    if _upnp_error_code(exc) in ("701", "711"):
                        raise UnsupportedOperationError(
                            f"Cannot skip {action} in '{resolved_room}' (active source may be live radio, TV, or end of queue)."
                        ) from exc
                    raise
            else:
                raise UnsupportedOperationError(f"Unsupported playback action: {action}")

            return ActionResult(
                ok=True,
                action=action,
                room=resolved_room,
                message=msg,
                state=self._build_room_state(resolved_room, speaker),
            )

        return self._run_with_recovery(room, _op)

    def play_media(
        self,
        query: str,
        room: Optional[str] = None,
        source: MediaSource = "auto",
        mode: QueueInsertMode = "replace",
    ) -> ActionResult:
        """Search and play or queue a Sonos Favorite or Apple Music track."""
        cleaned_query = (query or "").strip()
        if not cleaned_query:
            return self.control_playback("resume", room=room)

        def _op(resolved_room: str, speaker: Any) -> ActionResult:
            coord = _get_coordinator(speaker)
            verb = {"replace": "Playing", "play_next": "Queued next", "append": "Appended to queue"}[mode]

            if source == "apple_music":
                am = self._play_apple_music_on_coordinator(coord, cleaned_query, mode=mode)
                if am:
                    msg = f"{verb} '{am['title']}' by {am['artist']} on Apple Music in {resolved_room}."
                    return ActionResult(
                        ok=True,
                        action="play_media",
                        room=resolved_room,
                        message=msg,
                        state=self._build_room_state(resolved_room, speaker),
                        details={"source": "apple_music", "mode": mode, **am},
                    )
                raise MediaNotFoundError(
                    f"No playable Apple Music tracks found for '{cleaned_query}'.",
                    details={"query": cleaned_query, "source": "apple_music"},
                )

            if source == "favorite":
                fav_title = self._play_favorite_on_coordinator(coord, cleaned_query, mode=mode)
                if fav_title:
                    msg = f"{verb} favorite '{fav_title}' in {resolved_room}."
                    return ActionResult(
                        ok=True,
                        action="play_media",
                        room=resolved_room,
                        message=msg,
                        state=self._build_room_state(resolved_room, speaker),
                        details={"source": "favorite", "mode": mode, "title": fav_title},
                    )
                available = self.get_favorite_titles()
                raise MediaNotFoundError(
                    f"Sonos favorite matching '{cleaned_query}' not found. Available favorites: {available}",
                    details={"query": cleaned_query, "available_favorites": available},
                )

            # source == "auto": try favorites first, then Apple Music catalog
            try:
                fav_title = self._play_favorite_on_coordinator(coord, cleaned_query, mode=mode)
                if fav_title:
                    msg = f"{verb} favorite '{fav_title}' in {resolved_room}."
                    return ActionResult(
                        ok=True,
                        action="play_media",
                        room=resolved_room,
                        message=msg,
                        state=self._build_room_state(resolved_room, speaker),
                        details={"source": "favorite", "mode": mode, "title": fav_title},
                    )
            except Exception as fav_exc:
                logger.warning("Favorite playback failed for '%s' (%s); falling back to Apple Music.", cleaned_query, fav_exc)

            am = self._play_apple_music_on_coordinator(coord, cleaned_query, mode=mode)
            if am:
                msg = f"{verb} '{am['title']}' by {am['artist']} on Apple Music in {resolved_room}."
                return ActionResult(
                    ok=True,
                    action="play_media",
                    room=resolved_room,
                    message=msg,
                    state=self._build_room_state(resolved_room, speaker),
                    details={"source": "apple_music", "mode": mode, **am},
                )

            raise MediaNotFoundError(
                f"Could not find '{cleaned_query}' in Sonos Favorites or Apple Music.",
                details={"query": cleaned_query, "source": "auto"},
            )

        return self._run_with_recovery(room, _op)

    def _is_quiet_hours(self) -> bool:
        start = self.settings.quiet_hours_start
        end = self.settings.quiet_hours_end
        if start is None or end is None:
            return False
        hour = self._now_fn().hour
        if start == end:
            return True
        if start < end:
            return start <= hour < end
        # Overnight wrap (e.g., 21 -> 7)
        return hour >= start or hour < end

    def _apply_volume_guardrails(self, room: str, desired: int, *, force: bool = False) -> Tuple[int, bool, Optional[str]]:
        clamped = max(0, min(100, int(desired)))
        if force:
            return clamped, False, None

        caps: List[Tuple[int, str]] = []
        room_cap = self.settings.max_volume_by_room.get(room)
        if room_cap is not None:
            caps.append((room_cap, f"room safety cap ({room_cap}) for {room}"))
        if self._is_quiet_hours():
            caps.append((self.settings.quiet_hours_max_volume, f"quiet hours cap ({self.settings.quiet_hours_max_volume})"))

        if not caps:
            return clamped, False, None

        effective_cap, reason = min(caps, key=lambda c: c[0])
        if clamped > effective_cap:
            return effective_cap, True, reason
        return clamped, False, None

    def set_volume(
        self,
        room: Optional[str] = None,
        *,
        level: Optional[int] = None,
        delta: Optional[int] = None,
        mute: Optional[bool] = None,
        apply_to_group: bool = False,
        force: bool = False,
    ) -> VolumeResult:
        """Set absolute volume, adjust relative delta, or toggle mute with optional safety caps."""
        if level is None and delta is None and mute is None:
            raise SonosServiceError(
                "Provide at least one of 'level', 'delta', or 'mute'.",
                code="invalid_parameters",
            )
        if level is not None and delta is not None:
            raise SonosServiceError(
                "Provide either 'level' or 'delta', not both.",
                code="invalid_parameters",
            )

        def _op(resolved_room: str, speaker: Any) -> VolumeResult:
            target = speaker.group if (apply_to_group and getattr(speaker, "group", None) is not None) else speaker
            prev_vol = max(0, min(100, int(target.volume)))
            new_vol = prev_vol
            capped = False
            cap_reason: Optional[str] = None

            if level is not None or delta is not None:
                desired = int(level) if level is not None else (prev_vol + int(delta))  # type: ignore[arg-type]
                new_vol, capped, cap_reason = self._apply_volume_guardrails(resolved_room, desired, force=force)
                target.volume = new_vol

            final_mute: Optional[bool] = None
            if mute is not None:
                target.mute = bool(mute)
                final_mute = bool(mute)
            else:
                try:
                    final_mute = bool(speaker.mute)
                except Exception:
                    final_mute = None

            scope = f"{resolved_room} group" if apply_to_group else resolved_room
            parts: List[str] = []
            if level is not None or delta is not None:
                parts.append(f"volume set to {new_vol}")
                if capped and cap_reason:
                    parts.append(f"(capped by {cap_reason}; pass force=true to override)")
            if mute is not None:
                parts.append("muted" if mute else "unmuted")

            return VolumeResult(
                ok=True,
                action="set_volume",
                room=resolved_room,
                message=f"{scope}: {' '.join(parts)}.",
                volume=new_vol,
                previous_volume=prev_vol,
                muted=final_mute,
                capped=capped,
                cap_reason=cap_reason,
                applied_to_group=apply_to_group,
                state=self._build_room_state(resolved_room, speaker),
            )

        return self._run_with_recovery(room, _op)

    def group_rooms(self, coordinator_room: str, member_rooms: List[str]) -> GroupResult:
        """Join one or more member rooms into coordinator_room's active playback group."""
        if not member_rooms:
            raise SonosServiceError("Provide at least one room in member_rooms.", code="invalid_parameters")

        coord_room_name, target_speaker = self.registry.get_speaker(coordinator_room)
        target_coord = _get_coordinator(target_speaker)
        target_uid = getattr(target_coord, "uid", None)
        target_ip = getattr(target_coord, "ip_address", None)

        changed: List[str] = []
        skipped: List[str] = []

        for raw_member in member_rooms:
            member_name, member_speaker = self.registry.get_speaker(raw_member)
            member_coord = _get_coordinator(member_speaker)
            member_uid = getattr(member_speaker, "uid", None)
            member_ip = getattr(member_speaker, "ip_address", None)
            member_coord_uid = getattr(member_coord, "uid", None)
            member_coord_ip = getattr(member_coord, "ip_address", None)

            already_same = (
                member_name == coord_room_name
                or (target_uid and member_uid and target_uid == member_uid)
                or (target_ip and member_ip and target_ip == member_ip)
                or (target_uid and member_coord_uid and target_uid == member_coord_uid)
                or (target_ip and member_coord_ip and target_ip == member_coord_ip)
            )
            if already_same:
                skipped.append(member_name)
                continue

            try:
                member_speaker.join(target_coord)
                changed.append(member_name)
            except SoCoException as exc:
                raise SonosServiceError(
                    f"Failed to join '{member_name}' into '{coord_room_name}': {exc}",
                    code="group_error",
                ) from exc

        actual_coord = normalize_room_name(str(getattr(target_coord, "player_name", coord_room_name)))
        all_members = sorted(set([actual_coord] + changed + skipped))
        if changed:
            msg = f"Joined {', '.join(changed)} into {actual_coord}."
        else:
            msg = f"{', '.join(skipped)} already grouped with {actual_coord}."

        return GroupResult(
            ok=True,
            coordinator=actual_coord,
            members=all_members,
            changed=changed,
            skipped=skipped,
            message=msg,
        )

    def ungroup_rooms(self, rooms: List[str]) -> GroupResult:
        """Ungroup one or more rooms from their current Sonos groups."""
        if not rooms:
            raise SonosServiceError("Provide at least one room to ungroup.", code="invalid_parameters")

        changed: List[str] = []
        skipped: List[str] = []

        for raw_room in rooms:
            room_name, speaker = self.registry.get_speaker(raw_room)
            group = getattr(speaker, "group", None)
            members = [m for m in getattr(group, "members", []) if _is_visible_speaker(m)] if group else []
            if len(members) <= 1 and group is not None:
                skipped.append(room_name)
                continue
            try:
                speaker.unjoin()
                changed.append(room_name)
            except SoCoException as exc:
                raise SonosServiceError(
                    f"Failed to ungroup '{room_name}': {exc}",
                    code="ungroup_error",
                ) from exc

        msg = (
            f"Ungrouped {', '.join(changed)}."
            if changed
            else f"{', '.join(skipped)} already standalone."
        )
        return GroupResult(
            ok=True,
            coordinator=changed[0] if changed else (skipped[0] if skipped else self.settings.default_room),
            members=changed,
            changed=changed,
            skipped=skipped,
            message=msg,
        )

    def select_source(self, source: InputSource, room: Optional[str] = None) -> ActionResult:
        """Switch a speaker to its hardware TV (HDMI/Optical) or Line-In input."""
        normalized_source = str(source or "").strip().lower()

        def _op(resolved_room: str, speaker: Any) -> ActionResult:
            if normalized_source in ("tv", "hdmi", "spdif"):
                if not _is_soundbar(speaker):
                    soundbars = [
                        r for r, spk in sorted(self.registry.discover().items()) if _is_soundbar(spk)
                    ]
                    hint = f" Soundbar rooms with TV input: {soundbars}." if soundbars else ""
                    raise UnsupportedOperationError(
                        f"Room '{resolved_room}' does not have a TV audio input.{hint}",
                        details={"room": resolved_room, "soundbar_rooms": soundbars},
                    )
                speaker.switch_to_tv()
                msg = f"Switched {resolved_room} to TV audio."
            elif normalized_source in ("line_in", "line-in", "aux"):
                speaker.switch_to_line_in()
                msg = f"Switched {resolved_room} to Line-In audio."
            else:
                raise UnsupportedOperationError(f"Unsupported audio input source: '{source}'. Use 'tv' or 'line_in'.")

            return ActionResult(
                ok=True,
                action="select_source",
                room=resolved_room,
                message=msg,
                state=self._build_room_state(resolved_room, speaker),
                details={"source": normalized_source},
            )

        return self._run_with_recovery(room, _op)

    # ------------------------------------------------------------------
    # Backwards-Compatible Legacy Dispatcher (used by voice agent)
    # ------------------------------------------------------------------

    def execute_tool(self, tool: str, action: Dict[str, Any]) -> str:
        """Legacy dict-based dispatcher retained for backwards compatibility with voice actions."""
        room = action.get("room") or self.settings.default_room

        if tool == "get_status":
            st = self.get_room_state(room)
            if st.source_type == "tv":
                playing = "TV audio"
            elif st.track and st.track.title:
                artist = f" by {st.track.artist}" if st.track.artist else ""
                playing = f"{st.track.title}{artist}"
            else:
                playing = "nothing"
            group_note = f" (grouped with {', '.join(st.group_members)})" if len(st.group_members) > 1 else ""
            return f"Room {st.room} is {st.state}{group_note}. Volume is {st.volume}. Playing: {playing}."

        if tool in ("pause", "stop", "next", "previous"):
            return self.control_playback(tool, room=room).message  # type: ignore[arg-type]

        if tool == "set_volume":
            vol = _safe_int(action.get("volume"), 15)
            force = bool(action.get("force", False))
            return self.set_volume(room=room, level=vol, force=force).message

        if tool == "adjust_volume":
            delta = _safe_int(action.get("delta"), 10)
            force = bool(action.get("force", False))
            return self.set_volume(room=room, delta=delta, force=force).message

        if tool == "mute":
            return self.set_volume(room=room, mute=bool(action.get("mute", True))).message

        if tool == "ungroup":
            return self.ungroup_rooms([str(room)]).message

        if tool == "group":
            target_room = action.get("target_room") or self.settings.default_room
            return self.group_rooms(str(target_room), [str(room)]).message

        if tool == "select_source":
            source = str(action.get("source") or "tv").strip().lower()
            if source in ("hdmi", "spdif"):
                source = "tv"
            elif source in ("line-in", "aux"):
                source = "line_in"
            return self.select_source(source, room=room).message  # type: ignore[arg-type]

        if tool == "play":
            source_raw = str(action.get("source") or "auto").strip().lower()
            query = str(action.get("query") or "").strip()
            mode_raw = str(action.get("mode") or "replace").strip().lower()
            mode: QueueInsertMode = mode_raw if mode_raw in ("replace", "play_next", "append") else "replace"  # type: ignore[assignment]
            if source_raw == "resume" or not query:
                return self.control_playback("resume", room=room).message
            source: MediaSource = source_raw if source_raw in ("auto", "favorite", "apple_music") else "auto"  # type: ignore[assignment]
            return self.play_media(query=query, room=room, source=source, mode=mode).message

        raise UnsupportedOperationError(f"Unknown tool: {tool}")


# ------------------------------------------------------------------
# Default Singleton & Module-Level Facade
# ------------------------------------------------------------------

_default_service: Optional[SonosService] = None
_default_lock = threading.Lock()


def get_service() -> SonosService:
    """Return the process-wide default SonosService singleton."""
    global _default_service
    with _default_lock:
        if _default_service is None:
            _default_service = SonosService(load_settings())
        return _default_service


def reset_service(service: Optional[SonosService] = None) -> None:
    """Replace or clear the default service singleton (primarily for testing)."""
    global _default_service
    with _default_lock:
        _default_service = service


# Module-level compatibility symbols
_initial_settings = load_settings()
DEFAULT_ROOM = _initial_settings.default_room
SPEAKER_IPS = dict(_initial_settings.speaker_ips)


def discover_speakers(force_refresh: bool = False) -> Dict[str, Any]:
    return get_service().registry.discover(force_refresh=force_refresh)


def get_speaker(room: str) -> Any:
    _, speaker = get_service().registry.get_speaker(room)
    return speaker


def get_favorite_titles(force_refresh: bool = False) -> List[str]:
    return get_service().get_favorite_titles(force_refresh=force_refresh)


def execute_tool(tool: str, action: Dict[str, Any]) -> str:
    return get_service().execute_tool(tool, action)
