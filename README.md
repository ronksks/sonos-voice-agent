# Sonos Voice Agent

Local natural language voice controller for Sonos speakers powered by FastAPI, Ollama (`qwen2.5:3b`), SoCo, and iOS Shortcuts.

## Features
- Translates English and Hebrew voice commands into structured Sonos actions (`play`, `pause`, `set_volume`, `get_status`).
- Injects live Sonos Favorites into the LLM prompt and maps spoken station aliases.
- Falls back to Apple Music catalog searches via `ShareLinkPlugin` when favorites fail or when requesting artists and tracks.
- Keeps `qwen2.5:3b` resident in Ollama memory (`keep_alive: -1`) for fast LAN responses.

## Setup
1. Copy `.env.example` to `.env` and enter your local Sonos coordinator IP addresses and Ollama endpoint:
   `cp .env.example .env`
2. Build and run the Docker container:
   `docker build -t sonos-agent .`
   `docker run -d --name sonos-agent --restart unless-stopped --env-file .env --add-host=host.docker.internal:host-gateway -p 8080:8080 sonos-agent`
3. Configure an iOS Shortcut to send an HTTP POST request with JSON body `{"command": "<spoken text>"}` to `http://<HOST_LAN_IP>:8080/command` and speak the `reply` value.
