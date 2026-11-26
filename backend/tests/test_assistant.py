import json
import logging

import httpx
import pytest
from app.models import Appointment
from app.routes import chat, voice


class StubResponse:
    def __init__(self, data, status_code=200, headers=None):
        self.data = data
        self.response = httpx.Response(
            status_code,
            json=data,
            headers=headers,
            request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"),
        )

    def raise_for_status(self):
        self.response.raise_for_status()

    def json(self):
        return self.data


class StubAsyncClient:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None

    async def post(self, url, **kwargs):
        self.requests.append({"url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_assistant_tools_share_availability_booking_and_cancellation(client):
    availability = chat._run_tool(
        "check_doctor_availability",
        {"speciality": "Cardiology", "date": "2099-01-02"},
    )
    assert availability["success"] is True
    slot_id = availability["doctors"][0]["available_slots"][0]["slot_id"]

    booking = chat._run_tool(
        "book_appointment",
        {"doctor_id": 1, "slot_id": slot_id, "patient_phone": "15552345678"},
    )
    assert booking["success"] is True

    cancellation = chat._run_tool(
        "cancel_appointment",
        {
            "appointment_id": booking["appointment_id"],
            "patient_phone": "15552345678",
        },
    )
    assert cancellation["success"] is True
    assert cancellation["status"] == "cancelled"


def booking_provider():
    return StubAsyncClient([
        StubResponse({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "booking-call",
                        "type": "function",
                        "function": {
                            "name": "book_appointment",
                            "arguments": json.dumps({
                                "doctor_id": 1,
                                "slot_id": 1,
                                "patient_phone": "15552345678",
                            }),
                        },
                    }],
                },
            }],
        }),
    ])


