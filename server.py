"""Model Context Protocol (MCP) server for Sonos Home Audio control.

Supports:
- Local stdio transport (`python server.py`) for IDE/desktop MCP clients.
- Streamable HTTP transport (`mcp.streamable_http_app(...)`) mounted inside FastAPI (`agent.py`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Annotated, Any, Callable, List, Literal, Optional, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from models import (
    ActionResult,
    AnnouncementResult,
    FavoritesList,
    GroupResult,
    HandoffMode,
    HandoffResult,
    HouseholdState,
    InputSource,
    MediaSource,
    PlaybackAction,
    QueueAction,
    QueueInsertMode,
    QueueState,
    RampType,
    SceneResult,
    ScenesList,
    SleepTimerResult,
    VolumeResult,
)
from sonos_service import SonosServiceError, get_service
from workflows import get_workflows

T = TypeVar("T")

TOOL_TIMEOUT_SECONDS = float(os.getenv("MCP_TOOL_TIMEOUT", "20.0"))

logger = logging.getLogger("sonos_mcp")


def _configure_stderr_logging() -> None:
    """Route all logs strictly to stderr so stdio JSON-RPC streams are never corrupted."""
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        root.addHandler(handler)
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    root.setLevel(level)


async def _run_service_call(fn: Callable[..., T], *args: Any, timeout: float = TOOL_TIMEOUT_SECONDS, **kwargs: Any) -> T:
    """Execute blocking SonosService calls off the event loop with hard timeout and ToolError translation."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=timeout)
    except SonosServiceError as exc:
        raise ToolError(f"[{exc.code}] {exc}") from exc
    except asyncio.TimeoutError as exc:
        raise ToolError(f"[timeout] Sonos operation timed out after {timeout:.0f}s.") from exc
    except Exception as exc:
        logger.exception("Unexpected error in Sonos MCP tool")
        raise ToolError(f"[internal_error] Unexpected Sonos error: {exc}") from exc


