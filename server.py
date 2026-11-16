from mcp.server.fastmcp import FastMCP

from sonos_service import (
    DEFAULT_ROOM,
    SonosServiceError,
    discover_speakers,
    execute_tool,
)

mcp = FastMCP("Sonos")


@mcp.tool()
def list_rooms() -> str:
    """List discovered/configured Sonos rooms."""
    rooms = sorted(discover_speakers().keys())
    return ", ".join(rooms) if rooms else "No Sonos speakers discovered."


@mcp.tool()
def get_status(room: str = DEFAULT_ROOM) -> str:
    """Get playback state and volume for a Sonos room."""
    try:
        return execute_tool("get_status", {"room": room})
    except SonosServiceError as exc:
        return str(exc)


@mcp.tool()
def play(room: str = DEFAULT_ROOM, query: str = "", source: str = "auto") -> str:
    """Resume playback or play a Sonos favorite / Apple Music query."""
    try:
        return execute_tool("play", {"room": room, "query": query, "source": source})
    except SonosServiceError as exc:
        return str(exc)


@mcp.tool()
def pause(room: str = DEFAULT_ROOM) -> str:
    """Pause playback in a Sonos room."""
    try:
        return execute_tool("pause", {"room": room})
    except SonosServiceError as exc:
        return str(exc)


@mcp.tool()
def next_track(room: str = DEFAULT_ROOM) -> str:
    """Skip to the next track in a Sonos room."""
    try:
        return execute_tool("next", {"room": room})
    except SonosServiceError as exc:
        return str(exc)


@mcp.tool()
def previous_track(room: str = DEFAULT_ROOM) -> str:
    """Go back to the previous track in a Sonos room."""
    try:
        return execute_tool("previous", {"room": room})
    except SonosServiceError as exc:
        return str(exc)


@mcp.tool()
def set_volume(room: str = DEFAULT_ROOM, volume: int = 15) -> str:
    """Set volume between 0 and 100 for a Sonos room."""
    try:
        return execute_tool("set_volume", {"room": room, "volume": volume})
    except SonosServiceError as exc:
        return str(exc)


@mcp.tool()
def group_rooms(room: str, target_room: str = DEFAULT_ROOM) -> str:
    """Group a room with target_room's coordinator."""
    try:
        return execute_tool("group", {"room": room, "target_room": target_room})
    except SonosServiceError as exc:
        return str(exc)


@mcp.tool()
def ungroup_room(room: str = DEFAULT_ROOM) -> str:
    """Ungroup a room from its current Sonos group."""
    try:
        return execute_tool("ungroup", {"room": room})
    except SonosServiceError as exc:
        return str(exc)


if __name__ == "__main__":
    mcp.run()

