"""Unit and MCP tests for Phase 3 creative workflows, scenes, and prompts."""

import json
from unittest.mock import MagicMock

import pytest

from config import load_settings
import server
from sonos_service import (
    SonosService,
    SonosServiceError,
    SpeakerRegistry,
    UnsupportedOperationError,
    reset_service,
)
from tests.conftest import FakeShareLinkPlugin, build_fake_household
from workflows import SonosWorkflows


class FakeSnapshot:
    instances = []

    def __init__(self, device):
        self.device = device
        self.saved = False
        self.restored = False
        self.saved_volume = None
        FakeSnapshot.instances.append(self)

    def snapshot(self):
        self.saved = True
        self.saved_volume = self.device.volume

    def restore(self, fade=False):
        self.restored = True
        if self.saved_volume is not None:
            self.device.volume = self.saved_volume


def _make_workflows(env=None, http_get=None):
    household = build_fake_household()
    settings = load_settings(env or {})
    registry = SpeakerRegistry(
        settings,
        discover_fn=lambda timeout=2.0: {d for d in household.values() if d.is_visible},
    )
    svc = SonosService(
        settings,
        registry=registry,
        http_get=http_get,
        sharelink_factory=FakeShareLinkPlugin,
    )
    wf = SonosWorkflows(svc, snapshot_factory=FakeSnapshot, sleep_fn=lambda _: None)
    return svc, wf, household


def test_configure_tv_audio_arc_and_non_soundbar_rejection():
    _, wf, household = _make_workflows()
    arc = household["living_room"]

    res = wf.configure_tv_audio(
        room="living_room",
        night_mode=True,
        speech_enhancement=True,
        sub_gain=-4,
        surround_level=3,
    )
    assert res.ok is True
    assert arc.night_mode is True
    assert arc.dialog_mode is True
    assert arc.sub_gain == -4
    assert arc.surround_volume_tv == 3

    with pytest.raises(UnsupportedOperationError) as exc_info:
        wf.configure_tv_audio(room="kids_room", night_mode=True)
    assert "living_room" in str(exc_info.value)


def test_handoff_audio_transfer_extend_and_tv_guard():
    svc, wf, household = _make_workflows()
    living = household["living_room"]
    kids = household["kids_room"]

    # Music transfer: joins kids_room then unjoins living_room
    res = wf.handoff_audio("living_room", ["kids_room"], mode="transfer")
    assert res.ok is True
    assert res.mode == "transfer"
    assert "join:Living Room" in kids.calls
    assert "unjoin" in living.calls

    # Switch Living Room Arc to TV: transfer is blocked, extend succeeds!
    svc.select_source("tv", room="living_room")
    with pytest.raises(UnsupportedOperationError) as exc_info:
        wf.handoff_audio("living_room", ["kids_room"], mode="transfer")
    assert "extend" in str(exc_info.value).lower()

    ext = wf.handoff_audio("living_room", ["kids_room"], mode="extend")
    assert ext.ok is True
    assert ext.mode == "extend"
    assert kids.group.coordinator is living


def test_fade_volume_uses_ramp_and_respects_room_cap():
    _, wf, household = _make_workflows(env={"MAX_VOLUME_BY_ROOM": '{"kids_room": 35}'})
    kids = household["kids_room"]

    res = wf.fade_volume(60, room="kids_room", ramp_type="SLEEP_TIMER_RAMP_TYPE")
    assert res.volume == 35
    assert res.capped is True
    assert kids.ramps[-1] == {"volume": 35, "ramp_type": "SLEEP_TIMER_RAMP_TYPE"}


def test_queue_inspection_management_and_curation():
    def fake_itunes(url, params=None, timeout=5.0):
        term = (params or {}).get("term", "")
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        if "missing" in term.lower():
            resp.json.return_value = {"results": []}
        else:
            slug = term.lower().replace(" ", "_")
            resp.json.return_value = {
                "results": [
                    {
                        "trackName": f"Song {term}",
                        "artistName": "Artist",
                        "trackViewUrl": f"https://music.apple.com/track/{slug}",
                    }
                ]
            }
        return resp

    _, wf, household = _make_workflows(http_get=fake_itunes)
    living = household["living_room"]

    curated = wf.curate_queue(["Song A", "Missing Track", "Song B"], room="living_room", mode="replace")
    assert curated.ok is True
    assert len(curated.details["added_tracks"]) == 2
    assert curated.details["failed_queries"] == ["Missing Track"]
    assert len(living.queue) == 2

    q = wf.get_queue(room="living_room")
    assert q.total_items == 2
    assert len(q.items) == 2

    # Shuffle + Repeat composition preserves both flags
    wf.manage_queue("shuffle", room="living_room", enabled=True)
    assert living.play_mode == "SHUFFLE_NOREPEAT"
    wf.manage_queue("repeat", room="living_room", repeat_mode="all")
    assert living.play_mode == "SHUFFLE"
    wf.manage_queue("crossfade", room="living_room", enabled=True)
    assert living.cross_fade is True

    # Jump and remove
    wf.manage_queue("jump", room="living_room", index=1)
    assert "play_from_queue:1" in living.calls
    wf.manage_queue("remove", room="living_room", index=0)
    assert len(living.queue) == 1
    wf.manage_queue("clear", room="living_room")
    assert len(living.queue) == 0


