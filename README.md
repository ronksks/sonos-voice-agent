# Sonos Voice Agent

Local natural language voice controller for Sonos speakers powered by FastAPI, Ollama (`qwen2.5:3b`), SoCo, and iOS Shortcuts.

## Features
- **Auto-Discovery**: Automatically discovers Sonos speakers on your local network via UPnP, with optional static `.env` configuration.
- **Bilingual Support**: Translates English and Hebrew voice commands into structured Sonos actions (`play`, `pause`, `set_volume`, `get_status`).
- **Resilient Fallback**: Falls back to keyword parsing if Ollama is offline or unresponsive.
- **Live Favorites & Apple Music**: Injects live Sonos Favorites into the LLM prompt and falls back to Apple Music catalog searches via `ShareLinkPlugin`.
- **Health Diagnostics**: Includes a `/health` endpoint to verify Ollama connectivity and speaker discovery.

## Setup
1. Copy `.env.example` to `.env`:
   `cp .env.example .env`
2. Run with Docker Compose:
   `docker compose up -d --build`
3. Check system health:
   `curl http://localhost:8080/health`
4. Configure an iOS Shortcut to send an HTTP POST request with JSON body `{"command": "<spoken text>"}` to `http://<HOST_LAN_IP>:8080/command` and speak the `reply` value.
