import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from app.routes import voice


def test_realtime_voice_health_reports_ready(client, monkeypatch):
    async def available():
        return True

    monkeypatch.setattr(voice, "pipecat_client_is_available", available)

    response = client.get("/voice/realtime-health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "client_url": voice.PIPECAT_CLIENT_URL,
    }


def test_realtime_voice_health_reports_runner_unavailable(client, monkeypatch):
    async def unavailable():
        return False

    monkeypatch.setattr(voice, "pipecat_client_is_available", unavailable)

    response = client.get("/voice/realtime-health")

    assert response.status_code == 503
    assert "start_services.ps1" in response.json()["detail"]


def test_realtime_client_redirects_only_when_runner_is_available(client, monkeypatch):
    async def available():
        return True

    monkeypatch.setattr(voice, "pipecat_client_is_available", available)

    response = client.get("/voice/realtime-client", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == voice.PIPECAT_CLIENT_URL


def test_realtime_client_shows_startup_instructions_when_runner_is_missing(client, monkeypatch):
    async def unavailable():
        return False

    monkeypatch.setattr(voice, "pipecat_client_is_available", unavailable)

    response = client.get("/voice/realtime-client")

    assert response.status_code == 503
    assert "Real-time voice service is not running" in response.text
    assert "start_services.ps1" in response.text
    assert "ERR_CONNECTION_REFUSED" not in response.text


class StubResponse:
    def __init__(self, data, status_code=200, headers=None):
        self.data = data
        self.response = httpx.Response(
            status_code,
            json=data,
            headers=headers,
            request=httpx.Request("POST", "https://api.groq.com/openai/v1/audio/transcriptions"),
        )

    def raise_for_status(self):
        self.response.raise_for_status()

    def json(self):
        return self.data


class StubAsyncClient:
    def __init__(self, response):
        self.response = response
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None

    async def post(self, url, **kwargs):
        self.requests.append({"url": url, **kwargs})
        return self.response


def test_recorded_audio_transcription_uses_groq_and_returns_text(client, monkeypatch):
    provider = StubAsyncClient(StubResponse({"text": "Find a cardiologist."}))
    monkeypatch.setattr(voice, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **kwargs: provider)

    response = client.post(
        "/voice/transcribe",
        files={"audio": ("sample.webm", b"audio bytes", "audio/webm")},
    )

    assert response.status_code == 200
    assert response.json() == {"text": "Find a cardiologist."}
    request = provider.requests[0]
    assert request["data"]["model"] == "whisper-large-v3-turbo"
    assert request["files"]["file"][1] == b"audio bytes"


@pytest.mark.parametrize(("status_code", "expected_status"), [(401, 502), (429, 503)])
def test_transcription_provider_errors_are_safe(
    client, monkeypatch, caplog, status_code, expected_status
):
    provider = StubAsyncClient(
        StubResponse(
            {"error": {"message": "Rejected gsk_secret123."}},
            status_code=status_code,
            headers={"Retry-After": "9"},
        )
    )
    monkeypatch.setattr(voice, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **kwargs: provider)

    with caplog.at_level(logging.WARNING):
        response = client.post(
            "/voice/transcribe",
            files={"audio": ("sample.webm", b"audio bytes", "audio/webm")},
        )

    assert response.status_code == expected_status
    assert "gsk_secret123" not in caplog.text
    assert "gsk_secret123" not in response.text
    if status_code == 429:
        assert response.headers["retry-after"] == "9"


def test_transcription_rejects_empty_and_oversized_recordings(client):
    empty = client.post(
        "/voice/transcribe",
        files={"audio": ("empty.webm", b"", "audio/webm")},
    )
    oversized = client.post(
        "/voice/transcribe",
        files={"audio": ("large.webm", b"x" * (voice.MAX_AUDIO_BYTES + 1), "audio/webm")},
    )

    assert empty.status_code == 400
    assert oversized.status_code == 413


def test_pipecat_voice_confirmation_routes_through_booking_service(client):
    pytest.importorskip("pipecat")
    from app.agent.voice_confirmation import VoiceBookingSession, VoiceBookingStatus
    from app.agent.tools import book_appointment
    from app.models import Appointment
    from bot import VoiceBookingResponseProcessor, register_appointment_tools
    from pipecat.frames.frames import (
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
    )
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.frame_processor import FrameDirection

    class StubLLM:
        def __init__(self):
            self.functions = {}
            self.frames = []

        def register_function(self, name, handler):
            self.functions[name] = handler

        async def push_frame(self, frame):
            self.frames.append(frame)

    context = LLMContext()
    llm = StubLLM()
    state = VoiceBookingSession(book_appointment)
    register_appointment_tools(llm, context, state)

    def invoke(name, arguments):
        results = []

        async def result_callback(result, *, properties=None):
            results.append(result)
            if properties:
                llm.result_properties = properties

        params = SimpleNamespace(
            function_name=name,
            arguments=arguments,
            context=context,
            llm=llm,
            result_callback=result_callback,
        )
        asyncio.run(llm.functions[name](params))
        return results[0]

    availability = invoke(
        "check_doctor_availability",
        {"speciality": "Cardiology", "date": "2099-01-02"},
    )
    assert availability["success"] is True
    assert context.tools.standard_tools

    proposal = invoke(
        "prepare_appointment_booking",
        {
        "doctor_id": 1,
        "slot_id": 1,
        "patient_phone": "15552345678",
        },
    )
    assert proposal["success"] is True
    assert proposal["booking"]["doctor_name"] == "Dr. Test"
    assert proposal["booking"]["speciality"] == "Cardiology"
    assert proposal["booking"]["date"] == "2099-01-02"
    assert proposal["booking"]["slot_id"] == 1
    assert proposal["booking"]["patient_phone"] == "15552345678"
    assert state.status == VoiceBookingStatus.AWAITING_CONFIRMATION
    assert "prepare_appointment_booking" in llm.functions
    assert "book_appointment" not in llm.functions

    db = client.app.state.test_session()
    assert db.query(Appointment).count() == 0
    db.close()

    async def arm_readback():
        return await state.mark_readback_complete(proposal["booking"]["summary"])

    assert asyncio.run(arm_readback()) is True
    response_processor = VoiceBookingResponseProcessor(context, state)
    forwarded_frames = []

    async def capture_frame(frame, direction=FrameDirection.DOWNSTREAM):
        forwarded_frames.append(frame)

    response_processor.push_frame = capture_frame
    context.add_message({
        "role": "assistant",
        "content": proposal["booking"]["summary"],
    })
    context.add_message({"role": "user", "content": "Where?"})
    asyncio.run(
        response_processor.process_frame(
            LLMFullResponseStartFrame(),
            FrameDirection.DOWNSTREAM,
        )
    )
    assert state.status == VoiceBookingStatus.AWAITING_CONFIRMATION
    assert any(isinstance(frame, LLMFullResponseStartFrame) for frame in forwarded_frames)
    db = client.app.state.test_session()
    assert db.query(Appointment).count() == 0
    db.close()

    context.add_message({"role": "user", "content": "Yes, book it. Where?"})
    asyncio.run(
        response_processor.process_frame(
            LLMFullResponseStartFrame(),
            FrameDirection.DOWNSTREAM,
        )
    )
    asyncio.run(
        response_processor.process_frame(
            LLMTextFrame(text="Please confirm by replying with exactly yes book it."),
            FrameDirection.DOWNSTREAM,
        )
    )
    asyncio.run(
        response_processor.process_frame(
            LLMFullResponseEndFrame(),
            FrameDirection.DOWNSTREAM,
        )
    )

    assert state.status == VoiceBookingStatus.BOOKED
    assert any(
        isinstance(frame, LLMTextFrame) and "Your booking ID is" in frame.text
        for frame in forwarded_frames
    )
    assert all(
        not isinstance(frame, LLMTextFrame)
        or "exactly yes book it" not in frame.text
        for frame in forwarded_frames
    )
    db = client.app.state.test_session()
    booking = db.query(Appointment).one()
    appointment_id = booking.id
    db.close()

    cancellation_arguments = {
        "appointment_id": appointment_id,
        "patient_phone": "15552345678",
    }
    context.add_message({
        "role": "assistant",
        "content": (
            f"Cancel appointment {appointment_id} using phone 15552345678. "
            "Should I cancel it?"
        ),
    })
    context.add_message({"role": "user", "content": "Yes, cancel it."})
    cancellation = invoke("cancel_appointment", cancellation_arguments)
    assert cancellation["success"] is True
    assert cancellation["status"] == "cancelled"
