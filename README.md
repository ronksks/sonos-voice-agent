# Sonos MCP & Voice Agent

Unified **Model Context Protocol (MCP v2)** server and **bilingual (English/Hebrew) Voice Agent** for Sonos home audio systems—powered by FastAPI, MCP Python SDK (`MCPServer`), Ollama structured outputs, and SoCo.

## Architecture

```text
  iOS Shortcuts / HTTP                MCP Clients (Streamable HTTP)        Local MCP Clients (stdio)
  POST /command  GET /health          POST /mcp                            python server.py
              │                                    │                                  │
              └────────────► agent.py (FastAPI :8080, non-root Docker)                │
                             • Optional Bearer / X-API-Key auth                       │
                             • Origin & Host DNS-rebinding guard                      │
                             • Async Ollama JSON-Schema router + EN/HE fallback       │
                                                   │                                  │
                                                   ▼                                  ▼
                                             server.py (MCPServer v2 — 18 tools, 4 resources, 4 prompts)
                                                                   │
                                                   ┌───────────────┴───────────────┐
                                                   ▼                               ▼
                                           sonos_service.py                  workflows.py
                                           • SpeakerRegistry (TTL cache)     • Arc TV Night/Dialog EQ
                                           • visible_zones satellite filter  • Audio/TV Handoff
                                           • Coordinator-aware routing       • Queue curation & Sleep Timer
                                           • IP-drift auto-recovery          • Snapshot announcements
                                           • Volume guardrails & Favorites   • Declarative scenes.json
```

## Key Features

- **Resilient Discovery & Recovery**: Parallel static seed probing (`SPEAKER_IPS` in JSON or `room:ip`), unicast `visible_zones` expansion (works across Docker bridge NAT and filters out invisible bonded Era 100 surrounds/Sub), TTL caching, `soco.config.REQUEST_TIMEOUT`, and automatic one-shot topology refresh on DHCP IP drift.
- **Coordinator-Aware Control**: Transport and queue operations automatically route to `speaker.group.coordinator`, while volume, mute, joining, and hardware inputs (`tv` / `line_in`) target the physical speaker. Handles UPnP 701/711/800 edge cases cleanly and never silently falls back to the wrong room.
- **Rich MCP v2 Surface** (see [docs/TOOLS.md](docs/TOOLS.md)):
  - **18 structured, annotated tools** with Pydantic output schemas and `ToolAnnotations` (`read_only_hint`, `destructive_hint`, `idempotent_hint`).
  - **4 live JSON resources**: `sonos://household/topology`, `sonos://favorites`, `sonos://scenes`, `sonos://room/{room}/now-playing`.
  - **4 reusable workflow prompts**: `bedtime_kids`, `movie_night`, `morning_radio`, `party_mode`.
