# Sonos MCP Tool, Resource & Prompt Reference

This document details the complete MCP v2 contract exposed by [server.py](../server.py) (both via local `stdio` and mounted Streamable HTTP at `POST /mcp`).

All tools return structured Pydantic JSON payloads (`structuredContent` + JSON text content) and raise protocol-level `ToolError` (`isError: true`) with machine-readable prefixes (`[room_not_found]`, `[unsupported_operation]`, `[media_not_found]`, `[speaker_unreachable]`, `[timeout]`) on failure.

---

## Core Tools

| Tool | Annotations | Key Parameters | Return Model | Notes |
|------|-------------|----------------|--------------|-------|
| `get_household_state` | `read_only`, `idempotent` | `room?: str` | `HouseholdState` | Omit `room` to inspect all visible rooms, group topology, volumes, tracks, sleep timers, and soundbar capabilities in **1 round-trip**. |
| `list_favorites` | `read_only`, `idempotent` | `force_refresh: bool = false` | `FavoritesList` | Returns cached Sonos Favorites (`title`, `description`, `uri`). Call before `play_media(source="favorite")`. |
| `control_playback` | `idempotent` | `action: "resume"\|"pause"\|"stop"\|"next"\|"previous"`, `room?: str` | `ActionResult` | Routes automatically to group coordinator; idempotent on already-paused speakers; falls back to `stop()` on UPnP 701 streams. |
| `play_media` | `destructive`, `open_world` | `query: str`, `room?: str`, `source: "auto"\|"favorite"\|"apple_music"`, `mode: "replace"\|"play_next"\|"append"` | `ActionResult` | Unwraps `DidlFavorite.reference` with queue/URI fallback; searches Apple Music via iTunes Search + `ShareLinkPlugin`; supports non-destructive queuing. |
| `set_volume` | — | `room?: str`, `level?: 0..100`, `delta?: -50..50`, `mute?: bool`, `apply_to_group: bool = false`, `force: bool = false` | `VolumeResult` | Enforces `MAX_VOLUME_BY_ROOM` and quiet-hours caps unless `force=true`. Provide at least one of `level`, `delta`, or `mute`. |
| `group_rooms` | `idempotent` | `coordinator_room: str`, `member_rooms: list[str]` | `GroupResult` | Unambiguous directionality: preserves `coordinator_room`'s active playback and joins all `member_rooms` in 1 call. |
| `ungroup_rooms` | `idempotent` | `rooms: list[str]` | `GroupResult` | Detaches one or more rooms from their current playback groups. |
| `select_source` | `idempotent` | `source: "tv"\|"line_in"`, `room?: str` | `ActionResult` | Verifies soundbar capability before switching to TV HDMI/optical input and reports available soundbars on mismatch. |

---

## Advanced & Creative Workflow Tools

| Tool | Annotations | Key Parameters | Return Model | Notes |
|------|-------------|----------------|--------------|-------|
| `configure_tv_audio` | `idempotent` | `room?: str`, `night_mode?: bool`, `speech_enhancement?: bool`, `sub_gain?: -15..15`, `surround_level?: -15..15` | `ActionResult` | Configures Sonos Arc / Beam / Ray Night Sound, Speech Enhancement, Sub gain, and TV surround levels. |
| `handoff_audio` | `idempotent` | `from_room: str`, `to_rooms: list[str]`, `mode: "transfer"\|"extend"` | `HandoffResult` | `"transfer"` seamlessly moves music to `to_rooms` and detaches `from_room`; `"extend"` mirrors music or TV HDMI audio to additional rooms. Guards against detaching a soundbar playing TV HDMI. |
| `fade_volume` | `idempotent` | `target_volume: 0..100`, `room?: str`, `ramp_type: RampType`, `force: bool = false` | `VolumeResult` | Speaker-native hardware ramping via `ramp_to_volume` (subject to volume guardrails). |
| `get_queue` | `read_only`, `idempotent` | `room?: str`, `start: int = 0`, `limit: 1..100` | `QueueState` | Lists upcoming queued tracks along with `play_mode` and `cross_fade`. |
| `manage_queue` | `destructive` | `action: "clear"\|"remove"\|"jump"\|"shuffle"\|"repeat"\|"crossfade"`, `room?: str`, `index?`, `enabled?`, `repeat_mode?` | `ActionResult` | Toggling `shuffle` preserves existing `repeat` mode and vice versa. |
| `curate_queue` | `destructive`, `open_world` | `tracks: list[str]` (1–20), `room?: str`, `mode: "replace"\|"append"`, `start_playing: bool = true` | `ActionResult` | Searches Apple Music in parallel while preserving track order and reports both `added_tracks` and any `failed_queries`. |
| `set_sleep_timer` | `idempotent` | `room?: str`, `minutes?: 0..720` | `SleepTimerResult` | Sets a native hardware sleep timer on the group coordinator; pass `0` or `null` to cancel. |
| `announce_audio` | `open_world` | `rooms: list[str]`, `uri: http(s)://...`, `volume: 0..100`, `duration_seconds: 0.5..60`, `force: bool = false` | `AnnouncementResult` | Uses `soco.snapshot.Snapshot` to save state, play an `http(s)` alert chime at guarded volume, and restore exact prior audio/TV/volume in a `finally` block. |
| `list_scenes` | `read_only`, `idempotent` | *(none)* | `ScenesList` | Returns declarative household scenes loaded from [scenes.json](../scenes.json). |
| `apply_scene` | `destructive` | `scene_name: str`, `exclude_rooms?: list[str]`, `force: bool = false` | `SceneResult` | Executes a multi-step routine while skipping any rooms in `exclude_rooms` (e.g., `["bedroom"]` when someone is sleeping). |

---

## MCP Resources

| URI | MIME Type | Description |
|-----|-----------|-------------|
| `sonos://household/topology` | `application/json` | Live snapshot of all visible rooms, active groups, volumes, tracks, and soundbar capabilities. |
| `sonos://favorites` | `application/json` | Cached Sonos Favorites & saved radio stations. |
| `sonos://scenes` | `application/json` | Configured declarative household scenes from `scenes.json`. |
| `sonos://room/{room}/now-playing` | `application/json` | Live detailed status for a specific room (accepts canonical IDs or EN/HE aliases). |

---

## MCP Prompts

| Prompt | Arguments | Workflow |
|--------|-----------|----------|
| `bedtime_kids` | `duration_minutes=60`, `volume=22` | Checks topology, ungroups `kids_room`, plays `Pink Noise`, fades volume, sets sleep timer, and enables Living Room Arc Night Mode + Speech Enhancement. |
| `movie_night` | `enable_night_mode=False` | Checks topology, detaches Living Room Arc, switches to TV HDMI, enables Speech Enhancement (and optional Night Mode). |
| `morning_radio` | `station="GLGLZ"`, `room="living_room"`, `volume=24` | Verifies favorite title, ensures room is standalone, starts station, and gently ramps volume via `AUTOPLAY_RAMP_TYPE`. |
| `party_mode` | `exclude_rooms=""`, `vibe=""` | Groups eligible rooms into `living_room` (honoring exclusions), enables crossfade, balances group volume, and optionally curates a themed queue. |
