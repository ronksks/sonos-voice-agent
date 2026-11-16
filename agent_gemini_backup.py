import os
import socket
from fastapi import FastAPI
from pydantic import BaseModel
from google import genai
from google.genai import types
from soco import SoCo, config

config.HTTP_TIMEOUT = 3.0
socket.setdefaulttimeout(3.0)

app = FastAPI()

SPEAKERS = {
    "living_room": SoCo("192.168.68.54"),
    "kids_room": SoCo("192.168.68.55"),
    "bedroom": SoCo("192.168.68.57")
}

def get_status(room: str) -> str:
    """Check the current status and volume of a room."""
    print(f"--> [SoCo] Checking status for {room}...", flush=True)
    speaker = SPEAKERS.get(room)
    if not speaker:
        return f"Room {room} not found."
    try:
        info = speaker.get_current_transport_info()
        state = info.get("current_transport_state", "UNKNOWN")
        vol = speaker.volume
        print(f"--> [SoCo] Success: state={state}, vol={vol}", flush=True)
        return f"Room {room}: state={state}, volume={vol}"
    except Exception as e:
        print(f"--> [SoCo] Error/Timeout on {room}: {e}", flush=True)
        return f"Speaker {room} is offline or unreachable: {str(e)}"

def play(room: str) -> str:
    """Play music in a room."""
    print(f"--> [SoCo] Playing in {room}...", flush=True)
    speaker = SPEAKERS.get(room)
    if not speaker:
        return f"Room {room} not found."
    try:
        speaker.play()
        return f"Playing in {room}"
    except Exception as e:
        return f"Speaker {room} is offline or unreachable: {str(e)}"

def pause(room: str) -> str:
    """Pause music in a room."""
    print(f"--> [SoCo] Pausing in {room}...", flush=True)
    speaker = SPEAKERS.get(room)
    if not speaker:
        return f"Room {room} not found."
    try:
        speaker.pause()
        return f"Paused in {room}"
    except Exception as e:
        return f"Speaker {room} is offline or unreachable: {str(e)}"

def set_volume(room: str, volume: int) -> str:
    """Set the volume in a room."""
    print(f"--> [SoCo] Setting volume to {volume} in {room}...", flush=True)
    speaker = SPEAKERS.get(room)
    if not speaker:
        return f"Room {room} not found."
    try:
        speaker.volume = int(volume)
        return f"Volume set to {volume} in {room}"
    except Exception as e:
        return f"Speaker {room} is offline or unreachable: {str(e)}"

tools = [get_status, play, pause, set_volume]

client = genai.Client(
    api_key=os.environ.get("GEMINI_API_KEY"),
    http_options=types.HttpOptions(timeout=15000)
)

AVAILABLE_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash"
]

class CommandRequest(BaseModel):
    command: str

@app.post("/command")
def handle_command(req: CommandRequest):
    print(f"==> Received command: {req.command}", flush=True)
    last_error = ""
    for model_name in AVAILABLE_MODELS:
        try:
            print(f"==> Trying Gemini model: {model_name}...", flush=True)
            chat = client.chats.create(
                model=model_name,
                config=types.GenerateContentConfig(
                    tools=tools,
                    system_instruction="אתה עוזר בית חכם עבור רמקולים של סונוס. החדרים הם living_room (סלון), kids_room (חדר ילדים), ו-bedroom (חדר שינה). עליך להפעיל את הכלים המתאימים בלבד ולאחר מכן לענות במשפט קצר בעברית המאשר את ביצוע הפעולה או מספק את המידע. אם קיבלת שגיאת Timeout או שהרמקול לא זמין, ציין שהרמקול אינו זמין כרגע ואל תנסה לקרוא לכלי שוב."
                )
            )
            response = chat.send_message(req.command)
            print(f"==> Success with model: {model_name}", flush=True)
            return {"model_used": model_name, "reply": response.text}
        except Exception as e:
            print(f"==> Model {model_name} failed: {e}", flush=True)
            last_error = f"{model_name}: {str(e)}"
            continue
    return {"reply": f"All models failed. Last error: {last_error}"}