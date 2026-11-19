"""Integration and unit tests for the unified async FastAPI voice agent + mounted Streamable HTTP /mcp."""

import json
from fastapi.testclient import TestClient
import httpx
import pytest

from agent import create_app, fallback_keyword_parse
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
