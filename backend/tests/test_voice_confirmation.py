import asyncio

import pytest

from app.agent.confirmation import classify_confirmation
from app.agent.tools import book_appointment, prepare_appointment_booking
from app.agent.voice_confirmation import VoiceBookingSession, VoiceBookingStatus
from app.models import Appointment


AFFIRMATIVES = [
    "Yes",
    "Yes, book it",
    "Yes book it",
    "Go ahead",
    "Confirm",
    "Please book it",
    "That's correct",
    "Sure",
    "Absolutely",
    "Book that slot",
    "I confirm",
    "Okay, book it",
    "Yes, that's fine",
    "Please confirm the appointment",
    "Yes, book it. Where?",
]


def _prepared_session(book_appointment_function=book_appointment, *, ttl=1800):
    preview = prepare_appointment_booking(
        doctor_id=1,
        slot_id=1,
        patient_phone="15552345678",
    )
    assert preview["success"] is True
    state = VoiceBookingSession(
        book_appointment_function,
        confirmation_ttl_seconds=ttl,
    )
    return state, preview


def _appointment_count(client):
    db = client.app.state.test_session()
    try:
        return db.query(Appointment).count()
    finally:
        db.close()


@pytest.mark.parametrize("phrase", AFFIRMATIVES)
def test_voice_booking_state_accepts_natural_confirmations(client, phrase):
    state, preview = _prepared_session()

    async def confirm():
        pending = await state.stage(preview)
        assert await state.mark_readback_complete(pending["summary"])
        return await state.handle_confirmation(phrase)

    outcome = asyncio.run(confirm())

    assert outcome["handled"] is True
    assert outcome["status"] == VoiceBookingStatus.BOOKED
    assert outcome["result"]["success"] is True
    assert outcome["result"]["doctor_name"] == "Dr. Test"
    assert state.pending["confirmation_id"]
    assert _appointment_count(client) == 1


@pytest.mark.parametrize(
    "question",
    ["Where?", "Which doctor?", "Wait", "What time?", "Yes, where?"],
)
def test_voice_questions_preserve_pending_confirmation(client, question):
    state, preview = _prepared_session()

    async def handle():
        pending = await state.stage(preview)
        await state.mark_readback_complete(pending["summary"])
        return await state.handle_confirmation(question)

    outcome = asyncio.run(handle())

    assert outcome is None
    assert state.status == VoiceBookingStatus.AWAITING_CONFIRMATION
    assert state.pending["slot_id"] == 1
    assert _appointment_count(client) == 0


def test_voice_requires_booking_summary_before_consuming_confirmation(client):
    state, preview = _prepared_session()

    async def confirm_after_missing_readback():
        pending = await state.stage(preview)
        assert not await state.mark_readback_complete(
            f"Should I book? The phone number is {pending['patient_phone']}."
        )
        first = await state.handle_confirmation("Yes")
        assert first["readback_replayed"] is True
        assert state.status == VoiceBookingStatus.AWAITING_CONFIRMATION
        assert _appointment_count(client) == 0
        assert await state.mark_readback_complete(first["reply"])
        return await state.handle_confirmation("Yes")

    outcome = asyncio.run(confirm_after_missing_readback())

    assert outcome["status"] == VoiceBookingStatus.BOOKED
    assert outcome["result"]["success"] is True
    assert _appointment_count(client) == 1


@pytest.mark.parametrize("phrase", ["No.", "Cancel.", "Don't book it.", "No, wait."])
def test_voice_negative_confirmation_clears_pending_booking(client, phrase):
    state, preview = _prepared_session()

    async def handle():
        await state.stage(preview)
        return await state.handle_confirmation(phrase)

    outcome = asyncio.run(handle())

    assert outcome["handled"] is True
    assert outcome["status"] == VoiceBookingStatus.REJECTED
    assert state.pending is None
    assert _appointment_count(client) == 0


