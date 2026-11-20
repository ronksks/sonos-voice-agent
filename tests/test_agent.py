"""Integration and unit tests for the unified async FastAPI voice agent + mounted Streamable HTTP /mcp."""

import json
import re
import typing
from fastapi.testclient import TestClient
import httpx
import pytest

from agent import VoiceToolName, _build_system_prompt, create_app, fallback_keyword_parse
from config import load_settings
from sonos_service import SonosService, SpeakerRegistry, reset_service
from tests.conftest import FakeShareLinkPlugin, build_fake_household


def _build_mock_ollama_client(chat_response_json=None, *, fail_chat=False, tags_models=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/tags"):
            models = [{"name": m} for m in (tags_models if tags_models is not None else ["qwen2.5:3b"])]
            return httpx.Response(200, json={"models": models})
        if request.url.path.endswith("/api/chat"):
            if fail_chat:
                raise httpx.ConnectError("Ollama offline")
            content = json.dumps(chat_response_json or {"actions": [{"tool": "get_status", "room": "living_room"}]})
            return httpx.Response(200, json={"message": {"content": content}})
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _make_test_service(env=None):
    household = build_fake_household()
    settings = load_settings(env or {})
    registry = SpeakerRegistry(
        settings,
        discover_fn=lambda timeout=2.0: {d for d in household.values() if d.is_visible},
    )
    svc = SonosService(settings, registry=registry, sharelink_factory=FakeShareLinkPlugin)
    return settings, svc, household


def test_keyword_fallback_avoids_substring_false_positives():
    settings = load_settings({})
    rooms = ["bedroom", "kids_room", "living_room"]

    # "stop" inside "Don't Stop Believing" must NOT trigger pause!
    a1 = fallback_keyword_parse("play Don't Stop Believing in living room", settings, rooms)
    assert a1[0].tool == "play"
    assert a1[0].room == "living_room"
    assert "Stop Believing" in (a1[0].query or "")

    # "tv" inside "MTV" or "Stevie Wonder" must NOT switch to TV HDMI!
    a2 = fallback_keyword_parse("play Stevie Wonder", settings, rooms)
    assert a2[0].tool == "play"
    assert "Stevie Wonder" in (a2[0].query or "")

    a3 = fallback_keyword_parse("play MTV hits", settings, rooms)
    assert a3[0].tool == "play"

    # Explicit TV command still works
    a4 = fallback_keyword_parse("switch living room to TV", settings, rooms)
    assert a4[0].tool == "select_source"
    assert a4[0].source == "tv"

    # Multi-room grouping detects BOTH rooms in order
    a5 = fallback_keyword_parse("group kids room with living room", settings, rooms)
    assert a5[0].tool == "group"
    assert a5[0].room == "kids_room"
    assert a5[0].target_room == "living_room"

    # Hebrew commands work cleanly
    a6 = fallback_keyword_parse("תעצור את הניגון בסלון", settings, rooms)
    assert a6[0].tool == "pause"
    assert a6[0].room == "living_room"

    a7 = fallback_keyword_parse("טיימר שינה 45 דקות בחדר ילדים", settings, rooms)
    assert a7[0].tool == "set_sleep_timer"
    assert a7[0].room == "kids_room"
    assert a7[0].minutes == 45


def test_command_endpoint_ollama_multi_action_and_fallback():
    settings, svc, household = _make_test_service()
    ollama = _build_mock_ollama_client(
        {
            "actions": [
                {"tool": "pause", "room": "living_room"},
                {"tool": "set_sleep_timer", "room": "kids_room", "minutes": 30},
            ]
        }
    )
    app = create_app(settings, service=svc, ollama_client=ollama, enable_warmup=False)
    try:
        with TestClient(app) as client:
            resp = client.post("/command", json={"command": "pause living room and set kids timer 30m"})
            assert resp.status_code == 200
            body = resp.json()
            assert body["ok"] is True
            assert body["actions_executed"] == 2
            assert household["living_room"].transport_state == "PAUSED_PLAYBACK"
            assert household["kids_room"].get_sleep_timer() == 1800
    finally:
        reset_service(None)


def test_command_endpoint_falls_back_when_ollama_unreachable():
    settings, svc, household = _make_test_service()
    ollama = _build_mock_ollama_client(fail_chat=True)
    app = create_app(settings, service=svc, ollama_client=ollama, enable_warmup=False)
    try:
        with TestClient(app) as client:
            resp = client.post("/command", json={"command": "תעצור בסלון"})
            body = resp.json()
            assert resp.status_code == 200
            assert body["ok"] is True
            assert body["model_used"] == "keyword-fallback"
            assert household["living_room"].transport_state == "PAUSED_PLAYBACK"
    finally:
        reset_service(None)


def test_health_endpoint_reports_cache_and_mcp_status():
    settings, svc, _ = _make_test_service()
    ollama = _build_mock_ollama_client(tags_models=["qwen2.5:3b"])
    app = create_app(settings, service=svc, ollama_client=ollama, enable_warmup=False)
    try:
        with TestClient(app) as client:
            resp = client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "healthy"
            assert data["mcp_endpoint"] == "/mcp"
            assert data["discovered_speakers"] == ["bedroom", "kids_room", "living_room"]
            assert data["cache"]["cached_rooms"] == ["bedroom", "kids_room", "living_room"]
    finally:
        reset_service(None)


def test_security_middleware_enforces_api_key_and_origin():
    settings, svc, _ = _make_test_service(env={"SONOS_API_KEY": "top-secret-key"})
    ollama = _build_mock_ollama_client()
    app = create_app(settings, service=svc, ollama_client=ollama, enable_warmup=False)
    try:
        with TestClient(app) as client:
            # /health stays open for container probes
            assert client.get("/health").status_code == 200

            # /command rejects missing/wrong key
            assert client.post("/command", json={"command": "status"}).status_code == 401
            assert client.post(
                "/command",
                json={"command": "status"},
                headers={"Authorization": "Bearer wrong"},
            ).status_code == 401

            # /command accepts Bearer and X-API-Key
            ok_resp = client.post(
                "/command",
                json={"command": "status"},
                headers={"Authorization": "Bearer top-secret-key"},
            )
            assert ok_resp.status_code == 200
            assert ok_resp.json()["ok"] is True

            # Untrusted cross-site browser Origin is rejected (CSRF / DNS rebinding guard)
            bad_origin = client.post(
                "/command",
                json={"command": "status"},
                headers={"X-API-Key": "top-secret-key", "Origin": "https://evil.example.com"},
            )
            assert bad_origin.status_code == 403
    finally:
        reset_service(None)


def test_mounted_streamable_http_mcp_endpoint_works_end_to_end():
    settings, svc, _ = _make_test_service()
    ollama = _build_mock_ollama_client()
    app = create_app(settings, service=svc, ollama_client=ollama, enable_warmup=False)
    try:
        with TestClient(app) as client:
            headers = {"Accept": "application/json, text/event-stream"}
            init_resp = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "pytest", "version": "1.0"},
                    },
                },
            )
            assert init_resp.status_code == 200
            init_body = init_resp.json()
            assert "tools" in init_body["result"]["capabilities"]
            assert "resources" in init_body["result"]["capabilities"]
            assert "prompts" in init_body["result"]["capabilities"]

            # Invoke get_household_state over HTTP /mcp!
            call_resp = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "get_household_state", "arguments": {}},
                },
            )
            assert call_resp.status_code == 200
            call_body = call_resp.json()
            assert call_body["result"]["isError"] is False
            rooms = call_body["result"]["structuredContent"]["rooms"]
            assert [r["room"] for r in rooms] == ["bedroom", "kids_room", "living_room"]
    finally:
        reset_service(None)


