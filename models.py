"""Typed Pydantic models shared across SonosService, FastMCP tools, and FastAPI."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

TransportState = Literal["PLAYING", "PAUSED_PLAYBACK", "STOPPED", "TRANSITIONING", "UNKNOWN"]
SourceKind = Literal["music", "radio", "tv", "line_in", "idle"]
PlaybackAction = Literal["resume", "pause", "stop", "next", "previous"]
MediaSource = Literal["auto", "favorite", "apple_music"]
QueueInsertMode = Literal["replace", "play_next", "append"]
InputSource = Literal["tv", "line_in"]
RampType = Literal["SLEEP_TIMER_RAMP_TYPE", "ALARM_RAMP_TYPE", "AUTOPLAY_RAMP_TYPE"]
HandoffMode = Literal["transfer", "extend"]
QueueAction = Literal["clear", "remove", "jump", "shuffle", "repeat", "crossfade"]


class TrackInfo(BaseModel):
    """Currently playing track or stream metadata."""

    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    position: Optional[str] = None
    duration: Optional[str] = None
    uri: Optional[str] = None


class TvAudioSettings(BaseModel):
    """Home-theater soundbar settings (Sonos Arc / Beam / Ray)."""

    night_mode: Optional[bool] = None
    speech_enhancement: Optional[bool] = None
    sub_gain: Optional[int] = Field(default=None, ge=-15, le=15)
    surround_level: Optional[int] = Field(default=None, ge=-15, le=15)


class RoomState(BaseModel):
    """Complete structured status of a single visible Sonos room."""

    room: str = Field(description="Canonical snake_case room identifier.")
    display_name: str = Field(description="Sonos player_name as displayed in the Sonos app.")
    uid: Optional[str] = Field(default=None, description="Sonos RINCON unique device identifier.")
    ip: Optional[str] = Field(default=None, description="Current LAN IPv4 address.")
    model_name: Optional[str] = Field(default=None, description="Sonos hardware model (e.g., Sonos Arc).")
    online: bool = Field(default=True, description="Whether the speaker responded to status queries.")
    state: TransportState = Field(default="UNKNOWN", description="Active transport state of the group coordinator.")
    volume: int = Field(default=0, ge=0, le=100, description="Current speaker volume (0-100).")
    muted: bool = Field(default=False, description="Whether the speaker is muted.")
    coordinator: str = Field(description="Canonical room ID of the group coordinator.")
    is_coordinator: bool = Field(default=True, description="True if this room leads its group.")
    group_members: List[str] = Field(default_factory=list, description="All rooms currently grouped together.")
    source_type: SourceKind = Field(default="idle", description="Detected active audio source kind.")
    track: Optional[TrackInfo] = Field(default=None, description="Current track/station info when playing.")
    is_soundbar: bool = Field(default=False, description="True if speaker supports TV HDMI/optical input & Arc EQ.")
    sleep_timer_seconds: Optional[int] = Field(default=None, description="Remaining sleep timer seconds, if set.")
    tv_audio: Optional[TvAudioSettings] = Field(default=None, description="Soundbar Night Mode / Speech Enhancement state.")


class GroupTopology(BaseModel):
    """Summary of an active Sonos playback group."""

    coordinator: str
    members: List[str]
    state: TransportState = "UNKNOWN"


class HouseholdState(BaseModel):
    """Snapshot of all discovered Sonos rooms and active groups."""

    default_room: str
    rooms: List[RoomState]
    groups: List[GroupTopology] = Field(default_factory=list)


class FavoriteItem(BaseModel):
    """A saved Sonos Favorite entry."""

    title: str
    description: Optional[str] = None
    uri: Optional[str] = None


class FavoritesList(BaseModel):
    """Collection of Sonos Favorites."""

    count: int
    favorites: List[FavoriteItem]


class ActionResult(BaseModel):
    """Standard structured response for playback and source mutations."""

    ok: bool = True
    action: str
    room: str
    message: str
    state: Optional[RoomState] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class VolumeResult(ActionResult):
    """Structured response for volume and mute operations."""

    volume: int = Field(ge=0, le=100)
    previous_volume: Optional[int] = Field(default=None, ge=0, le=100)
    muted: Optional[bool] = None
    capped: bool = False
    cap_reason: Optional[str] = None
    applied_to_group: bool = False


class GroupResult(BaseModel):
    """Structured response for group / ungroup operations."""

    ok: bool = True
    coordinator: str
    members: List[str]
    changed: List[str] = Field(default_factory=list)
    skipped: List[str] = Field(default_factory=list)
    message: str


class QueueItem(BaseModel):
    """Single item in a Sonos playback queue."""

    position: int = Field(ge=0, description="0-based queue index.")
    title: str
    artist: Optional[str] = None
    album: Optional[str] = None


class QueueState(BaseModel):
    """Structured view of a room coordinator's playback queue."""

    room: str
    coordinator: str
    total_items: int
    play_mode: Optional[str] = None
    cross_fade: Optional[bool] = None
    items: List[QueueItem] = Field(default_factory=list)


class HandoffResult(BaseModel):
    """Structured result for transferring or extending playback across rooms."""

    ok: bool = True
    mode: HandoffMode
    from_room: str
    to_rooms: List[str]
    message: str


class SleepTimerResult(BaseModel):
    """Structured result for sleep timer operations."""

    ok: bool = True
    room: str
    coordinator: str
    remaining_seconds: Optional[int] = None
    message: str


class AnnouncementResult(BaseModel):
    """Structured result for snapshot-and-restore announcements."""

    ok: bool = True
    rooms: List[str]
    restored: List[str] = Field(default_factory=list)
    failed_restore: List[str] = Field(default_factory=list)
    message: str


class SceneStep(BaseModel):
    """A single declarative step inside a household scene."""

    action: str
    params: Dict[str, Any] = Field(default_factory=dict)


class SceneDefinition(BaseModel):
    """Declarative household scene configuration."""

    name: str
    description: str
    steps: List[SceneStep] = Field(default_factory=list)


class ScenesList(BaseModel):
    """Collection of available household scenes."""

    count: int
    scenes: List[SceneDefinition]


class SceneResult(BaseModel):
    """Result of applying a multi-step household scene."""

    ok: bool = True
    scene: str
    steps_executed: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    message: str
