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
from typing import Annotated, Any, Callable, List, Optional, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from models import (
    ActionResult,
    FavoritesList,
    GroupResult,
    HouseholdState,
    InputSource,
    MediaSource,
    PlaybackAction,
    QueueInsertMode,
    VolumeResult,
)
from sonos_service import SonosServiceError, get_service

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
    "sonos://room/{room}/now-playing",
    name="room_now_playing",
    title="Room Now Playing",
    description="Live playback status, track metadata, volume, and group membership for a specific room.",
    mime_type="application/json",
)
async def room_now_playing_resource(room: str) -> str:
    state = await _run_resource_call(get_service().get_room_state, room)
    return state.model_dump_json(indent=2)


if __name__ == "__main__":
    _configure_stderr_logging()
    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "streamable-http":
        mcp.run(transport="streamable-http")
    elif transport == "sse":
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
