"""Higher-level creative Sonos workflows built atop SonosService.

Includes:
- Sonos Arc / soundbar home-theater EQ controls (Night Mode, Speech Enhancement, Sub/Surround gain)
- Seamless audio & TV handoff ("follow me" transfer or multi-room extension)
- Hardware-accelerated volume ramping (`ramp_to_volume`)
- Non-destructive queue inspection, management, and multi-track Apple Music curation
- Native Sonos sleep timers
- Snapshot-and-restore announcements/chimes
- Declarative household scenes with room exclusion guards
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Literal, Optional
from urllib.parse import urlparse

from soco.exceptions import NotSupportedException, SoCoException
from soco.snapshot import Snapshot

from models import (
    ActionResult,
    AnnouncementResult,
    HandoffMode,
    HandoffResult,
    QueueAction,
    QueueItem,
    QueueState,
    RampType,
    SceneDefinition,
    SceneResult,
    ScenesList,
    SleepTimerResult,
    TvAudioSettings,
    VolumeResult,
)
from sonos_service import (
    MediaNotFoundError,
    SonosService,
    SonosServiceError,
    UnsupportedOperationError,
    _get_coordinator,
    _is_soundbar,
    get_service,
)

logger = logging.getLogger("sonos_service.workflows")

_ALLOWED_ANNOUNCEMENT_SCHEMES = {"http", "https"}
_MAX_ANNOUNCEMENT_SECONDS = 60.0
_MAX_CURATED_TRACKS = 20

# Play-mode lookup table mapping (shuffle, repeat) -> Sonos UPnP play_mode
_PLAY_MODES = {
    (False, "none"): "NORMAL",
    (False, "all"): "REPEAT_ALL",
    (False, "one"): "REPEAT_ONE",
    (True, "none"): "SHUFFLE_NOREPEAT",
    (True, "all"): "SHUFFLE",
    (True, "one"): "SHUFFLE_REPEAT_ONE",
}
_PLAY_MODE_FLAGS = {v: k for k, v in _PLAY_MODES.items()}


def _validate_announcement_uri(uri: str) -> str:
    cleaned = (uri or "").strip()
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in _ALLOWED_ANNOUNCEMENT_SCHEMES or not parsed.netloc:
        raise SonosServiceError(
            "Announcement URI must be a valid http:// or https:// URL.",
            code="invalid_uri",
            details={"uri": cleaned},
        )
    return cleaned


class SonosWorkflows:
    """Creative multi-step and hardware-specific Sonos workflows."""

    def __init__(
        self,
        service: SonosService,
        *,
        snapshot_factory: Callable[[Any], Any] = Snapshot,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.service = service
        self._snapshot_factory = snapshot_factory
        self._sleep_fn = sleep_fn

    # ------------------------------------------------------------------
    # 1. Sonos Arc / Soundbar TV Audio Controls
    # ------------------------------------------------------------------

    def configure_tv_audio(
        self,
        room: Optional[str] = None,
        *,
        night_mode: Optional[bool] = None,
        speech_enhancement: Optional[bool] = None,
        sub_gain: Optional[int] = None,
        surround_level: Optional[int] = None,
    ) -> ActionResult:
        """Configure Sonos soundbar (Arc / Beam / Ray) Night Mode, Speech Enhancement, Sub, and Surround gain."""
        if all(v is None for v in (night_mode, speech_enhancement, sub_gain, surround_level)):
            raise SonosServiceError(
                "Provide at least one setting: night_mode, speech_enhancement, sub_gain, or surround_level.",
                code="invalid_parameters",
            )

        def _op(resolved_room: str, speaker: Any) -> ActionResult:
            if not _is_soundbar(speaker):
                soundbars = [
                    r for r, spk in sorted(self.service.registry.discover().items()) if _is_soundbar(spk)
                ]
                hint = f" Soundbar rooms: {soundbars}." if soundbars else ""
                raise UnsupportedOperationError(
                    f"Room '{resolved_room}' is not a home-theater soundbar.{hint}",
                    details={"room": resolved_room, "soundbar_rooms": soundbars},
                )

            changes: List[str] = []
            if night_mode is not None:
                speaker.night_mode = bool(night_mode)
                changes.append(f"night_mode={'on' if night_mode else 'off'}")
            if speech_enhancement is not None:
                speaker.dialog_mode = bool(speech_enhancement)
                changes.append(f"speech_enhancement={'on' if speech_enhancement else 'off'}")
            if sub_gain is not None:
                clamped_sub = max(-15, min(15, int(sub_gain)))
                speaker.sub_gain = clamped_sub
                changes.append(f"sub_gain={clamped_sub}")
            if surround_level is not None:
                clamped_surr = max(-15, min(15, int(surround_level)))
                speaker.surround_volume_tv = clamped_surr
                changes.append(f"surround_level={clamped_surr}")

            settings_out = TvAudioSettings(
                night_mode=getattr(speaker, "night_mode", None),
                speech_enhancement=getattr(speaker, "dialog_mode", None),
                sub_gain=getattr(speaker, "sub_gain", None),
                surround_level=getattr(speaker, "surround_volume_tv", None),
            )
            return ActionResult(
                ok=True,
                action="configure_tv_audio",
                room=resolved_room,
                message=f"Updated TV audio on {resolved_room}: {', '.join(changes)}.",
                state=self.service._build_room_state(resolved_room, speaker),
                details=settings_out.model_dump(exclude_none=True),
            )

        return self.service._run_with_recovery(room, _op)

    # ------------------------------------------------------------------
    # 2. Seamless Audio / TV Handoff ("Follow Me")
    # ------------------------------------------------------------------

    def handoff_audio(
        self,
        from_room: str,
        to_rooms: List[str],
        mode: HandoffMode = "transfer",
    ) -> HandoffResult:
        """Transfer or extend active audio from one room to one or more target rooms."""
        if not to_rooms:
            raise SonosServiceError("Provide at least one target room in to_rooms.", code="invalid_parameters")

        src_room, src_speaker = self.service.registry.get_speaker(from_room)
        src_state = self.service._build_room_state(src_room, src_speaker)

        resolved_targets: List[str] = []
        for raw_target in to_rooms:
            target_name, _ = self.service.registry.get_speaker(raw_target)
            if target_name != src_room and target_name not in resolved_targets:
                resolved_targets.append(target_name)

        if not resolved_targets:
            raise SonosServiceError(
                f"No distinct target rooms provided (source room is '{src_room}').",
                code="invalid_parameters",
            )

        # TV HDMI input is physically attached to the soundbar; unjoining it stops TV broadcast
        if mode == "transfer" and src_state.source_type == "tv":
            raise UnsupportedOperationError(
                f"Cannot transfer TV HDMI audio away from '{src_room}' because the HDMI input is physically "
                "tethered to the soundbar. Use mode='extend' to mirror TV audio to other rooms instead.",
                details={"from_room": src_room, "to_rooms": resolved_targets, "source_type": "tv"},
            )

        # 1. Join all target rooms into source coordinator so playback stays uninterrupted
        self.service.group_rooms(src_room, resolved_targets)

        # 2. For 'transfer', detach source room so audio continues seamlessly only on target rooms
        if mode == "transfer":
            self.service.ungroup_rooms([src_room])
            msg = f"Transferred audio from {src_room} to {', '.join(resolved_targets)}."
        else:
            msg = f"Extended {src_room} audio to {', '.join(resolved_targets)}."

        return HandoffResult(
            ok=True,
            mode=mode,
            from_room=src_room,
            to_rooms=resolved_targets,
            message=msg,
        )

    # ------------------------------------------------------------------
    # 3. Hardware Volume Fade (`ramp_to_volume`)
    # ------------------------------------------------------------------

    def fade_volume(
        self,
        target_volume: int,
        room: Optional[str] = None,
        *,
        ramp_type: RampType = "SLEEP_TIMER_RAMP_TYPE",
        force: bool = False,
    ) -> VolumeResult:
        """Smoothly ramp speaker volume using native Sonos hardware ramping (with volume guardrails)."""

        def _op(resolved_room: str, speaker: Any) -> VolumeResult:
            prev_vol = max(0, min(100, int(speaker.volume)))
            safe_vol, capped, cap_reason = self.service._apply_volume_guardrails(
                resolved_room,
                target_volume,
                force=force,
            )
            ramp_seconds = speaker.ramp_to_volume(safe_vol, ramp_type=ramp_type)
            cap_note = f" (capped by {cap_reason}; pass force=true to override)" if capped and cap_reason else ""
            return VolumeResult(
                ok=True,
                action="fade_volume",
                room=resolved_room,
                message=f"Fading {resolved_room} volume from {prev_vol} to {safe_vol}{cap_note}.",
                volume=safe_vol,
                previous_volume=prev_vol,
                capped=capped,
                cap_reason=cap_reason,
                state=self.service._build_room_state(resolved_room, speaker),
                details={"ramp_type": ramp_type, "estimated_ramp_seconds": ramp_seconds},
            )

        return self.service._run_with_recovery(room, _op)

    # ------------------------------------------------------------------
    # 4. Non-Destructive Queue Inspection, Management & Curation
    # ------------------------------------------------------------------

    def get_queue(self, room: Optional[str] = None, start: int = 0, limit: int = 25) -> QueueState:
        """Inspect upcoming tracks, play_mode, and crossfade on a room's group coordinator."""
        safe_start = max(0, int(start))
        safe_limit = max(1, min(100, int(limit)))

        def _op(resolved_room: str, speaker: Any) -> QueueState:
            coord = _get_coordinator(speaker)
            raw_items = list(coord.get_queue(start=safe_start, max_items=safe_limit))
            items: List[QueueItem] = []
            for idx, entry in enumerate(raw_items, start=safe_start):
                items.append(
                    QueueItem(
                        position=idx,
                        title=str(getattr(entry, "title", "Unknown")),
                        artist=getattr(entry, "creator", None),
                        album=getattr(entry, "album", None),
                    )
                )
            total = getattr(raw_items, "total_matches", None)
            total_items = int(total) if total is not None else len(items)
            coord_name = self.service.registry.resolve_room_alias(getattr(coord, "player_name", resolved_room))
            return QueueState(
                room=resolved_room,
                coordinator=coord_name,
                total_items=total_items,
                play_mode=getattr(coord, "play_mode", None),
                cross_fade=getattr(coord, "cross_fade", None),
                items=items,
            )

        return self.service._run_with_recovery(room, _op)

    def manage_queue(
        self,
        action: QueueAction,
        room: Optional[str] = None,
        *,
        index: Optional[int] = None,
        enabled: Optional[bool] = None,
        repeat_mode: Optional[Literal["none", "all", "one"]] = None,
    ) -> ActionResult:
        """Manage a room coordinator's queue (clear, remove, jump, shuffle, repeat, crossfade)."""

        def _op(resolved_room: str, speaker: Any) -> ActionResult:
            coord = _get_coordinator(speaker)
            details: Dict[str, Any] = {"queue_action": action}

            if action == "clear":
                coord.clear_queue()
                msg = f"Cleared playback queue in {resolved_room}."
            elif action == "remove":
                if index is None or index < 0:
                    raise SonosServiceError("Provide a non-negative 0-based 'index' for remove.", code="invalid_parameters")
                coord.remove_from_queue(int(index))
                details["index"] = int(index)
                msg = f"Removed track at queue position {index} in {resolved_room}."
            elif action == "jump":
                if index is None or index < 0:
                    raise SonosServiceError("Provide a non-negative 0-based 'index' for jump.", code="invalid_parameters")
                coord.play_from_queue(int(index))
                details["index"] = int(index)
                msg = f"Jumped to queue position {index} in {resolved_room}."
            elif action == "shuffle":
                if enabled is None:
                    raise SonosServiceError("Provide boolean 'enabled' for shuffle.", code="invalid_parameters")
                cur_shuffle, cur_repeat = _PLAY_MODE_FLAGS.get(str(getattr(coord, "play_mode", "NORMAL")).upper(), (False, "none"))
                new_mode = _PLAY_MODES[(bool(enabled), cur_repeat)]
                coord.play_mode = new_mode
                details["play_mode"] = new_mode
                msg = f"Shuffle {'enabled' if enabled else 'disabled'} in {resolved_room} (play_mode={new_mode})."
            elif action == "repeat":
                if repeat_mode not in ("none", "all", "one"):
                    raise SonosServiceError("Provide repeat_mode as 'none', 'all', or 'one'.", code="invalid_parameters")
                cur_shuffle, _ = _PLAY_MODE_FLAGS.get(str(getattr(coord, "play_mode", "NORMAL")).upper(), (False, "none"))
                new_mode = _PLAY_MODES[(cur_shuffle, repeat_mode)]
                coord.play_mode = new_mode
                details["play_mode"] = new_mode
                msg = f"Repeat mode set to '{repeat_mode}' in {resolved_room} (play_mode={new_mode})."
            elif action == "crossfade":
                if enabled is None:
                    raise SonosServiceError("Provide boolean 'enabled' for crossfade.", code="invalid_parameters")
                coord.cross_fade = bool(enabled)
                details["cross_fade"] = bool(enabled)
                msg = f"Crossfade {'enabled' if enabled else 'disabled'} in {resolved_room}."
            else:
                raise UnsupportedOperationError(f"Unsupported queue action: {action}")

            return ActionResult(
                ok=True,
                action="manage_queue",
                room=resolved_room,
                message=msg,
                state=self.service._build_room_state(resolved_room, speaker),
                details=details,
            )

        return self.service._run_with_recovery(room, _op)

    def curate_queue(
        self,
        tracks: List[str],
        room: Optional[str] = None,
        *,
        mode: Literal["replace", "append"] = "replace",
        start_playing: bool = True,
    ) -> ActionResult:
        """Resolve a batch of song queries in parallel via Apple Music and enqueue them cleanly."""
        cleaned = [t.strip() for t in tracks if t and t.strip()]
        if not cleaned:
            raise SonosServiceError("Provide at least one non-empty track query.", code="invalid_parameters")
        if len(cleaned) > _MAX_CURATED_TRACKS:
            raise SonosServiceError(
                f"Maximum {_MAX_CURATED_TRACKS} tracks allowed per curate_queue call.",
                code="invalid_parameters",
            )

        # Resolve iTunes search matches in parallel while preserving requested order
        resolved_hits: Dict[int, Optional[Dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=min(6, len(cleaned)), thread_name_prefix="sonos-curate") as pool:
            futs = {
                pool.submit(self.service._search_apple_music_tracks, query, 1): idx
                for idx, query in enumerate(cleaned)
            }
            for fut in as_completed(futs):
                idx = futs[fut]
                hits = fut.result()
                valid = [h for h in hits if h.get("trackViewUrl")]
                resolved_hits[idx] = valid[0] if valid else None

        def _op(resolved_room: str, speaker: Any) -> ActionResult:
            coord = _get_coordinator(speaker)
            plugin = self.service._sharelink_factory(coord)

            if mode == "replace":
                coord.clear_queue()

            added_titles: List[str] = []
            failed_queries: List[str] = []

            for idx, query in enumerate(cleaned):
                hit = resolved_hits.get(idx)
                if not hit:
                    failed_queries.append(query)
                    continue
                try:
                    plugin.add_share_link_to_queue(hit["trackViewUrl"])
                    title = hit.get("trackName", query)
                    artist = hit.get("artistName", "Unknown")
                    added_titles.append(f"{title} — {artist}")
                except Exception as exc:
                    logger.debug("ShareLink enqueue failed for '%s': %s", query, exc)
                    failed_queries.append(query)

            if not added_titles:
                raise MediaNotFoundError(
                    f"Could not resolve or enqueue any tracks from: {cleaned}",
                    details={"failed_queries": failed_queries},
                )

            if start_playing and mode == "replace":
                coord.play_from_queue(0)
            elif start_playing and mode == "append":
                try:
                    transport = coord.get_current_transport_info() or {}
                    if str(transport.get("current_transport_state", "")).upper() != "PLAYING":
                        coord.play_from_queue(0)
                except Exception:
                    pass

            msg = f"Queued {len(added_titles)} track(s) in {resolved_room}"
            if failed_queries:
                msg += f" ({len(failed_queries)} unmatched: {', '.join(failed_queries)})"
            msg += "."

            return ActionResult(
                ok=True,
                action="curate_queue",
                room=resolved_room,
                message=msg,
                state=self.service._build_room_state(resolved_room, speaker),
                details={
                    "mode": mode,
                    "added_tracks": added_titles,
                    "failed_queries": failed_queries,
                },
            )

        return self.service._run_with_recovery(room, _op)

    # ------------------------------------------------------------------
    # 5. Native Sonos Sleep Timer
    # ------------------------------------------------------------------

    def set_sleep_timer(self, room: Optional[str] = None, minutes: Optional[int] = None) -> SleepTimerResult:
        """Set or cancel a native Sonos sleep timer on a room's group coordinator."""
        if minutes is not None and minutes < 0:
            raise SonosServiceError("Sleep timer minutes must be >= 0 (or null to cancel).", code="invalid_parameters")

        def _op(resolved_room: str, speaker: Any) -> SleepTimerResult:
            coord = _get_coordinator(speaker)
            coord_name = self.service.registry.resolve_room_alias(getattr(coord, "player_name", resolved_room))
            if minutes is None or minutes == 0:
                coord.set_sleep_timer(None)
                return SleepTimerResult(
                    ok=True,
                    room=resolved_room,
                    coordinator=coord_name,
                    remaining_seconds=None,
                    message=f"Cancelled sleep timer for {resolved_room}.",
                )

            seconds = int(minutes) * 60
            coord.set_sleep_timer(seconds)
            return SleepTimerResult(
                ok=True,
                room=resolved_room,
                coordinator=coord_name,
                remaining_seconds=seconds,
                message=f"Sleep timer set for {minutes} minute(s) on {resolved_room}.",
            )

        return self.service._run_with_recovery(room, _op)

    # ------------------------------------------------------------------
    # 6. Snapshot & Restore Announcements / Chimes
    # ------------------------------------------------------------------

    def announce_audio(
        self,
        rooms: List[str],
        uri: str,
        *,
        volume: int = 30,
        duration_seconds: float = 5.0,
        force: bool = False,
    ) -> AnnouncementResult:
        """Snapshot rooms, play a short audio chime/announcement URI, and seamlessly restore prior state."""
        if not rooms:
            raise SonosServiceError("Provide at least one target room for announcement.", code="invalid_parameters")
        safe_uri = _validate_announcement_uri(uri)
        wait_seconds = max(0.5, min(_MAX_ANNOUNCEMENT_SECONDS, float(duration_seconds)))

        resolved: List[tuple[str, Any]] = []
        seen: set[str] = set()
        for raw_room in rooms:
            r_name, spk = self.service.registry.get_speaker(raw_room)
            if r_name not in seen:
                seen.add(r_name)
                resolved.append((r_name, spk))

        snapshots: List[tuple[str, Any]] = []
        try:
            for r_name, spk in resolved:
                snap = self._snapshot_factory(spk)
                snap.snapshot()
                snapshots.append((r_name, snap))

            # Apply guarded volume and play chime on each target room
            for r_name, spk in resolved:
                safe_vol, _, _ = self.service._apply_volume_guardrails(r_name, volume, force=force)
                coord = _get_coordinator(spk)
                spk.volume = safe_vol
                coord.play_uri(safe_uri, title="Announcement")

            self._sleep_fn(wait_seconds)
        finally:
            restored: List[str] = []
            failed_restore: List[str] = []
            for r_name, snap in snapshots:
                try:
                    snap.restore(fade=True)
                    restored.append(r_name)
                except Exception as exc:
                    logger.warning("Failed to restore snapshot on '%s': %s", r_name, exc)
                    failed_restore.append(r_name)

        room_names = [r for r, _ in resolved]
        msg = f"Played announcement on {', '.join(room_names)} and restored {', '.join(restored)}."
        if failed_restore:
            msg += f" Restore failed on: {', '.join(failed_restore)}."

        return AnnouncementResult(
            ok=(len(failed_restore) == 0),
            rooms=room_names,
            restored=restored,
            failed_restore=failed_restore,
            message=msg,
        )

    # ------------------------------------------------------------------
    # 7. Declarative Household Scenes (scenes.json)
    # ------------------------------------------------------------------

    def _load_scenes_file(self) -> List[SceneDefinition]:
        path = Path(self.service.settings.scenes_path)
        if not path.is_absolute():
            candidate = Path(__file__).resolve().parent / path
            if candidate.exists():
                path = candidate
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            items = raw.get("scenes", []) if isinstance(raw, dict) else []
            return [SceneDefinition.model_validate(item) for item in items]
        except Exception as exc:
            logger.warning("Failed to load scenes file %s: %s", path, exc)
            return []

    def list_scenes(self) -> ScenesList:
        scenes = self._load_scenes_file()
        return ScenesList(count=len(scenes), scenes=scenes)

    def apply_scene(
        self,
        scene_name: str,
        *,
        exclude_rooms: Optional[List[str]] = None,
        force: bool = False,
    ) -> SceneResult:
        """Execute a declarative multi-step household scene while respecting excluded rooms (e.g., sleeping baby)."""
        target_key = scene_name.strip().lower()
        scenes = self._load_scenes_file()
        scene = next((s for s in scenes if s.name.strip().lower() == target_key), None)
        if scene is None:
            available = [s.name for s in scenes]
            raise SonosServiceError(
                f"Unknown scene '{scene_name}'. Available scenes: {available}",
                code="scene_not_found",
                details={"requested_scene": scene_name, "available_scenes": available},
            )

        excluded = {self.service.registry.resolve_room_alias(r) for r in (exclude_rooms or []) if str(r).strip()}
        executed: List[str] = []
        warnings: List[str] = []

        for step in scene.steps:
            act = step.action
            params = dict(step.params)

            # Skip single-room step if that room is excluded
            if "room" in params:
                step_room = self.service.registry.resolve_room_alias(str(params["room"]))
                if step_room in excluded:
                    warnings.append(f"Skipped {act} on excluded room '{step_room}'.")
                    continue

            try:
                if act == "ungroup":
                    rooms = [
                        self.service.registry.resolve_room_alias(r)
                        for r in params.get("rooms", [])
                    ]
                    rooms = [r for r in rooms if r not in excluded]
                    if rooms:
                        res = self.service.ungroup_rooms(rooms)
                        executed.append(res.message)

                elif act == "group_all":
                    coord_room = self.service.registry.resolve_room_alias(params.get("coordinator_room"))
                    if coord_room in excluded:
                        warnings.append(f"Skipped group_all because coordinator '{coord_room}' is excluded.")
                        continue
                    all_rooms = self.service.list_rooms()
                    members = [r for r in all_rooms if r != coord_room and r not in excluded]
                    if members:
                        res = self.service.group_rooms(coord_room, members)
                        executed.append(res.message)
                    else:
                        warnings.append("No non-excluded rooms to group.")

                elif act == "pause_all":
                    all_rooms = self.service.list_rooms()
                    targets = [r for r in all_rooms if r not in excluded]
                    for r in targets:
                        res = self.service.control_playback("pause", room=r)
                        executed.append(res.message)

                elif act == "play_media":
                    res = self.service.play_media(
                        query=params["query"],
                        room=params.get("room"),
                        source=params.get("source", "auto"),
                        mode=params.get("mode", "replace"),
                    )
                    executed.append(res.message)

                elif act == "set_volume":
                    res = self.service.set_volume(
                        room=params.get("room"),
                        level=params.get("level"),
                        delta=params.get("delta"),
                        mute=params.get("mute"),
                        apply_to_group=bool(params.get("apply_to_group", False)),
                        force=force,
                    )
                    executed.append(res.message)

                elif act == "fade_volume":
                    res = self.fade_volume(
                        target_volume=int(params["target_volume"]),
                        room=params.get("room"),
                        ramp_type=params.get("ramp_type", "SLEEP_TIMER_RAMP_TYPE"),
                        force=force,
                    )
                    executed.append(res.message)

                elif act == "select_source":
                    res = self.service.select_source(params["source"], room=params.get("room"))
                    executed.append(res.message)

                elif act == "configure_tv_audio":
                    res = self.configure_tv_audio(
                        room=params.get("room"),
                        night_mode=params.get("night_mode"),
                        speech_enhancement=params.get("speech_enhancement"),
                        sub_gain=params.get("sub_gain"),
                        surround_level=params.get("surround_level"),
                    )
                    executed.append(res.message)

                elif act == "set_sleep_timer":
                    res = self.set_sleep_timer(room=params.get("room"), minutes=params.get("minutes"))
                    executed.append(res.message)

                elif act == "manage_queue":
                    res = self.manage_queue(
                        action=params["action"],
                        room=params.get("room"),
                        index=params.get("index"),
                        enabled=params.get("enabled"),
                        repeat_mode=params.get("repeat_mode"),
                    )
                    executed.append(res.message)
                else:
                    warnings.append(f"Ignored unknown scene action '{act}'.")

            except (SonosServiceError, NotSupportedException, SoCoException) as exc:
                warnings.append(f"Step '{act}' skipped: {exc}")

        summary = f"Applied scene '{scene.name}' ({len(executed)} step(s) executed"
        if warnings:
            summary += f", {len(warnings)} warning(s)"
        summary += ")."

        return SceneResult(
            ok=len(executed) > 0,
            scene=scene.name,
            steps_executed=executed,
            warnings=warnings,
            message=summary,
        )


def get_workflows() -> SonosWorkflows:
    """Build a SonosWorkflows bound to the current default SonosService singleton."""
    return SonosWorkflows(get_service())
