"""Unit tests for config.py parsing, normalization, and settings loading."""

from config import load_settings, normalize_room_name, parse_speaker_ips


def test_normalize_room_name():
    assert normalize_room_name("  Living Room ") == "living_room"
    assert normalize_room_name("Kids-Room") == "kids_room"
    assert normalize_room_name("Master   Bedroom") == "master_bedroom"


def test_parse_speaker_ips_json():
    raw = '{"Living Room": "192.168.1.100", "kids_room": "192.168.1.101", "bad": "not-an-ip"}'
    parsed = parse_speaker_ips(raw)
    assert parsed == {
        "living_room": "192.168.1.100",
        "kids_room": "192.168.1.101",
    }


def test_parse_speaker_ips_csv_colon_and_equals():
    assert parse_speaker_ips("living_room:192.168.1.10, bedroom=192.168.1.11") == {
        "living_room": "192.168.1.10",
        "bedroom": "192.168.1.11",
    }


def test_load_settings_defaults_and_overrides():
    settings = load_settings(
        {
            "DEFAULT_ROOM": "Kids Room",
            "SPEAKER_IPS": '{"living_room": "10.0.0.5"}',
            "MAX_VOLUME_BY_ROOM": '{"kids_room": 40, "bedroom": 120}',
            "QUIET_HOURS_START": "21",
            "QUIET_HOURS_END": "7",
            "QUIET_HOURS_MAX_VOLUME": "30",
            "SOCO_REQUEST_TIMEOUT": "3.5",
            "OLLAMA_MODEL": "qwen2.5:3b",
            "OLLAMA_KEEP_ALIVE": "10m",
            "SONOS_API_KEY": "secret-token",
        }
    )
    assert settings.default_room == "kids_room"
    assert settings.speaker_ips == {"living_room": "10.0.0.5"}
    assert settings.max_volume_by_room == {"kids_room": 40, "bedroom": 100}
    assert settings.quiet_hours_start == 21
    assert settings.quiet_hours_end == 7
    assert settings.quiet_hours_max_volume == 30
    assert settings.soco_request_timeout == 3.5
    assert settings.ollama_model == "qwen2.5:3b"
    assert settings.ollama_keep_alive == "10m"
    assert settings.sonos_api_key == "secret-token"
    assert load_settings({}).ollama_keep_alive == "10m"
    assert load_settings({"OLLAMA_KEEP_ALIVE": "-1"}).ollama_keep_alive == -1
