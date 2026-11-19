"""Shared in-memory Sonos/SoCo test doubles (never touches live network or real speakers)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from soco.exceptions import NotSupportedException, SoCoSlaveException, SoCoUPnPException


class FakeDidlObject:
    def __init__(self, uri: str, title: str = ""):
        self._uri = uri
        self.title = title

    def get_uri(self) -> str:
        return self._uri


class FakeDidlFavorite:
    """Mimics soco.data_structures.DidlFavorite (notice: no .uri attribute!)."""

    def __init__(
        self,
        title: str,
        uri: str,
        *,
        description: Optional[str] = None,
        resource_meta_data: str = "",
        queueable: bool = True,
    ):
        self.title = title
        self.description = description
        self.resource_meta_data = resource_meta_data
        self.reference = FakeDidlObject(uri, title=title)
        self.queueable = queueable


class FakeMusicLibrary:
    def __init__(self, favorites: Optional[List[FakeDidlFavorite]] = None):
        self._favorites = list(favorites or [])
        self.fetch_count = 0

    def get_sonos_favorites(self) -> List[FakeDidlFavorite]:
        self.fetch_count += 1
        return list(self._favorites)


class FakeGroup:
    def __init__(self, coordinator: "FakeSoCo", members: Optional[List["FakeSoCo"]] = None, uid: Optional[str] = None):
        self.coordinator = coordinator
        self.members: List[FakeSoCo] = list(members or [coordinator])
        self.uid = uid or f"GROUP_{coordinator.uid}"

    @property
    def volume(self) -> int:
        visible = [m for m in self.members if m.is_visible]
        return visible[0].volume if visible else self.coordinator.volume

    @volume.setter
    def volume(self, val: int) -> None:
        for m in self.members:
            if m.is_visible:
                m.volume = val

    @property
    def mute(self) -> bool:
        return self.coordinator.mute

    @mute.setter
    def mute(self, val: bool) -> None:
        for m in self.members:
            if m.is_visible:
                m.mute = val


class FakeSoCo:
    """In-memory mock of soco.SoCo enforcing coordinator semantics and Sonos behaviors."""

    def __init__(
        self,
        ip_address: str,
        player_name: str,
        *,
        uid: Optional[str] = None,
        volume: int = 20,
        mute: bool = False,
        state: str = "STOPPED",
        is_visible: bool = True,
        is_soundbar: bool = False,
        model_name: str = "Sonos Era 100",
        favorites: Optional[List[FakeDidlFavorite]] = None,
        track_info: Optional[Dict[str, Any]] = None,
    ):
        self.ip_address = ip_address
        self.player_name = player_name
        self.uid = uid or f"RINCON_{ip_address.replace('.', '')}01400"
        self.volume = volume
        self.mute = mute
        self.transport_state = state
        self.is_visible = is_visible
        self.is_soundbar = is_soundbar
        self.model_name = model_name
        self.is_playing_tv = False
        self.is_playing_line_in = False
        self.night_mode: Optional[bool] = False if is_soundbar else None
        self.dialog_mode: Optional[bool] = False if is_soundbar else None
        self.sub_gain: Optional[int] = 0 if is_soundbar else None
        self.surround_volume_tv: Optional[int] = 0 if is_soundbar else None
        self.play_mode = "NORMAL"
        self.cross_fade = False
        self._sleep_timer: Optional[int] = None

        self.queue: List[Dict[str, Any]] = []
        self.played_uris: List[Dict[str, Any]] = []
        self.ramps: List[Dict[str, Any]] = []
        self.calls: List[str] = []
        self.next_error: Optional[Exception] = None
        self.fail_pause_701 = False
        self.fail_next_701 = False

        self.track_info: Dict[str, Any] = dict(
            track_info
            or {
                "title": "",
                "artist": "",
                "album": "",
                "uri": "",
                "position": "0:00:00",
                "duration": "0:00:00",
                "playlist_position": "1",
            }
        )
        self.music_library = FakeMusicLibrary(favorites)
        self.group: FakeGroup = FakeGroup(self, [self])
        self._all_zones: List[FakeSoCo] = [self]

    @property
    def is_coordinator(self) -> bool:
        return self.group.coordinator is self

    @property
    def all_zones(self) -> set["FakeSoCo"]:
        return set(self._all_zones)

    @property
    def visible_zones(self) -> set["FakeSoCo"]:
        return {z for z in self._all_zones if z.is_visible}

    def _maybe_raise(self) -> None:
        if self.next_error is not None:
            err = self.next_error
            self.next_error = None
            raise err

    def _require_coordinator(self, method: str) -> None:
        if not self.is_coordinator:
            raise SoCoSlaveException(f"The method or property '{method}' can only be called on the coordinator in a group")

    def get_speaker_info(self) -> Dict[str, Any]:
        self._maybe_raise()
        return {"zone_name": self.player_name, "model_name": self.model_name, "uid": self.uid}

    def get_current_transport_info(self) -> Dict[str, str]:
        self._maybe_raise()
        self._require_coordinator("get_current_transport_info")
        return {"current_transport_state": self.transport_state}

    def get_current_track_info(self) -> Dict[str, Any]:
        self._maybe_raise()
        self._require_coordinator("get_current_track_info")
        return dict(self.track_info)

    def play(self) -> None:
        self._maybe_raise()
        self._require_coordinator("play")
        self.calls.append("play")
        self.transport_state = "PLAYING"

    def pause(self) -> None:
        self._maybe_raise()
        self._require_coordinator("pause")
        self.calls.append("pause")
        if self.fail_pause_701:
            raise SoCoUPnPException("UPnP Error 701 received: Transition not available", error_code="701", error_xml="")
        self.transport_state = "PAUSED_PLAYBACK"

    def stop(self) -> None:
        self._maybe_raise()
        self._require_coordinator("stop")
        self.calls.append("stop")
        self.transport_state = "STOPPED"

    def next(self) -> None:
        self._maybe_raise()
        self._require_coordinator("next")
        self.calls.append("next")
        if self.fail_next_701:
            raise SoCoUPnPException("UPnP Error 701 received: Transition not available", error_code="701", error_xml="")

    def previous(self) -> None:
        self._maybe_raise()
        self._require_coordinator("previous")
        self.calls.append("previous")
        if self.fail_next_701:
            raise SoCoUPnPException("UPnP Error 711 received: Illegal seek target", error_code="711", error_xml="")

    def clear_queue(self) -> None:
        self._maybe_raise()
        self._require_coordinator("clear_queue")
        self.calls.append("clear_queue")
        self.queue.clear()

    def add_to_queue(self, item: Any, position: int = 0) -> int:
        self._maybe_raise()
        self._require_coordinator("add_to_queue")
        # Simulate live radio streams refusing queue insertion
        if getattr(item, "_uri", "").startswith("x-sonosapi-stream:"):
            raise SoCoUPnPException("UPnP Error 800 received", error_code="800", error_xml="")
        entry = {
            "title": getattr(item, "title", str(item)),
            "uri": item.get_uri() if hasattr(item, "get_uri") else str(item),
            "artist": getattr(item, "creator", None),
            "album": getattr(item, "album", None),
        }
        if position > 0:
            idx = min(len(self.queue), position - 1)
            self.queue.insert(idx, entry)
        else:
            self.queue.append(entry)
        self.calls.append(f"add_to_queue:{entry['title']}")
        return len(self.queue)

    def play_from_queue(self, index: int) -> None:
        self._maybe_raise()
        self._require_coordinator("play_from_queue")
        self.calls.append(f"play_from_queue:{index}")
        self.transport_state = "PLAYING"
        if 0 <= index < len(self.queue):
            item = self.queue[index]
            self.track_info = {
                "title": item["title"],
                "artist": item.get("artist") or "",
                "album": item.get("album") or "",
                "uri": item["uri"],
                "playlist_position": str(index + 1),
            }

    def get_queue(self, start: int = 0, max_items: int = 100) -> List[Any]:
        self._maybe_raise()
        self._require_coordinator("get_queue")
        sliced = self.queue[start : start + max_items]
        result = []
        for entry in sliced:
            obj = FakeDidlObject(entry["uri"], title=entry["title"])
            obj.creator = entry.get("artist")
            obj.album = entry.get("album")
            result.append(obj)
        return result

    def remove_from_queue(self, index: int) -> None:
        self._maybe_raise()
        self._require_coordinator("remove_from_queue")
        self.calls.append(f"remove_from_queue:{index}")
        del self.queue[index]

    def play_uri(self, uri: str = "", meta: str = "", title: str = "") -> None:
        self._maybe_raise()
        self._require_coordinator("play_uri")
        self.calls.append(f"play_uri:{uri}")
        self.played_uris.append({"uri": uri, "meta": meta, "title": title})
        self.transport_state = "PLAYING"
        self.track_info["uri"] = uri
        self.track_info["title"] = title or uri

    def switch_to_tv(self) -> None:
        self._maybe_raise()
        if not self.is_soundbar:
            raise NotSupportedException("TV input is not supported on this device")
        self.calls.append("switch_to_tv")
        self.is_playing_tv = True
        self.is_playing_line_in = False
        self.transport_state = "PLAYING"
        self.track_info = {"title": "TV", "artist": "", "uri": f"x-sonos-htastream:{self.uid}:spdif"}

    def switch_to_line_in(self) -> None:
        self._maybe_raise()
        self.calls.append("switch_to_line_in")
        self.is_playing_tv = False
        self.is_playing_line_in = True
        self.transport_state = "PLAYING"
        self.track_info = {"title": "Line-In", "artist": "", "uri": f"x-rincon-stream:{self.uid}"}

    def join(self, master: "FakeSoCo") -> None:
        self._maybe_raise()
        if not master.is_coordinator:
            raise SoCoSlaveException("Cannot join a non-coordinator")
        self.calls.append(f"join:{master.player_name}")
        if self in self.group.members and self.group is not master.group:
            self.group.members.remove(self)
        if self not in master.group.members:
            master.group.members.append(self)
        self.group = master.group

    def unjoin(self) -> None:
        self._maybe_raise()
        self.calls.append("unjoin")
        old_group = self.group
        if self in old_group.members:
            old_group.members.remove(self)
        if old_group.coordinator is self and old_group.members:
            old_group.coordinator = old_group.members[0]
        self.group = FakeGroup(self, [self])

    def ramp_to_volume(self, volume: int, ramp_type: str = "SLEEP_TIMER_RAMP_TYPE") -> int:
        self._maybe_raise()
        self.calls.append(f"ramp_to_volume:{volume}:{ramp_type}")
        self.ramps.append({"volume": volume, "ramp_type": ramp_type})
        self.volume = volume
        return 3

    def set_sleep_timer(self, sleep_time_seconds: Optional[int]) -> None:
        self._maybe_raise()
        self._require_coordinator("set_sleep_timer")
        self.calls.append(f"set_sleep_timer:{sleep_time_seconds}")
        self._sleep_timer = sleep_time_seconds

    def get_sleep_timer(self) -> Optional[int]:
        self._maybe_raise()
        return self._sleep_timer


class FakeShareLinkPlugin:
    def __init__(self, speaker: FakeSoCo):
        self.speaker = speaker

    def add_share_link_to_queue(self, uri: str, position: int = 0) -> int:
        item = FakeDidlObject(uri, title=f"Track({uri.rsplit('/', 1)[-1]})")
        return self.speaker.add_to_queue(item, position=position)


def build_fake_household() -> Dict[str, FakeSoCo]:
    """Build a realistic 3-room + 1-bonded-satellite household matching Living Room Arc + Kids Room + Bedroom."""
    favorites = [
        FakeDidlFavorite("Pink Noise", "x-sonos-spotify:spotify:track:pinknoise", queueable=True),
        FakeDidlFavorite(
            "GLGLZ",
            "x-sonosapi-stream:s25874?sid=254",
            description="Galgalatz Live Radio",
            resource_meta_data="<DIDL-Lite><item>GLGLZ</item></DIDL-Lite>",
            queueable=False,
        ),
        FakeDidlFavorite("KAN 88", "x-sonosapi-stream:s12345?sid=254", description="Kan 88 FM", queueable=False),
    ]

    living_arc = FakeSoCo(
        "192.168.1.100",
        "Living Room",
        uid="RINCON_LIVING_ARC",
        volume=25,
        state="PLAYING",
        is_visible=True,
        is_soundbar=True,
        model_name="Sonos Arc",
        favorites=favorites,
        track_info={"title": "Bohemian Rhapsody", "artist": "Queen", "album": "A Night at the Opera", "uri": "x-sonos-http:track1", "playlist_position": "1"},
    )
    living_surround = FakeSoCo(
        "192.168.1.109",
        "Living Room",
        uid="RINCON_LIVING_SURROUND_L",
        volume=25,
        is_visible=False,
        is_soundbar=False,
        model_name="Sonos Era 100",
        favorites=favorites,
    )
    kids_room = FakeSoCo(
        "192.168.1.101",
        "Kids Room",
        uid="RINCON_KIDS",
        volume=18,
        state="STOPPED",
        is_visible=True,
        is_soundbar=False,
        model_name="Sonos Era 100",
        favorites=favorites,
    )
    bedroom = FakeSoCo(
        "192.168.1.102",
        "Bedroom",
        uid="RINCON_BEDROOM",
        volume=15,
        state="PAUSED_PLAYBACK",
        is_visible=True,
        is_soundbar=False,
        model_name="Sonos Era 100",
        favorites=favorites,
    )

    all_devices = [living_arc, living_surround, kids_room, bedroom]
    for dev in all_devices:
        dev._all_zones = all_devices

    return {
        "living_room": living_arc,
        "living_surround": living_surround,
        "kids_room": kids_room,
        "bedroom": bedroom,
    }
