# Sonos Voice Agent - Project Notes

## 1. Project Overview and Architecture
- **Backend**: FastAPI server running in Docker on port `8080` with `--restart unless-stopped`.
- **Natural Language Router**: Local Ollama instance running `qwen2.5:3b` (`http://host.docker.internal:11434/api/chat`) configured with `format: "json"`, `temperature: 0`, and `keep_alive: -1` to prevent cold start timeouts.
- **Speaker Control**: Python `soco` library communicating directly with Sonos UPnP endpoints on the local LAN.
- **Client**: iOS Shortcuts sending HTTP POST JSON requests (`{"command": "..."}`) to `<HOST_LAN_IP>:8080/command` and speaking the `reply` field aloud.

## 2. Network and Speaker Topology
- **Primary Coordinator**: Living Room Sonos Arc (e.g., `192.168.1.100`), bonded with surround satellites. Always target the primary soundbar IP rather than bonded surround IPs.
- **Additional Zones**: Kids Room (`192.168.1.101`) and Bedroom (`192.168.1.102`).
- **Discovery**: Dynamic zone lookup via `SoCo(seed_ip).all_zones` normalized by lowercase room name, with static dictionary fallback loaded from `SPEAKER_IPS`.

## 3. Core Capabilities and Fallback Logic
- **Supported Actions**: `get_status`, `play`, `pause`, and `set_volume` (clamped `0-100`), defaulting to `living_room`.
- **Live Favorites Injection**: Queries `music_library.get_sonos_favorites()` per request and injects saved station titles into the system prompt.
- **Phonetic Aliases**: Maps spoken variations (`galgalatz` -> `glglz`, `white noise` -> `pink noise`) before matching favorites.
- **Favorite Playback Safety**: Unwraps `DidlFavorite.reference` and queues items via `add_to_queue` with `to_didl_string` metadata fallback.
- **Apple Music Catalog Fallback**: Queries the public iTunes Search API (`itunes.apple.com/search`) for track links and queues them through `soco.plugins.sharelink.ShareLinkPlugin`.

## 4. Troubleshooting Log and Wins
- **Cold Start Timeouts**: Fixed 15-second read timeouts by passing `"keep_alive": -1` to Ollama and raising the HTTP timeout to 45 seconds.
- **UPnP Error 701 / 800 and Empty Byte Replies**: Wrapped favorite playback in `try_favorite_safe` so UPnP rejections fall back cleanly to Apple Music `ShareLinkPlugin` queuing.
- **Coordinator vs Surround IP**: Verified model names via `http://<IP>:1400/xml/device_description.xml` to ensure commands hit the Sonos Arc rather than bonded Era 100 SL satellites.
- **Host Sleep Port Drops**: Disabled Windows AC standby (`powercfg /change standby-timeout-ac 0`) so Docker port forwarding stays active continuously.

## 5. GitHub Repository Checklist
- [x] Externalize speaker IPs and Ollama settings to environment variables (`SPEAKER_IPS`, `OLLAMA_URL`, `OLLAMA_MODEL`).
- [x] Provide `.env.example` with generic placeholder IPs and exclude `.env` via `.gitignore`.
- [ ] Add `README.md` with Docker build/run commands and iOS Shortcut setup steps.
- [ ] Add `requirements.txt` and `Dockerfile` review for clean public cloning.
- [ ] Future features: multiroom zone grouping (`join`/`unjoin`) and track skipping (`next`/`previous`).