@pytest.mark.parametrize(
    "confirmation",
    ["yes", "Yes, book it", "confirm", "go ahead", "proceed with booking", "that's correct"],
)
def test_booking_proposal_waits_for_natural_confirmation(
    client, monkeypatch, confirmation
):
    provider = booking_provider()
    monkeypatch.setattr(chat, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(chat.httpx, "AsyncClient", lambda **kwargs: provider)

    proposal = client.post(
        "/agent/chat",
        json={"messages": [{"role": "user", "content": "Book this appointment."}]},
    )

    assert proposal.status_code == 200
    proposal_data = proposal.json()
    assert "Dr. Test" in proposal_data["reply"]
    assert "Cardiology" in proposal_data["reply"]
    assert "January 2, 2099 at 9:00 AM" in proposal_data["reply"]
    assert "15552345678" in proposal_data["reply"]
    assert proposal_data["actions"] == []
    assert len(proposal_data["pending_booking_id"]) >= 20
    assert len(provider.requests) == 1
    assert provider.requests[0]["json"]["tools"]
    assert provider.requests[0]["json"]["model"] == chat.GROQ_MODEL

    db = client.app.state.test_session()
    assert db.query(Appointment).count() == 0
    db.close()

    confirmed = client.post(
        "/agent/chat",
        json={
            "messages": [
                {"role": "assistant", "content": proposal_data["reply"]},
                {"role": "user", "content": confirmation},
            ],
            "pending_booking_id": proposal_data["pending_booking_id"],
        },
    )

    assert confirmed.status_code == 200
    data = confirmed.json()
    assert "You're booked with Dr. Test" in data["reply"]
    assert data["actions"][0]["name"] == "book_appointment"
    assert data["actions"][0]["result"]["success"] is True
    assert data["actions"][0]["result"]["appointment_id"] == 1
    assert data["pending_booking_id"] is None


def test_invalid_booking_proposal_returns_empty_confirmation_state(client, monkeypatch):
    provider = StubAsyncClient([
        StubResponse({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "invalid-booking-call",
                        "type": "function",
                        "function": {
                            "name": "book_appointment",
                            "arguments": json.dumps({
                                "doctor_id": -1,
                                "slot_id": 1,
                                "patient_phone": "15552345678",
                            }),
                        },
                    }],
                },
            }],
        }),
    ])
    monkeypatch.setattr(chat, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(chat.httpx, "AsyncClient", lambda **kwargs: provider)

    response = client.post(
        "/agent/chat",
        json={"messages": [{"role": "user", "content": "Book this appointment."}]},
    )

    assert response.status_code == 200
    assert response.json()["pending_booking_id"] is None
    assert response.json()["actions"] == []
    db = client.app.state.test_session()
    assert db.query(Appointment).count() == 0
    db.close()


@pytest.mark.parametrize("rejection", ["no", "cancel", "not yet", "change the time"])
def test_booking_rejection_clears_confirmation_without_booking(
    client, monkeypatch, rejection
):
    provider = booking_provider()
    monkeypatch.setattr(chat, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(chat.httpx, "AsyncClient", lambda **kwargs: provider)
    proposal = client.post(
        "/agent/chat",
        json={"messages": [{"role": "user", "content": "Book this appointment."}]},
    ).json()

    response = client.post(
        "/agent/chat",
        json={
            "messages": [{"role": "user", "content": rejection}],
            "pending_booking_id": proposal["pending_booking_id"],
        },
    )

    assert response.status_code == 200
    assert response.json()["pending_booking_id"] is None
    assert "haven't booked anything" in response.json()["reply"]
    db = client.app.state.test_session()
    assert db.query(Appointment).count() == 0
    db.close()


def test_failed_booking_keeps_confirmation_details_for_retry(client, monkeypatch):
    provider = booking_provider()
    monkeypatch.setattr(chat, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(chat.httpx, "AsyncClient", lambda **kwargs: provider)
    proposal = client.post(
        "/agent/chat",
        json={"messages": [{"role": "user", "content": "Book this appointment."}]},
    ).json()
    invocations = []

    original_book = chat.book_appointment

    def fail_once(**kwargs):
        invocations.append(kwargs)
        if len(invocations) == 1:
            return {"success": False, "error": "temporary database failure"}
        return original_book(**kwargs)

    monkeypatch.setattr(chat, "book_appointment", fail_once)
    request_data = {
        "messages": [{"role": "user", "content": "Yes, book it"}],
        "pending_booking_id": proposal["pending_booking_id"],
    }

    failed = client.post("/agent/chat", json=request_data)
    assert failed.status_code == 200
    assert "couldn't complete the booking" in failed.json()["reply"]
    assert failed.json()["pending_booking_id"] == proposal["pending_booking_id"]

    retried = client.post("/agent/chat", json=request_data)
    assert retried.status_code == 200
    assert retried.json()["actions"][0]["result"]["success"] is True
    assert len(invocations) == 2
    assert invocations[0] == {
        "doctor_id": 1,
        "slot_id": 1,
        "patient_phone": "15552345678",
        "idempotent": True,
    }


def test_duplicate_confirmation_returns_cached_booking_without_second_call(
    client, monkeypatch
):
    provider = booking_provider()
    monkeypatch.setattr(chat, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(chat.httpx, "AsyncClient", lambda **kwargs: provider)
    proposal = client.post(
        "/agent/chat",
        json={"messages": [{"role": "user", "content": "Book this appointment."}]},
    ).json()
    original_book = chat.book_appointment
    calls = []

    def count_booking_calls(**kwargs):
        calls.append(kwargs)
        return original_book(**kwargs)

    monkeypatch.setattr(chat, "book_appointment", count_booking_calls)
    request_data = {
        "messages": [{"role": "user", "content": "Yes, book it"}],
        "pending_booking_id": proposal["pending_booking_id"],
    }
    first = client.post("/agent/chat", json=request_data)
    second = client.post("/agent/chat", json=request_data)

    assert first.status_code == second.status_code == 200
    assert first.json()["actions"][0]["result"]["appointment_id"] == 1
    assert second.json()["actions"][0]["result"]["appointment_id"] == 1
    assert len(calls) == 1
    db = client.app.state.test_session()
    assert db.query(Appointment).count() == 1
    db.close()

def test_assistant_cancellation_requires_confirmation_and_is_idempotent(
    client, monkeypatch
):
    booking = chat._run_tool(
        "book_appointment",
        {"doctor_id": 1, "slot_id": 1, "patient_phone": "15552345678"},
    )
    assert booking["success"] is True

    provider = StubAsyncClient([
        StubResponse({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "cancellation-call",
                        "type": "function",
                        "function": {
                            "name": "cancel_appointment",
                            "arguments": json.dumps({
                                "appointment_id": booking["appointment_id"],
                                "patient_phone": "15552345678",
                            }),
                        },
                    }],
                },
            }],
        }),
        StubResponse({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "Your appointment has been cancelled.",
                },
            }],
        }),
    ])
    monkeypatch.setattr(chat, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(chat.httpx, "AsyncClient", lambda **kwargs: provider)

    proposal = client.post(
        "/agent/chat",
        json={"messages": [{"role": "user", "content": "Cancel my appointment."}]},
    )

    assert proposal.status_code == 200
    proposal_data = proposal.json()
    assert proposal_data["actions"] == []
    assert proposal_data["pending_cancellation_id"]
    assert "Should I cancel" in proposal_data["reply"]

    db = client.app.state.test_session()
    assert db.query(Appointment).one().status == "booked"
    db.close()

    confirmation_request = {
        "messages": [{"role": "user", "content": "Yes, cancel it."}],
        "pending_cancellation_id": proposal_data["pending_cancellation_id"],
    }
    cancelled = client.post("/agent/chat", json=confirmation_request)
    repeated = client.post("/agent/chat", json=confirmation_request)

    assert cancelled.status_code == repeated.status_code == 200
    assert cancelled.json()["actions"][0]["result"]["status"] == "cancelled"
    assert repeated.json()["actions"][0]["result"] == cancelled.json()["actions"][0]["result"]
    assert repeated.json()["pending_cancellation_id"] is None

    db = client.app.state.test_session()
    assert db.query(Appointment).one().status == "cancelled"
    db.close()


@pytest.mark.parametrize(
    ("patient_reply", "expected_pending"),
    [
        ("No, don't cancel it.", False),
        ("Where is the clinic?", True),
    ],
)
def test_assistant_rejection_or_question_preserves_appointment(
    client,
    monkeypatch,
    patient_reply,
    expected_pending,
):
    booking = chat._run_tool(
        "book_appointment",
        {"doctor_id": 1, "slot_id": 1, "patient_phone": "15552345678"},
    )
    provider = StubAsyncClient([
        StubResponse({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "cancellation-call",
                        "type": "function",
                        "function": {
                            "name": "cancel_appointment",
                            "arguments": json.dumps({
                                "appointment_id": booking["appointment_id"],
                                "patient_phone": "15552345678",
                            }),
                        },
                    }],
                },
            }],
        }),
        StubResponse({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "Your appointment has been cancelled.",
                },
            }],
        }),
    ])
    monkeypatch.setattr(chat, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(chat.httpx, "AsyncClient", lambda **kwargs: provider)
    proposal = client.post(
        "/agent/chat",
        json={"messages": [{"role": "user", "content": "Cancel my appointment."}]},
    ).json()

    rejected = client.post(
        "/agent/chat",
        json={
            "messages": [{"role": "user", "content": patient_reply}],
            "pending_cancellation_id": proposal["pending_cancellation_id"],
        },
    )

    assert rejected.status_code == 200
    expected_token = (
        proposal["pending_cancellation_id"]
        if expected_pending
        else None
    )
    assert rejected.json()["pending_cancellation_id"] == expected_token
    db = client.app.state.test_session()
    assert db.query(Appointment).one().status == "booked"
    db.close()


def test_failed_cancellation_retains_confirmation_for_retry(client, monkeypatch):
    booking = chat._run_tool(
        "book_appointment",
        {"doctor_id": 1, "slot_id": 1, "patient_phone": "15552345678"},
    )
    provider = StubAsyncClient([
        StubResponse({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "cancellation-call",
                        "type": "function",
                        "function": {
                            "name": "cancel_appointment",
                            "arguments": json.dumps({
                                "appointment_id": booking["appointment_id"],
                                "patient_phone": "15552345678",
                            }),
                        },
                    }],
                },
            }],
        }),
    ])
    monkeypatch.setattr(chat, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(chat.httpx, "AsyncClient", lambda **kwargs: provider)
    proposal = client.post(
        "/agent/chat",
        json={"messages": [{"role": "user", "content": "Cancel my appointment."}]},
    ).json()

    calls = []
    original_cancel = chat.cancel_appointment

    def fail_once(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"success": False, "error": "temporary database failure"}
        return original_cancel(**kwargs)

    monkeypatch.setattr(chat, "cancel_appointment", fail_once)
    request_data = {
        "messages": [{"role": "user", "content": "Go ahead and cancel it."}],
        "pending_cancellation_id": proposal["pending_cancellation_id"],
    }

    failed = client.post("/agent/chat", json=request_data)
    retried = client.post("/agent/chat", json=request_data)

    assert failed.status_code == retried.status_code == 200
    assert failed.json()["pending_cancellation_id"] == proposal["pending_cancellation_id"]
    assert failed.json()["actions"][0]["result"]["success"] is False
    assert retried.json()["actions"][0]["result"]["success"] is True
    assert len(calls) == 2
    db = client.app.state.test_session()
    assert db.query(Appointment).one().status == "cancelled"
    db.close()


def test_expired_cancellation_confirmation_does_not_cancel(client, monkeypatch):
    booking = chat._run_tool(
        "book_appointment",
        {"doctor_id": 1, "slot_id": 1, "patient_phone": "15552345678"},
    )
    provider = StubAsyncClient([
        StubResponse({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "cancellation-call",
                        "type": "function",
                        "function": {
                            "name": "cancel_appointment",
                            "arguments": json.dumps({
                                "appointment_id": booking["appointment_id"],
                                "patient_phone": "15552345678",
                            }),
                        },
                    }],
                },
            }],
        }),
    ])
    monkeypatch.setattr(chat, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(chat.httpx, "AsyncClient", lambda **kwargs: provider)
    proposal = client.post(
        "/agent/chat",
        json={"messages": [{"role": "user", "content": "Cancel my appointment."}]},
    ).json()
    monkeypatch.setattr(
        "app.agent.confirmation._PENDING_CANCELLATION_TTL_SECONDS",
        0,
    )

    expired = client.post(
        "/agent/chat",
        json={
            "messages": [{"role": "user", "content": "Yes, cancel it."}],
            "pending_cancellation_id": proposal["pending_cancellation_id"],
        },
    )

    assert expired.status_code == 200
    assert expired.json()["pending_cancellation_id"] is None
    assert "expired" in expired.json()["reply"]
    db = client.app.state.test_session()
    assert db.query(Appointment).one().status == "booked"
    db.close()


def test_assistant_does_not_claim_failed_cancellation_succeeded(client, monkeypatch):
    booking = chat._run_tool(
        "book_appointment",
        {"doctor_id": 1, "slot_id": 1, "patient_phone": "15552345678"},
    )
    arguments = {
        "appointment_id": booking["appointment_id"],
        "patient_phone": "15552345678",
    }
    provider = StubAsyncClient([
        StubResponse({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "cancel_appointment-call",
                        "type": "function",
                        "function": {
                            "name": "cancel_appointment",
                            "arguments": json.dumps(arguments),
                        },
                    }],
                },
            }],
        }),
    ])
    monkeypatch.setattr(chat, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(
        chat,
        "cancel_appointment",
        lambda **kwargs: {"success": False, "error": "Slot unavailable."},
    )
    monkeypatch.setattr(chat.httpx, "AsyncClient", lambda **kwargs: provider)

    proposal = client.post(
        "/agent/chat",
        json={"messages": [{"role": "user", "content": "Cancel the appointment."}]},
    ).json()
    response = client.post(
        "/agent/chat",
        json={
            "messages": [{"role": "user", "content": "Yes, cancel it."}],
            "pending_cancellation_id": proposal["pending_cancellation_id"],
        },
    )

    assert response.status_code == 200
    assert "couldn't cancel" in response.json()["reply"]
    assert response.json()["actions"][0]["result"]["success"] is False
    assert "has been cancelled" not in response.json()["reply"]


@pytest.mark.parametrize(
    ("provider_status", "expected_status"),
    [(400, 502), (401, 502), (403, 502), (404, 502), (429, 503), (500, 502)],
)
def test_provider_http_errors_return_safe_status_and_diagnostics(
    client, monkeypatch, caplog, provider_status, expected_status
):
    provider = StubAsyncClient([
        StubResponse(
            {"error": {
                "type": "invalid_request_error",
                "code": "model_not_found",
                "message": "Provider rejected gsk_secret123.",
            }},
            status_code=provider_status,
            headers={"Retry-After": "12"},
        ),
    ])
    monkeypatch.setattr(chat, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(chat.httpx, "AsyncClient", lambda **kwargs: provider)

    with caplog.at_level(logging.WARNING):
        response = client.post(
            "/agent/chat",
            json={"messages": [{"role": "user", "content": "Hello"}]},
        )

    assert response.status_code == expected_status
    assert "gsk_secret123" not in caplog.text
    assert "gsk_secret123" not in response.text
    if provider_status == 429:
        assert response.headers["retry-after"] == "12"


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [(httpx.ReadTimeout("request timeout"), 504), (httpx.ConnectError("connection refused"), 502)],
)
def test_provider_network_errors_do_not_crash_backend(client, monkeypatch, error, expected_status):
    provider = StubAsyncClient([error])
    monkeypatch.setattr(chat, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(chat.httpx, "AsyncClient", lambda **kwargs: provider)

    response = client.post(
        "/agent/chat",
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )

    assert response.status_code == expected_status


def test_malformed_tool_call_is_rejected_before_execution(client, monkeypatch):
    provider = StubAsyncClient([
        StubResponse({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "type": "function",
                        "function": {
                            "name": "book_appointment",
                            "arguments": "{}",
                        },
                    }],
                },
            }],
        }),
    ])
    monkeypatch.setattr(chat, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(chat, "_run_tool", lambda *args: pytest.fail("invalid tool call was executed"))
    monkeypatch.setattr(chat.httpx, "AsyncClient", lambda **kwargs: provider)

    response = client.post(
        "/agent/chat",
        json={"messages": [{"role": "user", "content": "Book the appointment."}]},
    )

    assert response.status_code == 502


def test_provider_endpoints_explain_missing_credentials(client, monkeypatch):
    monkeypatch.setattr(chat, "GROQ_API_KEY", "")
    monkeypatch.setattr(voice, "GROQ_API_KEY", "")

    chat_response = client.post(
        "/agent/chat",
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )
    transcription_response = client.post(
        "/voice/transcribe",
        files={"audio": ("clip.webm", b"sample audio", "audio/webm")},
    )

    assert chat_response.status_code == 503
    assert "GROQ_API_KEY" in chat_response.json()["detail"]
    assert transcription_response.status_code == 503
