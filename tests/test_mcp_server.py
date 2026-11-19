"""Tests for the FastMCP / MCPServer v2 tool schemas, annotations, structured outputs, errors, and resources."""

import json
import pytest
from mcp.server.mcpserver.exceptions import ResourceError, ToolError

from config import load_settings
import server
from sonos_service import SonosService, SpeakerRegistry, reset_service
from tests.conftest import FakeShareLinkPlugin, build_fake_household


@pytest.fixture(autouse=True)
def injected_mock_service():
    household = build_fake_household()
    settings = load_settings({})
    registry = SpeakerRegistry(
        settings,
        discover_fn=lambda timeout=2.0: {d for d in household.values() if d.is_visible},
    )
    svc = SonosService(settings, registry=registry, sharelink_factory=FakeShareLinkPlugin)
    reset_service(svc)
    yield svc, household
    reset_service(None)


async def test_mcp_lists_expected_tools_with_annotations_and_schemas():
    tools = await server.mcp.list_tools()
    by_name = {t.name: t for t in tools}

    expected = {
        "get_household_state",
        "list_favorites",
        "control_playback",
        "play_media",
        "set_volume",
        "group_rooms",
        "ungroup_rooms",
        "select_source",
    }
    assert expected.issubset(by_name.keys())

    # Read-only annotations verified
    assert by_name["get_household_state"].annotations.read_only_hint is True
    assert by_name["list_favorites"].annotations.read_only_hint is True
    assert by_name["play_media"].annotations.destructive_hint is True

    # Output schemas are automatically generated from Pydantic return types
    assert by_name["get_household_state"].output_schema is not None
    assert by_name["set_volume"].output_schema is not None

    # Volume schema enforces 0..100 bounds & has no unsafe default
    vol_props = by_name["set_volume"].input_schema["properties"]
    level_schema = vol_props["level"]
    assert "default" not in level_schema or level_schema["default"] is None


async def test_mcp_get_household_state_structured_output():
    result = await server.mcp.call_tool("get_household_state", {})
    assert result.is_error is False
    assert result.structured_content is not None
    rooms = result.structured_content["rooms"]
    assert [r["room"] for r in rooms] == ["bedroom", "kids_room", "living_room"]


async def test_mcp_errors_propagate_as_tool_error_not_swallowed_strings():
    with pytest.raises(ToolError) as exc_info:
        await server.mcp.call_tool("control_playback", {"action": "pause", "room": "kitchen"})
    assert "room_not_found" in str(exc_info.value)
    assert "living_room" in str(exc_info.value)


async def test_mcp_volume_and_grouping_tools(injected_mock_service):
    _, household = injected_mock_service

    # Set volume with relative delta
    vol_res = await server.mcp.call_tool("set_volume", {"room": "living_room", "delta": 5})
    assert vol_res.structured_content["volume"] == 30
    assert household["living_room"].volume == 30

    # Group kids_room into living_room (multi-room list API)
    grp_res = await server.mcp.call_tool(
        "group_rooms",
        {"coordinator_room": "living_room", "member_rooms": ["kids_room"]},
    )
    assert grp_res.structured_content["coordinator"] == "living_room"
    assert grp_res.structured_content["changed"] == ["kids_room"]

    # Ungroup kids_room
    ungrp_res = await server.mcp.call_tool("ungroup_rooms", {"rooms": ["kids_room"]})
    assert ungrp_res.structured_content["changed"] == ["kids_room"]


async def test_mcp_play_media_favorite_and_select_source(injected_mock_service):
    _, household = injected_mock_service

    play_res = await server.mcp.call_tool(
        "play_media",
        {"query": "white noise", "room": "kids_room", "source": "favorite"},
    )
    assert play_res.structured_content["ok"] is True
    assert "Pink Noise" in play_res.structured_content["message"]

    tv_res = await server.mcp.call_tool("select_source", {"source": "tv", "room": "living_room"})
    assert tv_res.structured_content["ok"] is True
    assert household["living_room"].is_playing_tv is True


async def test_mcp_resources_return_valid_json():
    topo_contents = list(await server.mcp.read_resource("sonos://household/topology"))
    topo = json.loads(topo_contents[0].content)
    assert len(topo["rooms"]) == 3

    fav_contents = list(await server.mcp.read_resource("sonos://favorites"))
    favs = json.loads(fav_contents[0].content)
    assert favs["count"] == 3

    room_contents = list(await server.mcp.read_resource("sonos://room/living_room/now-playing"))
    room_data = json.loads(room_contents[0].content)
    assert room_data["room"] == "living_room"
    assert room_data["is_soundbar"] is True

    with pytest.raises(ResourceError):
        await server.mcp.read_resource("sonos://room/nonexistent_room/now-playing")