def test_system_prompt_lists_all_tools_and_avoids_pink_noise_primacy():
    settings = load_settings({})
    rooms = ["bedroom", "kids_room", "living_room"]
    prompt = _build_system_prompt(settings, rooms, ["Pink Noise", "GLGLZ"], ["movie_night"])

    for tool_name in typing.get_args(VoiceToolName):
        assert tool_name in prompt

    assert prompt.index("get_status") < prompt.index("play")

    without_context = re.sub(r"<context_data>.*?</context_data>", "", prompt, flags=re.DOTALL)
    assert "Pink Noise" not in without_context


def test_keyword_fallback_handles_ios_curly_apostrophe_and_status_variants():
    settings = load_settings({})
    rooms = ["bedroom", "kids_room", "living_room"]

    cases = [
        ("What\u2019s playing in the living room?", "living_room"),
        ("What is playing in kids room?", "kids_room"),
        ("what\u2019s on in living room", "living_room"),
        ("מה המצב בסלון?", "living_room"),
    ]
    for utterance, expected_room in cases:
        actions = fallback_keyword_parse(utterance, settings, rooms)
        assert actions[0].tool == "get_status"
        assert actions[0].room == expected_room


def test_command_endpoint_fast_path_for_read_only_status_skips_ollama():
    settings, svc, household = _make_test_service()
    ollama_offline = _build_mock_ollama_client(fail_chat=True)
    app = create_app(settings, service=svc, ollama_client=ollama_offline, enable_warmup=False)
    try:
        with TestClient(app) as client:
            resp = client.post("/command", json={"command": "What\u2019s playing in the living room?"})
            assert resp.status_code == 200
            body = resp.json()
            assert body["ok"] is True
            assert body["model_used"] == "fast-path"
            assert "living_room" in body["reply"]
            assert "Bohemian Rhapsody" in body["reply"]
            assert household["living_room"].queue == []
            assert household["living_room"].calls == []
    finally:
        reset_service(None)

    # Compound utterance must NOT be hijacked by fast-path
    settings2, svc2, household2 = _make_test_service()
    ollama_multi = _build_mock_ollama_client(
        {
            "actions": [
                {"tool": "get_status", "room": "living_room"},
                {"tool": "pause", "room": "living_room"},
            ]
        }
    )
    app2 = create_app(settings2, service=svc2, ollama_client=ollama_multi, enable_warmup=False)
    try:
        with TestClient(app2) as client2:
            resp2 = client2.post(
                "/command",
                json={"command": "what's playing in living room and pause living room"},
            )
            assert resp2.status_code == 200
            body2 = resp2.json()
            assert body2["ok"] is True
            assert body2["model_used"] == settings2.ollama_model
            assert body2["actions_executed"] == 2
            assert household2["living_room"].transport_state == "PAUSED_PLAYBACK"
    finally:
        reset_service(None)