- **Creative Sonos Workflows**:
  - **Sonos Arc Home-Theater Controls**: Night Mode, Speech Enhancement, Sub gain, Surround level.
  - **Audio / TV Handoff**: Transfer music ("follow me") or extend music/TV across rooms.
  - **Volume Safety Guardrails & Hardware Ramping**: Per-room max caps, overnight quiet-hours caps (with `force=true` override), and speaker-side `ramp_to_volume`.
  - **Non-Destructive Queue Curation**: `play_next`, `append`, `manage_queue`, and parallel multi-track `curate_queue` via Apple Music `ShareLinkPlugin`.
  - **Sleep Timers, Snapshot Announcements & Scenes**: Native sleep timers, `Snapshot`-restore audio chimes, and declarative `scenes.json` with `exclude_rooms` protection (e.g., protect a sleeping baby's room!).
- **Bilingual Voice Agent (`/command`)**: Async FastAPI + `httpx.AsyncClient` routing English and Hebrew utterances through Ollama using strict JSON Schema (`format`) with multi-action support and a word-boundary keyword fallback.
- **Security Hardening**: Non-root Docker image, optional `SONOS_API_KEY` (`Authorization: Bearer` / `X-API-Key`) middleware, `Origin` & `Host` validation against CSRF/DNS rebinding, and strict `http(s)` URI validation on announcements.

## Quickstart (Docker Compose)

1. Copy `.env.example` to `.env` and configure at least one Sonos speaker IP (one reachable seed IP expands the entire household via unicast `visible_zones`):
   ```bash
   cp .env.example .env
   ```
2. Build and start the unified container (serves `/command`, `/health`, and Streamable HTTP `/mcp` on port `8080`):
   ```bash
   docker compose up -d --build
   ```
3. Verify system health:
   ```bash
   curl http://localhost:8080/health
   ```

## Connecting Clients

### 1. MCP Client via Streamable HTTP (Docker / Remote)
Point any MCP Streamable HTTP client to `http://<HOST_LAN_IP>:8080/mcp`:
```json
{
  "mcpServers": {
    "sonos": {
      "url": "http://localhost:8080/mcp",
      "headers": {
        "Authorization": "Bearer <your-SONOS_API_KEY-if-set>"
      }
    }
  }
}
```

### 2. MCP Client via Local stdio
Install dependencies into a virtual environment and run `server.py` directly (logs are routed strictly to `stderr`):
```json
{
  "mcpServers": {
    "sonos": {
      "command": "python",
      "args": ["C:/Users/ronya/sonos_mcp/server.py"],
      "env": {
        "SPEAKER_IPS": "{\"living_room\": \"192.168.1.100\"}",
        "DEFAULT_ROOM": "living_room"
      }
    }
  }
}
```

### 3. iOS Shortcuts Voice Client
Configure an iOS Shortcut to send an HTTP `POST` request to `http://<HOST_LAN_IP>:8080/command`:
- **Headers**: `Content-Type: application/json` (and `Authorization: Bearer <SONOS_API_KEY>` if auth is enabled)
- **Body**: `{"command": "<Dictated Text>"}`
- **Action**: Speak the returned JSON `reply` field aloud.

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `SPEAKER_IPS` | *(empty)* | JSON object (`{"living_room": "192.168.1.100"}`) or comma-separated `room:ip` seed list. |
| `DEFAULT_ROOM` | `living_room` | Canonical room used when no room is specified. |
| `SOCO_REQUEST_TIMEOUT` | `5.0` | Per-request SoCo UPnP HTTP timeout in seconds (`soco.config.REQUEST_TIMEOUT`). |
| `DISCOVERY_TIMEOUT` | `2.0` | SSDP multicast fallback timeout in seconds. |
| `CACHE_TTL_SECONDS` | `180` | Speaker topology cache TTL in seconds. |
| `FAVORITES_CACHE_TTL_SECONDS` | `300` | Sonos Favorites cache TTL in seconds. |
| `APPLE_MUSIC_COUNTRY` | `IL` | ISO country code for iTunes Search API catalog queries. |
| `MAX_VOLUME_BY_ROOM` | `{}` | Optional JSON per-room volume ceiling, e.g. `{"kids_room": 40, "bedroom": 40}`. |
| `QUIET_HOURS_START` / `END` | *(unset)* | Optional 0–23 hours enabling quiet-hours volume cap (supports overnight wrap like `21` -> `7`). |
| `QUIET_HOURS_MAX_VOLUME` | `35` | Maximum volume during quiet hours unless `force=true`. |
| `OLLAMA_URL` | `http://host.docker.internal:11434/api/chat` | Local Ollama chat API endpoint. |
| `OLLAMA_MODEL` | `qwen2.5:3b` | Ollama model used for EN/HE voice command translation. |
| `OLLAMA_TIMEOUT` | `30.0` | Read timeout for Ollama chat calls (connect timeout defaults to `3.0s`). |
| `SONOS_API_KEY` | *(unset)* | When set, protects `/command` and `/mcp` with Bearer / `X-API-Key` authentication. |
| `ALLOWED_ORIGINS` | *(localhost only)* | Comma-separated allowed browser `Origin` headers (protects against LAN CSRF/DNS rebinding). |
| `ALLOWED_HOSTS` | *(any)* | Optional comma-separated hostnames enforced against the `Host` header. |
| `SCENES_PATH` | `scenes.json` | Path to declarative household scenes file. |

## Development & Testing

All automated tests use pure in-memory `FakeSoCo` / `httpx.MockTransport` doubles and never send packets or audio to physical speakers:

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt
.venv/Scripts/pytest -v
```
