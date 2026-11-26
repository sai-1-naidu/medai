from html import escape
import json
import logging

import httpx
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import (
    DASHBOARD_PROBE_HOST,
    DASHBOARD_PORT,
    GROQ_API_KEY,
    PIPECAT_CLIENT_URL,
    PIPECAT_PROBE_URL,
)
from app.provider_errors import log_provider_http_error, safe_provider_detail

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/voice", tags=["voice"])
MAX_AUDIO_BYTES = 12 * 1024 * 1024
PIPECAT_PROBE_TIMEOUT_SECONDS = 1.5


async def pipecat_client_is_available() -> bool:
    try:
        async with httpx.AsyncClient(timeout=PIPECAT_PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(PIPECAT_PROBE_URL)
    except httpx.RequestError as exc:
        logger.info("Pipecat client probe failed (%s).", type(exc).__name__)
        return False
    if response.status_code != 200:
        logger.warning("Pipecat client probe returned HTTP %s.", response.status_code)
    return response.status_code == 200


@router.get("/realtime-health")
async def realtime_voice_health():
    if not await pipecat_client_is_available():
        raise HTTPException(
            status_code=503,
            detail=(
                "The Pipecat voice service is not running. From the backend folder, "
                "run .\\start_services.ps1 or start the Pipecat runner in a second terminal."
            ),
        )
    return {"status": "ready", "client_url": PIPECAT_CLIENT_URL}


@router.get("/realtime-client", include_in_schema=False)
async def open_realtime_voice_client():
    if await pipecat_client_is_available():
        return RedirectResponse(PIPECAT_CLIENT_URL, status_code=307)

    dashboard_url = f"http://{DASHBOARD_PROBE_HOST}:{DASHBOARD_PORT}"
    client_url = escape(PIPECAT_CLIENT_URL)
    safe_dashboard_url = escape(dashboard_url, quote=True)
    return HTMLResponse(
        status_code=503,
        content=(
            "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>Real-time voice is unavailable</title>"
            "<body style=\"font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px;color:#183b35\">"
            "<h1>Real-time voice service is not running</h1>"
            "<p>The dashboard is available, but its separate Pipecat WebRTC runner "
            f"did not respond at <code>{client_url}</code>.</p>"
            "<p>From <code>D:\\medivoice-ai\\backend</code>, run "
            "<code> .\\start_services.ps1 </code>, or start the Pipecat runner in "
            "a second terminal. Keep both services running while you use the call.</p>"
            f"<p><a href=\"{safe_dashboard_url}\">Return to the dashboard</a></p>"
            "</body></html>"
        ),
    )


@router.post("/transcribe")
async def transcribe_audio(audio: UploadFile = File(...)):
    if not GROQ_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Speech transcription is not configured. Add GROQ_API_KEY to backend/.env.",
        )

    content = await audio.read(MAX_AUDIO_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail="The recording is empty.")
    if len(content) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="The recording exceeds the 12 MB limit.")

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                data={"model": "whisper-large-v3-turbo"},
                files={
                    "file": (
                        audio.filename or "recording.webm",
                        content,
                        audio.content_type or "audio/webm",
                    )
                },
            )
            response.raise_for_status()
            try:
                result = response.json()
            except json.JSONDecodeError as exc:
                logger.warning("Groq returned invalid transcription JSON.")
                raise HTTPException(
                    status_code=502,
                    detail="The speech provider returned an invalid response.",
                ) from exc
    except httpx.TimeoutException as exc:
        logger.warning(
            "Audio transcription timed out (%s): %s",
            type(exc).__name__,
            safe_provider_detail(str(exc)),
        )
        raise HTTPException(status_code=504, detail="Transcription timed out. Please try again.") from exc
    except httpx.HTTPStatusError as exc:
        log_provider_http_error(logger, "Groq transcription", exc.response)
        if exc.response.status_code == 429:
            retry_after = exc.response.headers.get("Retry-After")
            headers = {"Retry-After": retry_after} if retry_after and retry_after.isdigit() else None
            raise HTTPException(
                status_code=503,
                detail="Speech transcription is rate-limited. Please try again shortly.",
                headers=headers,
            ) from exc
        raise HTTPException(status_code=502, detail="The speech provider could not transcribe this recording.") from exc
    except httpx.RequestError as exc:
        logger.warning(
            "Audio transcription connection failed (%s): %s",
            type(exc).__name__,
            safe_provider_detail(str(exc)),
        )
        raise HTTPException(status_code=502, detail="Speech transcription is temporarily unavailable.") from exc

    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="The speech provider returned an invalid response.")
    text = result.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=502, detail="No speech was detected in the recording.")
    return {"text": text.strip()}
