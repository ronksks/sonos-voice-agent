# Project Progress & Lessons Learned Log

This document tracks every change, verification check, what worked, what failed, and lessons learned so we avoid repeating mistakes.

---

## Baseline Audit (2026-11-14)
### What Was Broken / Risky
1. **Syntax Error in `agent.py` (lines 325-346)**: A previous partial edit left an orphaned `"options": ...` block at the bottom of `agent.py`, preventing `uvicorn agent:app` from starting.
2. **UPnP Discovery vs Docker Bridge Mode**: `soco.discovery.discover()` uses UDP multicast (`239.255.255.250:1900`), which does not reliably cross Docker bridge NAT on Windows/macOS unless `SPEAKER_IPS` is configured or host networking is used.
3. **Permanent Stale Cache (`_discovered_cache`)**: Once populated (even empty or partial), `_discovered_cache` never refreshed and had no thread lock.
4. **Hardcoded Fallback IP (`192.168.1.100`)**: `get_seed_device()` fell back to a hard-coded IP, causing 5s timeouts on networks with different subnets.
5. **Global Socket Timeout Mutation**: `socket.setdefaulttimeout(5.0)` mutated process-wide sockets (affecting Uvicorn/HTTP calls).
6. **LLM `null` Fields & Unhandled Exceptions**: `args.get("room", DEFAULT_ROOM)` and `int(args.get("volume", 15))` crashed when the LLM returned JSON `null`; `play_apple_music()` didn't catch `requests.RequestException`; `/command` returned `HTTPException` (`{"detail": ...}`) instead of a speakable `reply` JSON for iOS Shortcuts.
7. **Cold-Start Timeout Mismatch**: `OLLAMA_TIMEOUT` was hardcoded to `15s` in `agent.py` even though `PROJECT_NOTES.md` documented `15s` causing cold-start failures and needing `45s`.
8. **Duplicate UPnP Favorites Calls**: Every `/command` queried `get_sonos_favorites()` twice (once for prompt injection, once in `play_favorite()`).
9. **Misleading `/health` Endpoint**: Always returned HTTP 200 `"healthy"` even if Ollama was unreachable and zero speakers were found, and didn't verify `OLLAMA_MODEL` was actually pulled.
10. **Dependency & Container Drift**: `requirements.txt` only listed `requests`, while `Dockerfile` installed unpinned packages inline; `docker-compose.yml` was missing `env_file: .env`; no `.dockerignore` existed; stray `nul` (212 KB) file created by MinGW/bash `2>nul` was sitting in the repo root.

---

## Work Log

### Step 1: Initialize Progress Tracking & Clean Repo Hygiene
- **Status**: In progress
- **Plan**:
  - Maintain `PROGRESS.md` throughout all edits and tests.
  - Remove stray `nul` file safely via Windows UNC path (`\\?\...`) and ignore `nul` / `.venv/` in `.gitignore` and `.dockerignore`.
  - Archive or clean up unused scratch files (`payload.json`, `agent_gemini_backup.py` -> move or document; keep workspace tidy).