async def _run_resource_call(fn: Callable[..., T], *args: Any, timeout: float = TOOL_TIMEOUT_SECONDS, **kwargs: Any) -> T:
    """Execute blocking SonosService calls for MCP resources, translating errors to ResourceError."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=timeout)
    except SonosServiceError as exc:
        raise ResourceError(f"[{exc.code}] {exc}") from exc
    except asyncio.TimeoutError as exc:
        raise ResourceError(f"[timeout] Sonos resource timed out after {timeout:.0f}s.") from exc
    except Exception as exc:
        logger.exception("Unexpected error reading Sonos MCP resource")
        raise ResourceError(f"[internal_error] Unexpected Sonos error: {exc}") from exc


mcp = MCPServer(
    name="sonos",
    title="Sonos Home Audio Controller",
    description="Control Sonos speakers, playback, multi-room groups, TV soundbars, queues, and household routines.",
    version="3.0.0",
    instructions=(
        "Call get_household_state() without arguments to inspect every visible room, group topology, "
        "volume, mute, track, and soundbar capability in a single round-trip. "
        "Room arguments accept canonical IDs (e.g., living_room, kids_room, bedroom) or English/Hebrew aliases. "
        "Before playing a saved Sonos favorite, inspect list_favorites() or read resource sonos://favorites "
        "so you pass an accurate title."
    ),
)


# ------------------------------------------------------------------
# Core MCP Tools
# ------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Household State",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
async def get_household_state(
    room: Annotated[
        Optional[str],
        Field(description="Optional room ID or alias; omit to inspect all visible rooms and active groups at once."),
    ] = None,
) -> HouseholdState:
    """Inspect playback state, volume, mute, current track/source, group membership, and soundbar status."""
    return await _run_service_call(get_service().get_household_state, room=room)


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Sonos Favorites",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
async def list_favorites(
    force_refresh: Annotated[
        bool,
        Field(description="Bypass the favorites cache and query Sonos directly."),
    ] = False,
) -> FavoritesList:
    """List saved Sonos Favorites (radio stations, playlists, white/pink noise) available for play_media."""
    return await _run_service_call(get_service().list_favorites, force_refresh=force_refresh)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Control Playback",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
async def control_playback(
    action: Annotated[
        PlaybackAction,
        Field(description="Transport command: 'resume', 'pause', 'stop', 'next', or 'previous'."),
    ],
    room: Annotated[
        Optional[str],
        Field(description="Target room ID or alias (defaults to DEFAULT_ROOM). Automatically routes to group coordinator."),
    ] = None,
) -> ActionResult:
    """Control transport playback on a room (automatically routed to its group coordinator)."""
    return await _run_service_call(get_service().control_playback, action, room=room)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Play or Queue Media",
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    )
)
async def play_media(
    query: Annotated[
        str,
        Field(min_length=1, description="Favorite title, radio station, artist, or song name to play or queue."),
    ],
    room: Annotated[
        Optional[str],
        Field(description="Target room ID or alias (defaults to DEFAULT_ROOM)."),
    ] = None,
    source: Annotated[
        MediaSource,
        Field(description="'auto' checks Sonos Favorites first then Apple Music; 'favorite' or 'apple_music' targets one."),
    ] = "auto",
    mode: Annotated[
        QueueInsertMode,
        Field(description="'replace' clears queue & plays immediately; 'play_next' inserts after current track; 'append' adds to end."),
    ] = "replace",
) -> ActionResult:
    """Play or queue a Sonos Favorite or Apple Music track/artist on a room's group coordinator."""
    return await _run_service_call(
        get_service().play_media,
        query=query,
        room=room,
        source=source,
        mode=mode,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Set or Adjust Volume",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    )
)
async def set_volume(
    room: Annotated[
        Optional[str],
        Field(description="Target room ID or alias (defaults to DEFAULT_ROOM)."),
    ] = None,
    level: Annotated[
        Optional[int],
        Field(ge=0, le=100, description="Absolute volume 0-100. Mutually exclusive with `delta`."),
    ] = None,
    delta: Annotated[
        Optional[int],
        Field(ge=-50, le=50, description="Relative volume step (e.g., +10 louder, -10 quieter). Mutually exclusive with `level`."),
    ] = None,
    mute: Annotated[
        Optional[bool],
        Field(description="Set mute state explicitly (true to mute, false to unmute)."),
    ] = None,
    apply_to_group: Annotated[
        bool,
        Field(description="When true, adjusts the entire Sonos group proportionally instead of only this room."),
    ] = False,
    force: Annotated[
        bool,
        Field(description="Set true to override configured per-room or quiet-hours maximum volume safety caps."),
    ] = False,
) -> VolumeResult:
    """Set absolute volume, adjust relative delta, or toggle mute. Provide at least one of level, delta, or mute."""
    return await _run_service_call(
        get_service().set_volume,
        room=room,
        level=level,
        delta=delta,
        mute=mute,
        apply_to_group=apply_to_group,
        force=force,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Group Sonos Rooms",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
async def group_rooms(
    coordinator_room: Annotated[
        str,
        Field(min_length=1, description="Room whose active audio/TV playback will be preserved and broadcast."),
    ],
    member_rooms: Annotated[
        List[str],
        Field(min_length=1, description="One or more rooms to join into coordinator_room's session."),
    ],
) -> GroupResult:
    """Join one or more member_rooms into coordinator_room's playback session (preserving coordinator_room's audio)."""
    return await _run_service_call(get_service().group_rooms, coordinator_room, member_rooms)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Ungroup Sonos Rooms",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
async def ungroup_rooms(
    rooms: Annotated[
        List[str],
        Field(min_length=1, description="One or more room IDs or aliases to detach from their current groups."),
    ],
) -> GroupResult:
    """Detach one or more rooms from their current Sonos playback groups."""
    return await _run_service_call(get_service().ungroup_rooms, rooms)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Select Hardware Input",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
async def select_source(
    source: Annotated[
        InputSource,
        Field(description="Hardware audio input: 'tv' (HDMI/optical on soundbars like Arc) or 'line_in'."),
    ],
    room: Annotated[
        Optional[str],
        Field(description="Target room ID or alias (defaults to DEFAULT_ROOM)."),
    ] = None,
) -> ActionResult:
    """Switch a capable Sonos speaker to its TV (HDMI/optical) or Line-In hardware input."""
    return await _run_service_call(get_service().select_source, source, room=room)


# ------------------------------------------------------------------
# Advanced & Creative Workflow Tools
# ------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Configure Soundbar TV Audio",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
async def configure_tv_audio(
    room: Annotated[
        Optional[str],
        Field(description="Soundbar room ID or alias (defaults to DEFAULT_ROOM, e.g., living_room Arc)."),
    ] = None,
    night_mode: Annotated[
        Optional[bool],
        Field(description="Enable/disable Night Sound (compresses loud explosions while preserving quiet details)."),
    ] = None,
    speech_enhancement: Annotated[
        Optional[bool],
        Field(description="Enable/disable Speech Enhancement (boosts vocal dialogue frequencies)."),
    ] = None,
    sub_gain: Annotated[
        Optional[int],
        Field(ge=-15, le=15, description="Bonded Sonos Sub gain level (-15 to +15)."),
    ] = None,
    surround_level: Annotated[
        Optional[int],
        Field(ge=-15, le=15, description="TV surround satellite level (-15 to +15)."),
    ] = None,
) -> ActionResult:
    """Configure Sonos Arc/Beam/Ray home-theater Night Mode, Speech Enhancement, Sub gain, or surround level."""
    return await _run_service_call(
        get_workflows().configure_tv_audio,
        room=room,
        night_mode=night_mode,
        speech_enhancement=speech_enhancement,
        sub_gain=sub_gain,
        surround_level=surround_level,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Handoff or Extend Audio",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
async def handoff_audio(
    from_room: Annotated[
        str,
        Field(min_length=1, description="Source room currently playing music or TV."),
    ],
    to_rooms: Annotated[
        List[str],
        Field(min_length=1, description="Destination room(s) to receive the audio."),
    ],
    mode: Annotated[
        HandoffMode,
        Field(description="'transfer' moves music seamlessly and detaches from_room; 'extend' mirrors music or TV across both."),
    ] = "transfer",
) -> HandoffResult:
    """Transfer music seamlessly to another room ('follow me') or extend music/TV audio across rooms."""
    return await _run_service_call(get_workflows().handoff_audio, from_room, to_rooms, mode=mode)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Fade Volume Smoothly",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
async def fade_volume(
    target_volume: Annotated[
        int,
        Field(ge=0, le=100, description="Destination volume (0-100) reached smoothly via native Sonos ramping."),
    ],
    room: Annotated[
        Optional[str],
        Field(description="Target room ID or alias (defaults to DEFAULT_ROOM)."),
    ] = None,
    ramp_type: Annotated[
        RampType,
        Field(description="Sonos ramp profile: SLEEP_TIMER_RAMP_TYPE, AUTOPLAY_RAMP_TYPE, or ALARM_RAMP_TYPE."),
    ] = "SLEEP_TIMER_RAMP_TYPE",
    force: Annotated[
        bool,
        Field(description="Override per-room or quiet-hours maximum volume safety caps."),
    ] = False,
) -> VolumeResult:
    """Smoothly fade a room's volume using native speaker hardware ramping (ideal for bedtime or wake-up)."""
    return await _run_service_call(
        get_workflows().fade_volume,
        target_volume,
        room=room,
        ramp_type=ramp_type,
        force=force,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Inspect Playback Queue",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
async def get_queue(
    room: Annotated[
        Optional[str],
        Field(description="Target room ID or alias (defaults to DEFAULT_ROOM)."),
    ] = None,
    start: Annotated[int, Field(ge=0, description="0-based starting queue offset.")] = 0,
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum queue items to return.")] = 25,
) -> QueueState:
    """Inspect upcoming tracks, play_mode (shuffle/repeat), and crossfade state in a room's playback queue."""
    return await _run_service_call(get_workflows().get_queue, room=room, start=start, limit=limit)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Manage Playback Queue",
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=False,
    )
)
async def manage_queue(
    action: Annotated[
        QueueAction,
        Field(description="Queue operation: 'clear', 'remove', 'jump', 'shuffle', 'repeat', or 'crossfade'."),
    ],
    room: Annotated[
        Optional[str],
        Field(description="Target room ID or alias (defaults to DEFAULT_ROOM)."),
    ] = None,
    index: Annotated[
        Optional[int],
        Field(ge=0, description="0-based queue index required for 'remove' or 'jump'."),
    ] = None,
    enabled: Annotated[
        Optional[bool],
        Field(description="Boolean toggle required for 'shuffle' or 'crossfade'."),
    ] = None,
    repeat_mode: Annotated[
        Optional[Literal["none", "all", "one"]],
        Field(description="Repeat setting required when action='repeat'."),
    ] = None,
) -> ActionResult:
    """Clear, reorder, jump within, or toggle shuffle/repeat/crossfade on a room coordinator's queue."""
    return await _run_service_call(
        get_workflows().manage_queue,
        action,
        room=room,
        index=index,
        enabled=enabled,
        repeat_mode=repeat_mode,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Curate Multi-Track Queue",
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    )
)
async def curate_queue(
    tracks: Annotated[
        List[str],
        Field(min_length=1, max_length=20, description="List of song/artist search queries (up to 20) to enqueue in order."),
    ],
    room: Annotated[
        Optional[str],
        Field(description="Target room ID or alias (defaults to DEFAULT_ROOM)."),
    ] = None,
    mode: Annotated[
        Literal["replace", "append"],
        Field(description="'replace' clears existing queue first; 'append' preserves existing tracks."),
    ] = "replace",
    start_playing: Annotated[
        bool,
        Field(description="Start playing immediately if replacing queue or currently stopped."),
    ] = True,
) -> ActionResult:
    """Resolve multiple Apple Music song queries in parallel and queue them as a cohesive playlist in one call."""
    return await _run_service_call(
        get_workflows().curate_queue,
        tracks,
        room=room,
        mode=mode,
        start_playing=start_playing,
        timeout=30.0,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Set Sleep Timer",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
async def set_sleep_timer(
    room: Annotated[
        Optional[str],
        Field(description="Target room ID or alias (defaults to DEFAULT_ROOM)."),
    ] = None,
    minutes: Annotated[
        Optional[int],
        Field(ge=0, le=720, description="Duration in minutes before auto-stop (pass 0 or null to cancel timer)."),
    ] = None,
) -> SleepTimerResult:
    """Set or cancel a native Sonos hardware sleep timer on a room's group coordinator."""
    return await _run_service_call(get_workflows().set_sleep_timer, room=room, minutes=minutes)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Play Snapshot-Restore Announcement",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    )
)
async def announce_audio(
    rooms: Annotated[
        List[str],
        Field(min_length=1, description="Rooms to play the chime/announcement on (explicitly omit sleeping rooms!)."),
    ],
    uri: Annotated[
        str,
        Field(min_length=8, description="Public or LAN http(s):// audio URL to play as an alert/announcement."),
    ],
    volume: Annotated[
        int,
        Field(ge=0, le=100, description="Temporary announcement volume (subject to safety caps unless force=true)."),
    ] = 30,
    duration_seconds: Annotated[
        float,
        Field(ge=0.5, le=60.0, description="Seconds to wait before restoring previous playback state."),
    ] = 5.0,
    force: Annotated[
        bool,
        Field(description="Override per-room or quiet-hours maximum volume safety caps."),
    ] = False,
) -> AnnouncementResult:
    """Snapshot rooms, play a short http(s) audio alert/chime, and restore exact previous playback/TV/volume."""
    return await _run_service_call(
        get_workflows().announce_audio,
        rooms,
        uri,
        volume=volume,
        duration_seconds=duration_seconds,
        force=force,
        timeout=max(TOOL_TIMEOUT_SECONDS, duration_seconds + 15.0),
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Household Scenes",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
async def list_scenes() -> ScenesList:
    """List declarative multi-step household scenes defined in scenes.json."""
    return await _run_service_call(get_workflows().list_scenes)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Apply Household Scene",
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=False,
    )
)
async def apply_scene(
    scene_name: Annotated[
        str,
        Field(min_length=1, description="Scene name from list_scenes() (e.g., bedtime_kids, movie_night, morning_radio, party_mode, all_off)."),
    ],
    exclude_rooms: Annotated[
        Optional[List[str]],
        Field(description="Optional rooms to protect/skip (e.g., ['bedroom'] when someone is sleeping)."),
    ] = None,
    force: Annotated[
        bool,
        Field(description="Override volume safety caps while applying scene."),
    ] = False,
) -> SceneResult:
    """Execute a multi-step household routine from scenes.json while safely skipping any excluded rooms."""
    return await _run_service_call(
        get_workflows().apply_scene,
        scene_name,
        exclude_rooms=exclude_rooms,
        force=force,
        timeout=30.0,
    )


