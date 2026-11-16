from contextlib import asynccontextmanager
import json
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import requests

from sonos_service import (
    DEFAULT_ROOM,
    SPEAKER_IPS,
    SonosServiceError,
    discover_speakers,
    execute_tool,
    get_favorite_titles,
    normalize_room_name,
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sonos_agent")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:4b")
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "45.0"))
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "4096"))
OLLAMA_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0.1"))
OLLAMA_THINK = os.getenv("OLLAMA_THINK", "false").strip().lower() in ("1", "true", "yes")

_ollama_session = requests.Session()

ROOM_SYNONYMS: Dict[str, str] = {
    "living room": "living_room",
    "living_room": "living_room",
    "סלון": "living_room",
    "בסלון": "living_room",
    "kids room": "kids_room",
    "kids_room": "kids_room",
    "חדר ילדים": "kids_room",
    "בחדר ילדים": "kids_room",
    "חדר הילדים": "kids_room",
    "bedroom": "bedroom",
    "חדר שינה": "bedroom",
    "בחדר שינה": "bedroom",
    "חדר השינה": "bedroom",
}

VALID_TOOLS = {
    "get_status",
    "play",
    "pause",
    "next",
    "previous",
    "set_volume",
    "adjust_volume",
    "group",
    "ungroup",
    "select_source",
}


