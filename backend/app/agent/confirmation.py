from dataclasses import dataclass
import re
import secrets
import threading
import time


_NEGATIVE_PHRASES = (
    "no",
    "nope",
    "not yet",
    "not now",
    "stop",
    "change",
    "different",
    "maybe later",
    "do not",
    "don t",
)
_AFFIRMATIVE_PHRASES = (
    "yes book it",
    "yes book that",
    "yes please book it",
    "yes please book that",
    "yes go ahead",
    "yes i confirm",
    "yes that s correct",
    "yes that s fine",
    "yes that is correct",
    "yes that is fine",
    "okay book it",
    "ok book it",
    "please book it",
    "please book that",
    "book that slot",
    "book this slot",
    "please confirm the appointment",
    "i confirm",
    "go ahead",
    "proceed",
    "that s correct",
    "that is correct",
    "that s right",
    "that s fine",
    "that is fine",
    "yeah",
    "yep",
    "yes",
    "confirm",
    "confirmed",
    "correct",
    "sure",
    "absolutely",
    "okay",
    "ok",
    "please do",
    "book it",
)
_CANCELLATION_AFFIRMATIVES = (
    "yes cancel it",
    "yes please cancel it",
    "yes cancel the appointment",
    "please cancel it",
    "go ahead and cancel it",
    "cancel it",
    "cancel the appointment",
    "cancel that",
)
_SHORT_AFFIRMATIVES = {
    "yes",
    "yeah",
    "yep",
    "sure",
    "absolutely",
    "confirm",
    "confirmed",
    "correct",
    "okay",
    "ok",
}


@dataclass
class _PendingAction:
    details: dict
    created_at: float
    result: dict | None = None


_PENDING_BOOKING_TTL_SECONDS = 30 * 60
_PENDING_CANCELLATION_TTL_SECONDS = 30 * 60
_pending_bookings: dict[str, _PendingAction] = {}
_pending_bookings_lock = threading.Lock()
_pending_cancellations: dict[str, _PendingAction] = {}
_pending_cancellations_lock = threading.Lock()


def normalize_confirmation_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.casefold())).strip()


def classify_confirmation(text: str, action: str = "booking") -> str:
    normalized = normalize_confirmation_text(text)
    if not normalized:
        return "unclear"
    if any(
        re.search(rf"\b{re.escape(phrase)}\b", normalized)
        for phrase in _NEGATIVE_PHRASES
    ):
        return "reject"
    if action != "cancellation" and re.search(r"\bcancel\b", normalized):
        return "reject"
    if action == "cancellation" and any(
        normalized == phrase or normalized.startswith(f"{phrase} ")
        for phrase in _CANCELLATION_AFFIRMATIVES
    ):
        return "confirm"
    leading_clause = normalize_confirmation_text(
        re.split(r"[.!?;\n]", text, maxsplit=1)[0]
    )
    if any(
        normalized == phrase
        or (
            phrase not in _SHORT_AFFIRMATIVES
            and normalized.startswith(f"{phrase} ")
        )
        for phrase in _AFFIRMATIVE_PHRASES
    ):
        return "confirm"
    if leading_clause in _SHORT_AFFIRMATIVES:
        return "confirm"
    return "unclear"


def create_pending_booking(details: dict) -> str:
    now = time.monotonic()
    with _pending_bookings_lock:
        expired = [
            token
            for token, booking in _pending_bookings.items()
            if now - booking.created_at >= _PENDING_BOOKING_TTL_SECONDS
        ]
        for token in expired:
            del _pending_bookings[token]

        token = secrets.token_urlsafe(32)
        _pending_bookings[token] = _PendingAction(
            details=dict(details),
            created_at=now,
        )
        return token


def get_pending_booking(token: str) -> dict | None:
    with _pending_bookings_lock:
        pending = _pending_bookings.get(token)
        if pending is None:
            return None
        if time.monotonic() - pending.created_at >= _PENDING_BOOKING_TTL_SECONDS:
            del _pending_bookings[token]
            return None
        return {
            "details": dict(pending.details),
            "result": dict(pending.result) if pending.result is not None else None,
        }


def complete_pending_booking(token: str, result: dict) -> None:
    with _pending_bookings_lock:
        pending = _pending_bookings.get(token)
        if pending is not None:
            pending.result = dict(result)


def discard_pending_booking(token: str) -> None:
    with _pending_bookings_lock:
        _pending_bookings.pop(token, None)


def create_pending_cancellation(details: dict) -> str:
    now = time.monotonic()
    with _pending_cancellations_lock:
        expired = [
            token
            for token, cancellation in _pending_cancellations.items()
            if now - cancellation.created_at >= _PENDING_CANCELLATION_TTL_SECONDS
        ]
        for token in expired:
            del _pending_cancellations[token]

        token = secrets.token_urlsafe(32)
        _pending_cancellations[token] = _PendingAction(
            details=dict(details),
            created_at=now,
        )
        return token


def get_pending_cancellation(token: str) -> dict | None:
    with _pending_cancellations_lock:
        pending = _pending_cancellations.get(token)
        if pending is None:
            return None
        if time.monotonic() - pending.created_at >= _PENDING_CANCELLATION_TTL_SECONDS:
            del _pending_cancellations[token]
            return None
        return {
            "details": dict(pending.details),
            "result": dict(pending.result) if pending.result is not None else None,
        }


def complete_pending_cancellation(token: str, result: dict) -> None:
    with _pending_cancellations_lock:
        pending = _pending_cancellations.get(token)
        if pending is not None:
            pending.result = dict(result)


def discard_pending_cancellation(token: str) -> None:
    with _pending_cancellations_lock:
        _pending_cancellations.pop(token, None)


def assistant_requested_confirmation(text: str, action: str) -> bool:
    normalized = normalize_confirmation_text(text)
    action_terms = {
        "booking": ("book", "booking"),
        "cancellation": ("cancel", "cancellation"),
    }.get(action, ())
    request_cues = (
        "confirm",
        "should i",
        "would you like",
        "shall i",
        "say yes",
        "reply yes",
    )
    return any(term in normalized.split() for term in action_terms) and any(
        cue in normalized for cue in request_cues
    )