# ------------------------------------------------------------------
# MCP Resources (Read-Only Context)
# ------------------------------------------------------------------


@mcp.resource(
    "sonos://household/topology",
    name="household_topology",
    title="Sonos Household Topology",
    description="Live JSON snapshot of all visible rooms, active groups, volumes, and soundbar capabilities.",
    mime_type="application/json",
)
async def household_topology_resource() -> str:
    state = await _run_resource_call(get_service().get_household_state)
    return state.model_dump_json(indent=2)


@mcp.resource(
    "sonos://favorites",
    name="sonos_favorites",
    title="Sonos Favorites",
    description="Cached list of saved Sonos Favorites and radio stations.",
    mime_type="application/json",
)
async def favorites_resource() -> str:
    favs = await _run_resource_call(get_service().list_favorites)
    return favs.model_dump_json(indent=2)


@mcp.resource(
    "sonos://scenes",
    name="household_scenes",
    title="Household Audio Scenes",
    description="Configured declarative household scenes from scenes.json.",
    mime_type="application/json",
)
async def scenes_resource() -> str:
    scenes = await _run_resource_call(get_workflows().list_scenes)
    return scenes.model_dump_json(indent=2)


@mcp.resource(
    "sonos://room/{room}/now-playing",
    name="room_now_playing",
    title="Room Now Playing",
    description="Live playback status, track metadata, volume, and group membership for a specific room.",
    mime_type="application/json",
)
async def room_now_playing_resource(room: str) -> str:
    state = await _run_resource_call(get_service().get_room_state, room)
    return state.model_dump_json(indent=2)