def test_command_endpoint_sends_configured_ollama_keep_alive():
    captured_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/chat"):
            captured_payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={"message": {"content": json.dumps({"actions": [{"tool": "pause", "room": "living_room"}]})}},
            )
        return httpx.Response(404)

    settings, svc, _ = _make_test_service(env={"OLLAMA_KEEP_ALIVE": "10m"})
    ollama = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(settings, service=svc, ollama_client=ollama, enable_warmup=False)
    try:
        with TestClient(app) as client:
            resp = client.post("/command", json={"command": "pause living room"})
            assert resp.status_code == 200
            assert resp.json()["ok"] is True
        assert len(captured_payloads) == 1
        assert captured_payloads[0]["keep_alive"] == "10m"
    finally:
        reset_service(None)

    captured_override = []

    def handler_override(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/chat"):
            captured_override.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={"message": {"content": json.dumps({"actions": [{"tool": "pause", "room": "living_room"}]})}},
            )
        return httpx.Response(404)

    settings_override, svc_override, _ = _make_test_service(env={"OLLAMA_KEEP_ALIVE": "5m"})
    ollama_override = httpx.AsyncClient(transport=httpx.MockTransport(handler_override))
    app_override = create_app(settings_override, service=svc_override, ollama_client=ollama_override, enable_warmup=False)
    try:
        with TestClient(app_override) as client_override:
            resp_override = client_override.post("/command", json={"command": "pause living room"})
            assert resp_override.status_code == 200
            assert resp_override.json()["ok"] is True
        assert len(captured_override) == 1
        assert captured_override[0]["keep_alive"] == "5m"
    finally:
        reset_service(None)
