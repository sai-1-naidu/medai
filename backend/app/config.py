import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BACKEND_DIR / ".env")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = (
    os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
    or "openai/gpt-oss-120b"
)
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = (
    os.getenv("ELEVENLABS_VOICE_ID", "").strip()
    or "JBFqnCBsd6RMkjVDRZzb"
)

DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
PIPECAT_HOST = os.getenv("PIPECAT_HOST", "127.0.0.1").strip() or "127.0.0.1"
PIPECAT_CLIENT_HOST = (
    os.getenv("PIPECAT_CLIENT_HOST", "127.0.0.1").strip() or "127.0.0.1"
)


def _read_port(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        port = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer between 1 and 65535.") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError(f"{name} must be an integer between 1 and 65535.")
    return port


DASHBOARD_PORT = _read_port("DASHBOARD_PORT", 8000)
PIPECAT_PORT = _read_port("PIPECAT_PORT", 7860)
PIPECAT_CLIENT_PATH = os.getenv("PIPECAT_CLIENT_PATH", "/client/").strip() or "/client/"
if not PIPECAT_CLIENT_PATH.startswith("/"):
    PIPECAT_CLIENT_PATH = f"/{PIPECAT_CLIENT_PATH}"
if not PIPECAT_CLIENT_PATH.endswith("/"):
    PIPECAT_CLIENT_PATH = f"{PIPECAT_CLIENT_PATH}/"


def _format_url_host(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


PIPECAT_CLIENT_URL = (
    f"http://{_format_url_host(PIPECAT_CLIENT_HOST)}:{PIPECAT_PORT}"
    f"{PIPECAT_CLIENT_PATH}"
)
PIPECAT_PROBE_HOST = (
    "127.0.0.1" if PIPECAT_HOST in {"0.0.0.0", "::"} else PIPECAT_HOST
)
PIPECAT_PROBE_URL = (
    f"http://{_format_url_host(PIPECAT_PROBE_HOST)}:{PIPECAT_PORT}"
    f"{PIPECAT_CLIENT_PATH}"
)
DASHBOARD_PROBE_HOST = (
    "127.0.0.1" if DASHBOARD_HOST in {"0.0.0.0", "::"} else DASHBOARD_HOST
)


def validate_api_keys() -> None:
    missing = [
        name
        for name, value in (
            ("GROQ_API_KEY", GROQ_API_KEY),
            ("ELEVENLABS_API_KEY", ELEVENLABS_API_KEY),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")