def _warmup_ollama() -> None:
    """Keep the configured model resident in VRAM without blocking server startup."""
    try:
        payload = {
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": "ok"}],
            "think": OLLAMA_THINK,
            "options": {"temperature": OLLAMA_TEMPERATURE, "num_ctx": OLLAMA_NUM_CTX},
            "stream": False,
            "keep_alive": -1,
        }
        resp = _ollama_session.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
        if resp.ok:
            logger.info("Ollama model '%s' pre-warmed (keep_alive=-1, think=%s).", OLLAMA_MODEL, OLLAMA_THINK)
        else:
            logger.warning("Ollama pre-warm returned HTTP %s.", resp.status_code)
    except Exception as exc:
        logger.warning("Ollama pre-warm skipped (%s); will use fallback until ready.", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    devices = discover_speakers()
    logger.info("Startup discovered speakers: %s", sorted(devices.keys()))
    get_favorite_titles()
    threading.Thread(target=_warmup_ollama, name="ollama-warmup", daemon=True).start()
    yield


app = FastAPI(title="Sonos Voice Agent", version="2.1.0", lifespan=lifespan)


def _known_rooms() -> List[str]:
    rooms = set(SPEAKER_IPS.keys()) | set(discover_speakers().keys()) | {DEFAULT_ROOM}
    return sorted(r for r in rooms if r)


def _normalize_room_from_text(raw_room: Optional[str]) -> str:
    if not raw_room or not str(raw_room).strip():
        return DEFAULT_ROOM
    cleaned = str(raw_room).strip().lower()
    if cleaned in ROOM_SYNONYMS:
        return ROOM_SYNONYMS[cleaned]
    return normalize_room_name(cleaned)


def _detect_room_in_command(cmd_lower: str) -> str:
    for phrase, canonical in sorted(ROOM_SYNONYMS.items(), key=lambda kv: len(kv[0]), reverse=True):
        if phrase in cmd_lower:
            return canonical
    for room in _known_rooms():
        readable = room.replace("_", " ")
        if room in cmd_lower or readable in cmd_lower:
            return room
    return DEFAULT_ROOM


def fallback_keyword_parse(command: str) -> Dict[str, Any]:
    cmd = command.lower().strip()
    room = _detect_room_in_command(cmd)

    if any(k in cmd for k in ("pause", "stop", "עצור", "תעצור", "הפסק", "תפסיק")):
        return {"tool": "pause", "room": room}

    if any(k in cmd for k in ("next", "skip", "הבא", "שיר הבא", "דלג")):
        return {"tool": "next", "room": room}

    if any(k in cmd for k in ("previous", "prev", "הקודם", "שיר קודם")):
        return {"tool": "previous", "room": room}

    if any(k in cmd for k in ("status", "what's playing", "whats playing", "מצב", "מה מתנגן")):
        return {"tool": "get_status", "room": room}

    if any(k in cmd for k in ("ungroup", "unjoin", "תפריד", "הפרד")):
        return {"tool": "ungroup", "room": room}

    if any(k in cmd for k in ("louder", "volume up", "turn up", "תגביר", "הגבר")):
        return {"tool": "adjust_volume", "room": room, "delta": 10}

    if any(k in cmd for k in ("quieter", "softer", "volume down", "turn down", "תנמיך", "הנמך")):
        return {"tool": "adjust_volume", "room": room, "delta": -10}

    if any(k in cmd for k in ("volume", "volent", "ווליום", "עוצמה")):
        nums = re.findall(r"\d+", cmd)
        vol = int(nums[0]) if nums else 20
        return {"tool": "set_volume", "room": room, "volume": vol}

    if any(k in cmd for k in ("tv", "hdmi", "טלוויזיה")):
        return {"tool": "select_source", "room": room, "source": "tv"}

    if any(k in cmd for k in ("group", "join", "חבר", "תאחד")):
        others = [r for r in _known_rooms() if r != room]
        target = others[0] if others else DEFAULT_ROOM
        return {"tool": "group", "room": room, "target_room": target}

    # Strip common play verbs and room phrases to isolate the requested music/station.
    cleaned = command.strip()
    for token in (
        "living room", "living_room", "kids room", "kids_room", "bedroom",
        "בסלון", "סלון", "בחדר ילדים", "חדר ילדים", "חדר הילדים",
        "בחדר שינה", "חדר שינה", "חדר השינה",
        "play", "resume", "in the", "in", "on", "תנגן", "נגן", "שים", "את", "ב",
    ):
        cleaned = re.sub(rf"(?<!\w){re.escape(token)}(?!\w)", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if not cleaned:
        return {"tool": "play", "room": room, "source": "resume", "query": ""}
    return {"tool": "play", "room": room, "source": "auto", "query": cleaned}


def _sanitize_llm_action(parsed: Dict[str, Any], raw_command: str) -> Dict[str, Any]:
    if not isinstance(parsed, dict):
        return fallback_keyword_parse(raw_command)

    tool = str(parsed.get("tool") or "").strip().lower()
    if tool not in VALID_TOOLS:
        return fallback_keyword_parse(raw_command)

    sanitized: Dict[str, Any] = dict(parsed)
    sanitized["tool"] = tool
    sanitized["room"] = _normalize_room_from_text(parsed.get("room"))

    if parsed.get("target_room") is not None:
        sanitized["target_room"] = _normalize_room_from_text(parsed.get("target_room"))

    if sanitized.get("query") is None:
        sanitized["query"] = ""
    if sanitized.get("source") is None:
        sanitized["source"] = "auto"

    return sanitized


def _build_system_prompt(rooms: List[str], favorites: List[str]) -> str:
    rooms_str = ", ".join(rooms)
    fav_list = ", ".join(favorites) if favorites else "Pink Noise, GLGLZ, KAN 88"
    return (
        "You translate voice commands in English or Hebrew into Sonos JSON actions. "
        f"Valid rooms: [{rooms_str}] "
        "(aliases: living room / סלון -> living_room, kids room / חדר ילדים -> kids_room, bedroom / חדר שינה -> bedroom). "
        f"Default room is {DEFAULT_ROOM}. "
        "Valid tools: get_status, play, pause, next, previous, set_volume, adjust_volume, group, ungroup, select_source. "
        f"Saved Sonos favorites and radio stations: [{fav_list}]. "
        "Rules for play: "
        "1. If the user asks for white noise or רעש לבן, set source to favorite and query to Pink Noise. "
        "2. If the user asks for Galgalatz or גלגלצ, set source to favorite and query to GLGLZ. "
        "3. If the user asks for any radio station or saved favorite, match the closest name from saved favorites and set source to favorite. "
        "4. If the user asks for an artist, band, song, or album, set source to apple_music and query to the artist or song name. "
        "5. If the user only says play or resume without naming content, set source to resume and query to empty string. "
        "6. For relative volume (louder/quieter/תגביר/תנמיך), use adjust_volume with positive or negative delta (e.g. 10 or -10). "
        "7. For grouping rooms, use group with room and target_room. For TV/Line-In input, use select_source with source tv or line_in. "
        "Reply ONLY with valid JSON matching: "
        '{"tool": "get_status|play|pause|next|previous|set_volume|adjust_volume|group|ungroup|select_source", '
        '"room": "<valid_room>", "target_room": "", "volume": 15, "delta": 0, '
        '"source": "favorite|apple_music|resume|tv|line_in", "query": ""}'
    )


def _check_ollama_status() -> Dict[str, Any]:
    tags_url = OLLAMA_URL.replace("/api/chat", "/api/tags")
    try:
        resp = _ollama_session.get(tags_url, timeout=3)
        resp.raise_for_status()
        models = resp.json().get("models", [])
        installed = [m.get("name", "") for m in models if isinstance(m, dict)]
        base_target = OLLAMA_MODEL.split(":")[0]
        model_available = any(
            name == OLLAMA_MODEL
            or name == f"{OLLAMA_MODEL}:latest"
            or (":" not in OLLAMA_MODEL and name.split(":")[0] == base_target)
            for name in installed
        )
        return {
            "ollama_reachable": True,
            "model_available": model_available,
            "installed_models": installed,
        }
    except Exception as exc:
        logger.debug("Ollama health check failed: %s", exc)
        return {
            "ollama_reachable": False,
            "model_available": False,
            "installed_models": [],
        }


@app.get("/health")
def health_check(strict: bool = Query(default=False)):
    ollama_info = _check_ollama_status()
    devices = discover_speakers()
    speakers = sorted(devices.keys())

    issues: List[str] = []
    if not speakers:
        issues.append("No Sonos speakers discovered or configured.")
    if not ollama_info["ollama_reachable"]:
        issues.append("Ollama is unreachable; keyword fallback mode active.")
    elif not ollama_info["model_available"]:
        issues.append(f"Model '{OLLAMA_MODEL}' is not pulled in Ollama.")

    status = "healthy" if not issues else "degraded"
    http_code = 200
    if not speakers or (strict and issues):
        http_code = 503

    return JSONResponse(
        status_code=http_code,
        content={
            "status": status,
            "ollama_reachable": ollama_info["ollama_reachable"],
            "model": OLLAMA_MODEL,
            "model_available": ollama_info["model_available"],
            "think_disabled": not OLLAMA_THINK,
            "discovered_speakers": speakers,
            "issues": issues,
        },
    )


class CommandRequest(BaseModel):
    command: str


@app.post("/command")
def handle_command(req: CommandRequest):
    cmd_text = (req.command or "").strip()
    if not cmd_text:
        return {"reply": "Please provide a voice command.", "ok": False, "model_used": "validation"}

    logger.info("Received command: %s", cmd_text)
    model_used = OLLAMA_MODEL

    try:
        rooms = _known_rooms()
        favorites = get_favorite_titles()
        system_prompt = _build_system_prompt(rooms, favorites)

        payload = {
            "model": OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": cmd_text},
            ],
            "think": OLLAMA_THINK,
            "format": "json",
            "options": {
                "temperature": OLLAMA_TEMPERATURE,
                "num_ctx": OLLAMA_NUM_CTX,
            },
            "stream": False,
            "keep_alive": -1,
        }

        res = _ollama_session.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
        res.raise_for_status()
        content = res.json().get("message", {}).get("content", "{}")
        logger.info("LLM JSON: %s", content)
        parsed = json.loads(content)
        action = _sanitize_llm_action(parsed, cmd_text)
    except Exception as exc:
        logger.warning("LLM translation fallback triggered (%r); using keyword parser.", exc)
        action = fallback_keyword_parse(cmd_text)
        model_used = "keyword-fallback"

    try:
        result = execute_tool(action["tool"], action)
        return {"reply": result, "ok": True, "model_used": model_used}
    except SonosServiceError as exc:
        logger.error("Sonos error executing %s: %s", action.get("tool"), exc)
        return {"reply": str(exc), "ok": False, "model_used": model_used}
    except Exception as exc:
        logger.exception("Unexpected error executing %s", action.get("tool"))
        return {"reply": f"Sorry, Sonos command failed: {exc}", "ok": False, "model_used": model_used}

