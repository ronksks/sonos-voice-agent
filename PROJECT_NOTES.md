# Sonos MCP & Voice Agent — Project Notes

## 1. Architecture & Runtime
- **Unified Backend**: Async FastAPI server (`agent:app`) running in Docker as non-root `appuser` on port `8080` (`--restart unless-stopped`), serving:
  - `POST /command` & `GET /health` for iOS Shortcuts and HTTP automation.
  - `POST /mcp` via mounted MCP v2 Streamable HTTP (`MCPServer.streamable_http_app(stateless_http=True)`).
- **Local MCP Server**: `python server.py` exposes the exact same `MCPServer` instance over `stdio` (with logging routed strictly to `stderr`).
- **Natural Language Router**: Async `httpx.AsyncClient` calling local Ollama (`qwen2.5:3b`) with strict Pydantic JSON Schema (`format`), `keep_alive: -1`, `<context_data>` prompt-injection delimiting, multi-action support, and a word-boundary EN/HE keyword fallback.
- **Typed Service & Workflows**:
  - `config.py` — Validated settings (`SPEAKER_IPS` JSON or `room:ip`, aliases, timeouts, volume caps, quiet hours, auth).
  - `models.py` — Pydantic v2 structured input/output models.
  - `sonos_service.py` — Thread-safe `SpeakerRegistry` (parallel seed probing, unicast `visible_zones` expansion, TTL cache, strict `RoomNotFoundError`), coordinator-aware playback, `DidlFavorite` unwrapping, Apple Music `ShareLinkPlugin`, UPnP 701/711 resilience, and IP-drift auto-recovery.
  - `workflows.py` — Sonos Arc home-theater controls, audio/TV handoff, hardware volume ramping, non-destructive queue curation, sleep timers, `Snapshot` announcements, and declarative `scenes.json` with `exclude_rooms`.

## 2. Network & Speaker Topology
- **Primary Soundbar Coordinator**: Living Room Sonos Arc (bonded with Era 100 surround satellites). `SpeakerRegistry` filters strictly on `is_visible` / `visible_zones` so bonded satellites sharing the `"Living Room"` name never hijack commands intended for the Arc.
- **Additional Zones**: Kids Room and Bedroom (Era 100).
- **Discovery Resilience**: Only a single reachable seed IP in `SPEAKER_IPS` is needed inside Docker bridge NAT to discover all visible zones and live DHCP IPs via unicast HTTP (`:1400`).

## 3. Troubleshooting Log & Lessons Learned
- **SoCo Timeout Attribute**: SoCo uses `soco.config.REQUEST_TIMEOUT` (NOT `HTTP_TIMEOUT`, which was a silent no-op).
- **MCP SDK v2 Migration**: Unpinned `mcp` upgraded to v2.x (`mcp>=2.3,<3.0`), where `FastMCP` was renamed to `MCPServer` (`mcp.server.mcpserver.MCPServer`). Dependencies are now explicitly pinned in `requirements.txt`.
- **Coordinator vs. Member Routing**: Playback, track info, queue ops, and sleep timers must target `speaker.group.coordinator`; volume, mute, join/unjoin, and Arc TV controls target the physical speaker.
- **Grouping Idempotency Check**: Always compare resolved coordinator UIDs/IPs rather than stale `group.uid` strings, especially right after a coordinator unjoins and promotes a member.
- **UPnP Error 701 / 800**: Live radio favorites reject `add_to_queue` (UPnP 800) and fall back cleanly to `play_uri` with `resource_meta_data` / `to_didl_string`; pausing live streams or already-paused rooms handles UPnP 701 cleanly via idempotency / `stop()`.
- **Host Sleep Port Drops**: Windows AC standby disabled (`powercfg /change standby-timeout-ac 0`) to preserve continuous Docker port forwarding.

## 4. Verification Checklist
- [x] Externalized speaker IPs, aliases, timeouts, volume caps, quiet hours, and auth to `.env` (`.env.example` provided; `.env` gitignored).
- [x] Unified Docker image runs as non-root `appuser` with `HEALTHCHECK` and serves `/command`, `/health`, and `/mcp`.
- [x] Full mocked `pytest` suite (`40+` tests) covering discovery, satellite filtering, coordinator routing, MCP v2 schemas/resources/prompts, creative workflows, security middleware, and Streamable HTTP `/mcp`.
- [x] Complete tool reference documented in `docs/TOOLS.md`.
