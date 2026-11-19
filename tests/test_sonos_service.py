"""Unit tests for SonosService playback, coordinator routing, guardrails, and IP recovery."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest
import requests

from config import load_settings
from sonos_service import (
    MediaNotFoundError,
    SonosService,
    SpeakerRegistry,
    UnsupportedOperationError,
)
from tests.conftest import FakeShareLinkPlugin, FakeSoCo, build_fake_household


def _make_service(household=None, env=None, http_get=None, now_dt=None):
    household = household or build_fake_household()
    settings = load_settings(env or {})
    registry = SpeakerRegistry(
        settings,
        discover_fn=lambda timeout=2.0: {
            d for d in household.values() if d.is_visible
        },
    )
    service = SonosService(
        settings,
        registry=registry,
        http_get=http_get,
        sharelink_factory=FakeShareLinkPlugin,
        now_fn=(lambda: now_dt) if now_dt else datetime.now,
    )
    return service, household


def test_grouped_member_routes_transport_to_coordinator():
    service, household = _make_service()
    living = household["living_room"]
    kids = household["kids_room"]

    # Group kids_room into living_room (living_room is coordinator)
    res = service.group_rooms("living_room", ["kids_room"])
    assert res.changed == ["kids_room"]
    assert kids.group.coordinator is living

    # Calling pause / next / resume on grouped slave 'kids_room' must route to coordinator (no SoCoSlaveException!)
    pause_res = service.control_playback("pause", room="kids_room")
    assert pause_res.ok is True
    assert living.transport_state == "PAUSED_PLAYBACK"

    play_res = service.control_playback("resume", room="kids_room")
    assert play_res.ok is True
    assert living.transport_state == "PLAYING"

    next_res = service.control_playback("next", room="kids_room")
    assert "next" in living.calls
    assert next_res.room == "kids_room"

    # Volume must still adjust the individual room unless apply_to_group=True
    service.set_volume(room="kids_room", level=30)
    assert kids.volume == 30
    assert living.volume == 25

    # Group volume updates both visible rooms
    gvol = service.set_volume(room="kids_room", level=22, apply_to_group=True)
    assert gvol.applied_to_group is True
    assert kids.volume == 22
    assert living.volume == 22


def test_pause_is_idempotent_and_handles_upnp_701_stream_stop():
    service, household = _make_service()
    kids = household["kids_room"]
    assert kids.transport_state == "STOPPED"

    # Already stopped -> idempotent success without error
    res = service.control_playback("pause", room="kids_room")
    assert res.ok is True
    assert "already" in res.message.lower()

    # Playing live stream that rejects pause with UPnP 701 -> falls back cleanly to stop()
    living = household["living_room"]
    living.transport_state = "PLAYING"
    living.fail_pause_701 = True
    res2 = service.control_playback("pause", room="living_room")
    assert res2.ok is True
    assert living.transport_state == "STOPPED"
    assert "stop" in living.calls


def test_next_on_live_radio_raises_clear_unsupported_error():
    service, household = _make_service()
    living = household["living_room"]
    living.fail_next_701 = True

    with pytest.raises(UnsupportedOperationError) as exc_info:
        service.control_playback("next", room="living_room")
    assert "live radio" in str(exc_info.value).lower()


def test_play_favorite_unwraps_didl_reference_and_falls_back_to_play_uri():
    service, household = _make_service()
    living = household["living_room"]

    # Queueable favorite via alias ('white noise' -> 'Pink Noise')
    res_noise = service.play_media("white noise", room="living_room", source="favorite")
    assert res_noise.ok is True
    assert "Pink Noise" in res_noise.message
    assert living.queue[0]["title"] == "Pink Noise"

    # Live radio favorite ('גלגלצ' -> 'GLGLZ') rejects add_to_queue (UPnP 800) -> uses play_uri with DIDL meta
    res_radio = service.play_media("גלגלצ", room="living_room", source="favorite")
    assert res_radio.ok is True
    assert "GLGLZ" in res_radio.message
    assert living.played_uris[-1]["uri"] == "x-sonosapi-stream:s25874?sid=254"
    assert "<DIDL-Lite>" in living.played_uris[-1]["meta"]


def test_play_apple_music_uses_itunes_search_and_supports_non_destructive_modes():
    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {
        "results": [
            {"trackName": "Let It Be", "artistName": "The Beatles", "trackViewUrl": "https://music.apple.com/track/101"},
            {"trackName": "Hey Jude", "artistName": "The Beatles", "trackViewUrl": "https://music.apple.com/track/102"},
        ]
    }
    service, household = _make_service(http_get=lambda *a, **kw: fake_response)
    living = household["living_room"]

    # Replace mode clears queue & plays track 0
    res = service.play_media("Beatles", room="living_room", source="auto", mode="replace")
    assert res.ok is True
    assert "Let It Be" in res.message
    assert len(living.queue) == 2

    # Append mode does NOT clear existing queue
    res_append = service.play_media("Beatles", room="living_room", source="apple_music", mode="append")
    assert res_append.ok is True
    assert len(living.queue) == 4

    # Not found raises MediaNotFoundError (never silently lies!)
    empty_resp = MagicMock()
    empty_resp.raise_for_status.return_value = None
    empty_resp.json.return_value = {"results": []}
    service_empty, _ = _make_service(http_get=lambda *a, **kw: empty_resp)
    with pytest.raises(MediaNotFoundError):
        service_empty.play_media("Nonexistent XYZ Track 999", room="living_room", source="auto")


def test_volume_guardrails_room_cap_quiet_hours_and_force_override():
    service, household = _make_service(
        env={
            "MAX_VOLUME_BY_ROOM": '{"kids_room": 40}',
            "QUIET_HOURS_START": "21",
            "QUIET_HOURS_END": "7",
            "QUIET_HOURS_MAX_VOLUME": "30",
        },
        now_dt=datetime(2026, 11, 19, 22, 30),  # Inside quiet hours
    )
    kids = household["kids_room"]
    living = household["living_room"]

    # Quiet hours cap (30) is stricter than kids_room cap (40)
    res = service.set_volume(room="kids_room", level=75)
    assert res.volume == 30
    assert res.capped is True
    assert "quiet hours" in (res.cap_reason or "")
    assert kids.volume == 30

    # force=True bypasses guardrails
    res_forced = service.set_volume(room="living_room", level=55, force=True)
    assert res_forced.volume == 55
    assert res_forced.capped is False
    assert living.volume == 55


def test_select_source_checks_soundbar_capability():
    service, household = _make_service()
    living = household["living_room"]

    res = service.select_source("tv", room="living_room")
    assert res.ok is True
    assert living.is_playing_tv is True
    assert res.state is not None and res.state.source_type == "tv"

    # Kids Room Era 100 has no TV input -> clear error naming soundbar rooms
    with pytest.raises(UnsupportedOperationError) as exc_info:
        service.select_source("tv", room="kids_room")
    assert "living_room" in str(exc_info.value)


def test_ip_drift_auto_recovery_refreshes_topology_and_retries():
    old_living = FakeSoCo("192.168.1.100", "Living Room", uid="RINCON_LIVING_ARC", is_soundbar=True)
    new_living = FakeSoCo("192.168.1.150", "Living Room", uid="RINCON_LIVING_ARC", is_soundbar=True)
    old_living.next_error = requests.ConnectionError("DHCP IP changed")

    discover_round = [0]

    def fake_discover(timeout=2.0):
        discover_round[0] += 1
        return {old_living} if discover_round[0] == 1 else {new_living}

    settings = load_settings({})
    registry = SpeakerRegistry(settings, discover_fn=fake_discover)
    service = SonosService(settings, registry=registry)

    res = service.control_playback("resume", room="living_room")
    assert res.ok is True
    assert discover_round[0] == 2
    assert new_living.transport_state == "PLAYING"


def test_get_household_state_returns_all_rooms_and_groups():
    service, household = _make_service()
    service.group_rooms("living_room", ["kids_room"])

    state = service.get_household_state()
    assert state.default_room == "living_room"
    assert [r.room for r in state.rooms] == ["bedroom", "kids_room", "living_room"]
    living_state = next(r for r in state.rooms if r.room == "living_room")
    assert living_state.is_soundbar is True
    assert living_state.model_name == "Sonos Arc"
    assert living_state.group_members == ["kids_room", "living_room"]
    assert len(state.groups) == 2  # (living_room+kids_room) and (bedroom)


def test_legacy_execute_tool_handles_null_volume_safely():
    service, household = _make_service()
    msg = service.execute_tool("set_volume", {"room": "living_room", "volume": None})
    assert "15" in msg
    assert household["living_room"].volume == 15