# ------------------------------------------------------------------
# MCP Prompts (Reusable Multi-Step Workflow Templates)
# ------------------------------------------------------------------


@mcp.prompt(
    name="bedtime_kids",
    title="Kids Bedtime Routine",
    description="Safely start Kids Room Pink Noise with sleep timer and enable Living Room TV Night Mode.",
)
def bedtime_kids_prompt(duration_minutes: int = 60, volume: int = 22) -> str:
    return (
        "Execute the kids bedtime routine safely:\n"
        "1. Inspect current playback via get_household_state() first so you do not disturb sleeping rooms.\n"
        "2. Ensure kids_room is ungrouped via ungroup_rooms(['kids_room']).\n"
        f"3. Play the 'Pink Noise' favorite in kids_room, fade volume smoothly to {volume}, "
        f"and set a {duration_minutes}-minute sleep timer.\n"
        "4. If living_room is a soundbar (Sonos Arc), enable night_mode=True and speech_enhancement=True via configure_tv_audio.\n"
        "5. Summarize the exact changes made."
    )


@mcp.prompt(
    name="movie_night",
    title="Living Room Movie Night",
    description="Switch Living Room Sonos Arc to TV audio with Speech Enhancement and optional Night Mode.",
)
def movie_night_prompt(enable_night_mode: bool = False) -> str:
    return (
        "Prepare Living Room for Movie Night:\n"
        "1. Call get_household_state() to verify group topology and check if other rooms are in use.\n"
        "2. Detach living_room if grouped via ungroup_rooms(['living_room']).\n"
        "3. Switch living_room to TV input via select_source(source='tv', room='living_room').\n"
        f"4. Call configure_tv_audio(room='living_room', speech_enhancement=True, night_mode={enable_night_mode}).\n"
        "5. Confirm volume and TV audio settings concisely."
    )


