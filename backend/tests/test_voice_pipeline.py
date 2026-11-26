import asyncio
import json
import logging
from datetime import datetime
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


def test_pipecat_voice_confirmation_routes_through_booking_service(client, monkeypatch):
    pytest.importorskip("pipecat")
    from app.agent.voice_confirmation import VoiceBookingSession, VoiceBookingStatus
    from app.agent.tools import book_appointment
    from app.models import Appointment
    from bot import (
        VoiceBookingConfirmationGate,
        VoiceBookingResponseProcessor,
        register_appointment_tools,
    )
    from pipecat.frames.frames import (
        LLMContextFrame,
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

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2099, 1, 1, 12, 0)

    monkeypatch.setattr("app.agent.tools.datetime", FixedDateTime)

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
        {"speciality": "cardiologist frio", "date": "tommorrow"},
    )
    assert availability["success"] is True
    assert availability["date"] == "2099-01-02"
    assert availability["doctors"][0]["speciality"] == "Cardiology"
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
    assert "confirmation_id" not in proposal["booking"]
    assert state.status == VoiceBookingStatus.AWAITING_CONFIRMATION
    assert "prepare_appointment_booking" in llm.functions
    assert "book_appointment" not in llm.functions

    db = client.app.state.test_session()
    assert db.query(Appointment).count() == 0
    db.close()

    response_processor = VoiceBookingResponseProcessor(state)
    forwarded_frames = []

    async def capture_frame(frame, direction=FrameDirection.DOWNSTREAM):
        forwarded_frames.append(frame)

    response_processor.push_frame = capture_frame
    context.add_message({
        "role": "assistant",
        "content": proposal["booking"]["summary"],
    })
    asyncio.run(response_processor.process_frame(
        LLMFullResponseStartFrame(),
        FrameDirection.DOWNSTREAM,
    ))
    asyncio.run(response_processor.process_frame(
        LLMTextFrame(text=proposal["booking"]["summary"]),
        FrameDirection.DOWNSTREAM,
    ))
    asyncio.run(response_processor.process_frame(
        LLMFullResponseEndFrame(),
        FrameDirection.DOWNSTREAM,
    ))

    gate = VoiceBookingConfirmationGate(context, state, llm)
    gate_forwarded_frames = []

    async def capture_gate_frame(frame, direction=FrameDirection.DOWNSTREAM):
        gate_forwarded_frames.append(frame)

    gate.push_frame = capture_gate_frame
    context.add_message({"role": "user", "content": "Where?"})
    asyncio.run(gate.process_frame(
        LLMContextFrame(context),
        FrameDirection.DOWNSTREAM,
    ))
    assert state.status == VoiceBookingStatus.AWAITING_CONFIRMATION
    assert any(isinstance(frame, LLMContextFrame) for frame in gate_forwarded_frames)
    db = client.app.state.test_session()
    assert db.query(Appointment).count() == 0
    db.close()

    context.add_message({"role": "user", "content": "Yes, book it. Where?"})
    llm.frames.clear()
    asyncio.run(gate.process_frame(
        LLMContextFrame(context),
        FrameDirection.DOWNSTREAM,
    ))

    assert state.status == VoiceBookingStatus.BOOKED
    assert len(llm.frames) == 3
    async def send_direct_confirmation_response():
        for frame in llm.frames:
            await response_processor.process_frame(frame, FrameDirection.DOWNSTREAM)

    asyncio.run(send_direct_confirmation_response())
    assert any(
        isinstance(frame, LLMTextFrame) and "Your booking ID is" in frame.text
        for frame in forwarded_frames
    )
    db = client.app.state.test_session()
    booking = db.query(Appointment).one()
    appointment_id = booking.id
    db.close()

    context.add_message({"role": "user", "content": "Absolutely."})
    llm.frames.clear()
    asyncio.run(gate.process_frame(
        LLMContextFrame(context),
        FrameDirection.DOWNSTREAM,
    ))
    assert state.status == VoiceBookingStatus.BOOKED
    assert len(llm.frames) == 3
    db = client.app.state.test_session()
    assert db.query(Appointment).count() == 1
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


def test_voice_output_does_not_claim_an_unrecorded_booking():
    pytest.importorskip("pipecat")
    from app.agent.voice_confirmation import VoiceBookingSession
    from bot import VoiceBookingResponseProcessor
    from pipecat.frames.frames import (
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
    )
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.frame_processor import FrameDirection

    state = VoiceBookingSession(lambda **_: {"success": False})
    processor = VoiceBookingResponseProcessor(state)
    output_frames = []

    async def capture_frame(frame, direction=FrameDirection.DOWNSTREAM):
        output_frames.append(frame)

    processor.push_frame = capture_frame

    async def send_false_success():
        await processor.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
        await processor.process_frame(
            LLMTextFrame(text="Your appointment is booked. Your confirmation ID is random."),
            FrameDirection.DOWNSTREAM,
        )
        await processor.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    asyncio.run(send_false_success())
    visible_text = "".join(
        frame.text for frame in output_frames if isinstance(frame, LLMTextFrame)
    )
    assert "couldn't verify a completed appointment" in visible_text
    assert "random" not in visible_text