def test_voice_repeated_confirmation_returns_original_booking(client):
    calls = []

    def counting_booking(**kwargs):
        calls.append(kwargs)
        return book_appointment(**kwargs)

    state, preview = _prepared_session(counting_booking)

    async def handle_twice():
        pending = await state.stage(preview)
        await state.mark_readback_complete(pending["summary"])
        first = await state.handle_confirmation("Yes, book it")
        repeated = await state.handle_confirmation("Absolutely")
        return first, repeated

    first, repeated = asyncio.run(handle_twice())

    assert first["result"]["success"] is True
    assert repeated["result"] == first["result"]
    assert len(calls) == 1
    assert _appointment_count(client) == 1


def test_concurrent_voice_confirmations_create_only_one_appointment(client):
    calls = []

    def counting_booking(**kwargs):
        calls.append(kwargs)
        return book_appointment(**kwargs)

    state, preview = _prepared_session(counting_booking)

    async def confirm_concurrently():
        pending = await state.stage(preview)
        await state.mark_readback_complete(pending["summary"])
        return await asyncio.gather(
            state.handle_confirmation("Yes, book it"),
            state.handle_confirmation("Go ahead"),
        )

    outcomes = asyncio.run(confirm_concurrently())

    assert all(outcome["status"] == VoiceBookingStatus.BOOKED for outcome in outcomes)
    assert outcomes[0]["result"] == outcomes[1]["result"]
    assert len(calls) == 1
    assert _appointment_count(client) == 1


def test_voice_booking_failure_retains_draft_for_retry(client):
    calls = []

    def fail_once(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"success": False, "error": "temporary failure"}
        return book_appointment(**kwargs)

    state, preview = _prepared_session(fail_once)

    async def retry():
        pending = await state.stage(preview)
        await state.mark_readback_complete(pending["summary"])
        failed = await state.handle_confirmation("Go ahead")
        assert failed["status"] == VoiceBookingStatus.FAILED
        assert state.pending["slot_id"] == 1
        succeeded = await state.handle_confirmation("Yes")
        return failed, succeeded

    failed, succeeded = asyncio.run(retry())

    assert failed["result"]["success"] is False
    assert succeeded["result"]["success"] is True
    assert len(calls) == 2
    assert _appointment_count(client) == 1


def test_restaging_expired_booking_refreshes_confirmation_window(client, monkeypatch):
    state, preview = _prepared_session(ttl=60)
    clock = [100.0]
    monkeypatch.setattr(
        "app.agent.voice_confirmation.time.monotonic",
        lambda: clock[0],
    )

    async def restage_and_confirm():
        original = await state.stage(preview)
        clock[0] = 161.0
        renewed = await state.stage(preview)
        assert renewed["confirmation_id"] != original["confirmation_id"]
        assert await state.mark_readback_complete(renewed["summary"])
        return await state.handle_confirmation("Yes, book it")

    outcome = asyncio.run(restage_and_confirm())

    assert outcome["status"] == VoiceBookingStatus.BOOKED
    assert outcome["result"]["success"] is True
    assert _appointment_count(client) == 1


def test_expired_voice_confirmation_is_rejected(client):
    state, preview = _prepared_session(ttl=0)

    async def confirm_expired():
        await state.stage(preview)
        return await state.handle_confirmation("Yes, book it")

    outcome = asyncio.run(confirm_expired())

    assert outcome["handled"] is True
    assert outcome["status"] == VoiceBookingStatus.IDLE
    assert "expired" in outcome["reply"]
    assert state.pending is None
    assert _appointment_count(client) == 0


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("Yes, book it. Where?", "confirm"),
        ("Please confirm the appointment", "confirm"),
        ("Where?", "unclear"),
        ("What time?", "unclear"),
        ("Yes, where?", "unclear"),
        ("No, book it", "reject"),
    ],
)
def test_confirmation_classifier_distinguishes_approval_from_questions(phrase, expected):
    assert classify_confirmation(phrase) == expected