@mcp.prompt(
    name="morning_radio",
    title="Morning Radio Briefing",
    description="Gently fade in a saved radio station favorite in the morning.",
)
def morning_radio_prompt(station: str = "GLGLZ", room: str = "living_room", volume: int = 24) -> str:
    return (
        f"Start morning radio ({station}) in {room}:\n"
        f"1. Check get_household_state() and list_favorites() to confirm exact station title.\n"
        f"2. Ensure {room} is standalone so sleeping rooms stay silent.\n"
        f"3. Play favorite '{station}' in {room} and fade volume smoothly to {volume} with AUTOPLAY_RAMP_TYPE.\n"
        "4. Report what is now playing."
    )


@mcp.prompt(
    name="party_mode",
    title="Multi-Room Party Mode",
    description="Group active rooms, enable crossfade, and optionally curate a themed playlist.",
)
def party_mode_prompt(exclude_rooms: str = "", vibe: str = "") -> str:
    excl = f" Always exclude these rooms: {exclude_rooms}." if exclude_rooms.strip() else ""
    music = f" Then curate a 6-track queue matching '{vibe}' using curate_queue()." if vibe.strip() else ""
    return (
        "Set up multi-room Party Mode:\n"
        f"1. Inspect rooms via get_household_state().{excl}\n"
        "2. Group eligible rooms into living_room via group_rooms().\n"
        "3. Enable crossfade via manage_queue(action='crossfade', room='living_room', enabled=True).\n"
        f"4. Balance group volume to a comfortable level (around 28-32).{music}\n"
        "5. Summarize grouped rooms and active playlist."
    )


if __name__ == "__main__":
    _configure_stderr_logging()
    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "streamable-http":
        mcp.run(transport="streamable-http")
    elif transport == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
