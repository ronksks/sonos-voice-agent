"""Unit tests for SpeakerRegistry discovery, caching, satellite filtering, and strict lookup."""

import pytest
import requests

from config import load_settings
from sonos_service import NoSpeakersError, RoomNotFoundError, SpeakerRegistry
from tests.conftest import FakeSoCo, build_fake_household


def test_discovery_uses_visible_zones_and_skips_bonded_satellites():
    household = build_fake_household()
    by_ip = {d.ip_address: d for d in household.values()}
    ssdp_calls = 0

    def fake_ssdp(timeout=2.0):
        nonlocal ssdp_calls
        ssdp_calls += 1
        return set(household.values())

    # Only seed living_room IP; unicast visible_zones should discover kids_room & bedroom too
    settings = load_settings({"SPEAKER_IPS": '{"living_room": "192.168.1.100"}'})
    registry = SpeakerRegistry(settings, soco_factory=lambda ip: by_ip[ip], discover_fn=fake_ssdp)

    devices = registry.discover()
    assert sorted(devices.keys()) == ["bedroom", "kids_room", "living_room"]
    # Must bind living_room to the Sonos Arc, NOT the invisible bonded surround!
    assert devices["living_room"].uid == "RINCON_LIVING_ARC"
    assert devices["living_room"].is_soundbar is True
    # Should NOT invoke slow SSDP multicast when static seed + visible_zones succeeded
    assert ssdp_calls == 0


def test_discovery_filters_invisible_satellites_from_ssdp():
    household = build_fake_household()
    # Put invisible surround first to verify collision resolution picks the visible Arc
    ssdp_order = [household["living_surround"], household["living_room"], household["kids_room"]]
    settings = load_settings({})
    registry = SpeakerRegistry(settings, discover_fn=lambda timeout=2.0: ssdp_order)

    devices = registry.discover()
    assert sorted(devices.keys()) == ["kids_room", "living_room"]
    assert devices["living_room"].uid == "RINCON_LIVING_ARC"


def test_discovery_ttl_cache_avoids_repeated_scans():
    household = build_fake_household()
    now = [100.0]
    ssdp_calls = 0

    def fake_ssdp(timeout=2.0):
        nonlocal ssdp_calls
        ssdp_calls += 1
        return {household["living_room"], household["kids_room"]}

    settings = load_settings({"CACHE_TTL_SECONDS": "60"})
    registry = SpeakerRegistry(settings, discover_fn=fake_ssdp, clock=lambda: now[0])

    registry.discover()
    registry.discover()
    assert ssdp_calls == 1

    # Advance past TTL -> refreshes
    now[0] += 65.0
    registry.discover()
    assert ssdp_calls == 2


def test_get_speaker_raises_room_not_found_with_suggestion_instead_of_silent_fallback():
    household = build_fake_household()
    settings = load_settings({})
    registry = SpeakerRegistry(
        settings,
        discover_fn=lambda timeout=2.0: {household["living_room"], household["kids_room"], household["bedroom"]},
    )

    with pytest.raises(RoomNotFoundError) as exc_info:
        registry.get_speaker("kids_rom")

    err = exc_info.value
    assert err.code == "room_not_found"
    assert err.suggestion == "kids_room"
    assert err.available_rooms == ["bedroom", "kids_room", "living_room"]


def test_get_speaker_resolves_hebrew_and_english_aliases():
    household = build_fake_household()
    settings = load_settings({})
    registry = SpeakerRegistry(
        settings,
        discover_fn=lambda timeout=2.0: {household["living_room"], household["kids_room"]},
    )

    room, spk = registry.get_speaker("בסלון")
    assert room == "living_room"
    assert spk.uid == "RINCON_LIVING_ARC"

    room2, spk2 = registry.get_speaker("חדר הילדים")
    assert room2 == "kids_room"
    assert spk2.uid == "RINCON_KIDS"


def test_get_speaker_raises_no_speakers_when_network_empty():
    settings = load_settings({"SPEAKER_IPS": '{"living_room": "192.168.1.250"}'})

    def failing_factory(ip: str):
        raise requests.ConnectionError("unreachable")

    registry = SpeakerRegistry(settings, soco_factory=failing_factory, discover_fn=lambda timeout=2.0: set())

    with pytest.raises(NoSpeakersError):
        registry.get_speaker("living_room")
