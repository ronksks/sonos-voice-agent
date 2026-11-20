# Project Progress & Lessons Learned Log

This document tracks architectural phases, verification checks, bugs caught, and lessons learned.

---

## v3.0 Audit & Full Refactoring (2026-11-19)

### Baseline Audit Findings (Resolved)
1. **Broken `Dockerfile` ASGI CMD**: `uvicorn server:mcp` crashed because `FastMCP` was not an ASGI callable and disabled `agent:app` (`/command`, `/health`). Resolved by mounting MCP v2 Streamable HTTP inside FastAPI (`agent:app`) and running as non-root `appuser`.
2. **`SPEAKER_IPS` Format Mismatch & Missing `env_file`**: `.env` stored JSON while `sonos_service.py` parsed `room:ip` pairs, and `docker-compose.yml` omitted `env_file: .env`. Resolved in `config.py` (supports both JSON and delimited formats with IP validation) and `docker-compose.yml`.
3. **Deleted Media Playback & `DidlFavorite.uri` Crash**: Commit `4d84bca` regressed Apple Music `ShareLinkPlugin` search to a no-op `speaker.play()` and referenced nonexistent `matched.uri`. Restored full `DidlFavorite.reference` unwrapping, DIDL metadata fallback, and non-destructive Apple Music queuing.
4. **Missing Coordinator Routing & Bonded Satellite Collisions**: Grouped slaves threw `SoCoSlaveException`, and SSDP set order could bind `living_room` to an invisible Era 100 surround instead of the Sonos Arc. Resolved via `_get_coordinator()` and `is_visible` / `visible_zones` filtering.
5. **Uncached Discovery & Bogus Timeout Attribute**: Every `/command` ran 3–4 full 2s SSDP scans, and prior code set `soco.config.HTTP_TIMEOUT` (a silent no-op instead of `soco.config.REQUEST_TIMEOUT`). Resolved via thread-safe `SpeakerRegistry` TTL cache, parallel seed probing, and `soco.config.REQUEST_TIMEOUT`.
6. **MCP SDK v2 & Tool Ergonomics**: Upgraded to `mcp>=2.3,<3.0` (`MCPServer`), replaced prose returns and swallowed errors with Pydantic structured outputs, `ToolAnnotations`, `ToolError`, 4 live resources, and 4 workflow prompts.
7. **Voice Agent Safety & Fallback Bugs**: Migrated `requests.Session` to lifecycle-managed async `httpx.AsyncClient`, enforced Ollama Pydantic JSON Schema (`format`), fixed substring fallback collisions (`"Don't Stop Believing"`, `"MTV"`, `"Stevie Wonder"`), and added optional API key + `Origin`/`Host` security middleware.

---

### Completed Phases & Commits

| Phase | Scope | Verification |
|-------|-------|--------------|
| **Phase 1** | `config.py`, `models.py`, rebuilt typed `sonos_service.py` with `SpeakerRegistry` caching, `visible_zones`, coordinator routing, guardrails, and IP-drift recovery | `20/20` unit tests passing (`tests/test_config.py`, `test_discovery.py`, `test_sonos_service.py`) |
| **Phase 2** | Redesigned `server.py` on `MCPServer` v2 with structured outputs, `ToolAnnotations`, `ToolError`, `stderr` logging, and 3 live resources | `26/26` tests passing (`+tests/test_mcp_server.py`) |
| **Phase 3** | Added `workflows.py`, `scenes.json`, 10 advanced MCP tools (Arc TV EQ, handoff, fade, queue curation, sleep timer, snapshot announcements, scenes with `exclude_rooms`), `sonos://scenes`, and 4 MCP prompts | `34/34` tests passing (`+tests/test_workflows.py`; caught & fixed post-unjoin coordinator grouping bug) |
| **Phase 4** | Refactored `agent.py` to async FastAPI + `httpx.AsyncClient`, Ollama JSON Schema multi-action router, word-boundary EN/HE fallback, API key & Origin/Host middleware, and mounted Streamable HTTP `/mcp` | `40/40` tests passing (`+tests/test_agent.py` incl. E2E JSON-RPC over `/mcp`) |
| **Phase 5** | Hardened non-root `Dockerfile`, fixed `docker-compose.yml` (`env_file`, `healthcheck`), pinned `requirements.txt` + `requirements-dev.txt`, updated `.env.example`/`.dockerignore`, removed legacy `agent_gemini_backup.py` & `payload.json` | `docker compose config` exit 0; `docker build` + `--network=none` non-root UID 1000 smoke test verified `/health` + `/mcp` (`18` tools) |
| **Phase 6** | Updated `README.md`, `PROJECT_NOTES.md`, `PROGRESS.md`, and created `docs/TOOLS.md` | Full test suite + clean `git log` review |

---

## v3.0.1 Voice Routing & Prompt Fix (2026-11-20)

- **Root Cause**: `qwen2.5:3b` turned `"What’s playing in the living room?"` into playing `Pink Noise` because `_build_system_prompt()` omitted `get_status` (and most tool names) while putting `Pink Noise` as an early example, and `fallback_keyword_parse()` didn't normalize iOS curly apostrophes (`\u2019`).
- **Fixes in `agent.py`**:
  - Added `_normalize_punctuation()` converting curly apostrophes/quotes to ASCII across `/command` and `fallback_keyword_parse()`.
  - Added `_STATUS_PHRASES` and deterministic `_fast_path_parse()` for unambiguous single-intent read-only status queries (skipping Ollama while still passing compound/mutating commands through).
  - Updated `_build_system_prompt()` to list all 15 `VoiceToolName` tools (starting with `get_status`), include balanced few-shot examples, and remove hardcoded `Pink Noise` / `GLGLZ` rules outside `<context_data>`.
- **Verification**: Added 3 regression tests in `tests/test_agent.py` (`43/43` passing).