def test_sleep_timer_set_and_cancel():
    _, wf, household = _make_workflows()
    kids = household["kids_room"]

    res = wf.set_sleep_timer(room="kids_room", minutes=45)
    assert res.remaining_seconds == 2700
    assert kids.get_sleep_timer() == 2700

    cancel = wf.set_sleep_timer(room="kids_room", minutes=0)
    assert cancel.remaining_seconds is None
    assert kids.get_sleep_timer() is None


def test_announce_audio_snapshots_plays_restores_and_validates_uri():
    FakeSnapshot.instances.clear()
    _, wf, household = _make_workflows()
    living = household["living_room"]
    original_vol = living.volume

    # Reject non-http(s) URI schemes
    with pytest.raises(SonosServiceError) as exc_info:
        wf.announce_audio(["living_room"], "file:///etc/passwd")
    assert exc_info.value.code == "invalid_uri"

    res = wf.announce_audio(
        ["living_room"],
        "https://cdn.example.com/chime.mp3",
        volume=40,
        duration_seconds=1.0,
    )
    assert res.ok is True
    assert res.restored == ["living_room"]
    assert living.played_uris[-1]["uri"] == "https://cdn.example.com/chime.mp3"
    # Volume restored cleanly by snapshot
    assert living.volume == original_vol
    assert FakeSnapshot.instances[-1].saved is True
    assert FakeSnapshot.instances[-1].restored is True


def test_apply_scene_respects_exclude_rooms_protecting_bedroom():
    _, wf, household = _make_workflows()
    bedroom = household["bedroom"]
    living = household["living_room"]
    kids = household["kids_room"]

    scenes = wf.list_scenes()
    names = {s.name for s in scenes.scenes}
    assert {"bedtime_kids", "movie_night", "morning_radio", "party_mode", "all_off"}.issubset(names)

    # Apply party_mode while excluding bedroom -> bedroom must NOT be grouped or modified!
    res = wf.apply_scene("party_mode", exclude_rooms=["bedroom"])
    assert res.ok is True
    assert kids.group.coordinator is living
    assert bedroom.group.coordinator is bedroom
    assert bedroom.calls == []
    assert bedroom.volume == 15

    # Apply bedtime_kids
    bedtime = wf.apply_scene("bedtime_kids")
    assert bedtime.ok is True
    assert kids.get_sleep_timer() == 3600
    assert living.night_mode is True
    assert bedroom.calls == []


async def test_mcp_workflow_tools_scene_resource_and_prompts():
    svc, _, _ = _make_workflows()
    reset_service(svc)
    try:
        tools = {t.name for t in await server.mcp.list_tools()}
        for expected in (
            "configure_tv_audio",
            "handoff_audio",
            "fade_volume",
            "get_queue",
            "manage_queue",
            "curate_queue",
            "set_sleep_timer",
            "announce_audio",
            "list_scenes",
            "apply_scene",
        ):
            assert expected in tools

        # Resource sonos://scenes
        scene_contents = list(await server.mcp.read_resource("sonos://scenes"))
        scenes_json = json.loads(scene_contents[0].content)
        assert scenes_json["count"] >= 5

        # Prompts
        prompts = {p.name for p in await server.mcp.list_prompts()}
        assert {"bedtime_kids", "movie_night", "morning_radio", "party_mode"}.issubset(prompts)

        prompt_res = await server.mcp.get_prompt("bedtime_kids", {"duration_minutes": "45", "volume": "20"})
        text = prompt_res.messages[0].content.text
        assert "45-minute" in text and "20" in text

        # Call tool via MCP
        timer_res = await server.mcp.call_tool("set_sleep_timer", {"room": "living_room", "minutes": 30})
        assert timer_res.structured_content["remaining_seconds"] == 1800
    finally:
        reset_service(None)
