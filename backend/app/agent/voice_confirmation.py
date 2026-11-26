import asyncio
from datetime import datetime
from enum import Enum
import logging
import re
import secrets
import time
from typing import Callable

from app.agent.confirmation import (
    assistant_requested_confirmation,
    classify_confirmation,
    normalize_confirmation_text,
)

logger = logging.getLogger(__name__)


class VoiceBookingStatus(str, Enum):
    IDLE = "IDLE"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    BOOKED = "BOOKED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class VoiceBookingSession:
    def __init__(
        self,
        book_appointment: Callable[..., dict],
        *,
        confirmation_ttl_seconds: float = 30 * 60,
    ) -> None:
        self._book_appointment = book_appointment
        self._confirmation_ttl_seconds = confirmation_ttl_seconds
        self._lock = asyncio.Lock()
        self._status = VoiceBookingStatus.IDLE
        self._pending: dict | None = None
        self._result: dict | None = None
        self._expires_at: float | None = None
        self._readback_complete = False

    @property
    def status(self) -> VoiceBookingStatus:
        return self._status

    @property
    def pending(self) -> dict | None:
        return dict(self._pending) if self._pending is not None else None

    @property
    def result(self) -> dict | None:
        return dict(self._result) if self._result is not None else None

    async def stage(self, preview: dict) -> dict:
        start_time = datetime.fromisoformat(preview["start_time"])
        details = {
            "confirmation_id": secrets.token_urlsafe(24),
            "doctor_id": preview["doctor_id"],
            "doctor_name": preview["doctor_name"],
            "speciality": preview["speciality"],
            "date": start_time.date().isoformat(),
            "slot_id": preview["slot_id"],
            "time": start_time.strftime("%I:%M %p").lstrip("0"),
            "start_time": preview["start_time"],
            "end_time": preview["end_time"],
            "patient_phone": preview["patient_phone"],
        }

        async with self._lock:
            if (
                self._status == VoiceBookingStatus.AWAITING_CONFIRMATION
                or self._status == VoiceBookingStatus.FAILED
            ) and (
                self._expires_at is not None
                and time.monotonic() < self._expires_at
                and self._pending is not None
                and all(
                    self._pending[key] == details[key]
                    for key in (
                        "doctor_id",
                        "slot_id",
                        "patient_phone",
                    )
                )
            ):
                details = dict(self._pending)
            else:
                self._pending = details
                self._result = None
                self._status = VoiceBookingStatus.AWAITING_CONFIRMATION
                self._expires_at = time.monotonic() + self._confirmation_ttl_seconds
                self._readback_complete = False

            return {
                **details,
                "status": self._status.value,
                "summary": self.format_summary(details),
            }

    def format_summary(self, details: dict | None = None) -> str:
        booking = details or self._pending
        if booking is None:
            return ""
        start_time = datetime.fromisoformat(booking["start_time"])
        date = f"{start_time:%A}, {start_time:%B} {start_time.day}"
        return (
            f"I found {booking['doctor_name']} in {booking['speciality']}. "
            f"{date} at {booking['time']} is available. "
            f"The phone number is {booking['patient_phone']}. "
            "Would you like me to book this appointment?"
        )

    async def mark_readback_complete(self, assistant_text: str) -> bool:
        async with self._lock:
            if (
                self._status not in {
                    VoiceBookingStatus.AWAITING_CONFIRMATION,
                    VoiceBookingStatus.FAILED,
                }
                or self._pending is None
            ):
                return False

            spoken_digits = re.sub(r"\D", "", assistant_text)
            normalized_text = normalize_confirmation_text(assistant_text)
            start_time = datetime.fromisoformat(self._pending["start_time"])
            name_tokens = [
                token
                for token in normalize_confirmation_text(self._pending["doctor_name"]).split()
                if token not in {"dr", "doctor"}
            ]
            date_was_read = (
                (
                    start_time.strftime("%B").casefold() in normalized_text.split()
                    and str(start_time.day) in normalized_text.split()
                )
                or start_time.strftime("%A").casefold() in normalized_text.split()
                or any(word in normalized_text.split() for word in ("today", "tomorrow"))
            )
            time_was_read = self._time_was_read(assistant_text, start_time)
            if (
                not assistant_requested_confirmation(assistant_text, "booking")
                or self._pending["patient_phone"] not in spoken_digits
                or not all(token in normalized_text.split() for token in name_tokens)
                or not date_was_read
                or not time_was_read
            ):
                return False

            self._readback_complete = True
            return True

    @staticmethod
    def _time_was_read(text: str, start_time: datetime) -> bool:
        hour = int(start_time.strftime("%I"))
        minute = start_time.minute
        meridiem = start_time.strftime("%p").casefold()
        minutes = rf":0?{minute:02d}" if minute else r"(?::00)?"
        numeric_time = rf"\b0?{hour}{minutes}\s*{meridiem}\b"
        if re.search(numeric_time, text, re.IGNORECASE):
            return True

        number_words = (
            "one", "two", "three", "four", "five", "six",
            "seven", "eight", "nine", "ten", "eleven", "twelve",
        )
        hour_word = number_words[hour - 1]
        minute_words = {
            0: "",
            15: " fifteen",
            30: " thirty",
            45: " forty five",
        }
        minute_word = minute_words.get(minute)
        if minute_word is None:
            return False
        spoken_time = rf"\b{hour_word}{minute_word}\s*{meridiem}\b"
        return re.search(spoken_time, text, re.IGNORECASE) is not None

    async def handle_confirmation(self, text: str) -> dict | None:
        async with self._lock:
            if (
                self._status in {
                    VoiceBookingStatus.AWAITING_CONFIRMATION,
                    VoiceBookingStatus.FAILED,
                }
                and self._pending is not None
                and (
                    self._expires_at is None
                    or time.monotonic() >= self._expires_at
                )
            ):
                self._pending = None
                self._result = None
                self._expires_at = None
                self._readback_complete = False
                self._status = VoiceBookingStatus.IDLE
                return {
                    "handled": True,
                    "status": self._status,
                    "reply": "That booking confirmation expired. I haven't booked anything. Please check availability again.",
                }

            decision = classify_confirmation(text)
            if self._status == VoiceBookingStatus.BOOKED:
                if decision == "confirm" and self._result is not None:
                    return {
                        "handled": True,
                        "status": self._status,
                        "result": dict(self._result),
                    }
                return None

            if self._pending is None or self._status not in {
                VoiceBookingStatus.AWAITING_CONFIRMATION,
                VoiceBookingStatus.FAILED,
            }:
                return None

            if decision == "reject":
                self._pending = None
                self._result = None
                self._expires_at = None
                self._readback_complete = False
                self._status = VoiceBookingStatus.REJECTED
                return {"handled": True, "status": self._status}
            if decision != "confirm":
                return None
            if not self._readback_complete:
                return {
                    "handled": True,
                    "status": self._status,
                    "reply": self.format_summary(),
                    "readback_replayed": True,
                }

            self._status = VoiceBookingStatus.CONFIRMED
            details = dict(self._pending)
            try:
                result = await asyncio.to_thread(
                    self._book_appointment,
                    doctor_id=details["doctor_id"],
                    slot_id=details["slot_id"],
                    patient_phone=details["patient_phone"],
                    idempotent=True,
                )
            except Exception as exc:
                logger.error(
                    "Voice booking failed (%s, slot_id=%s).",
                    type(exc).__name__,
                    details["slot_id"],
                )
                result = {"success": False, "error": "The booking could not be completed."}

            if not result.get("success"):
                self._status = VoiceBookingStatus.FAILED
                logger.warning(
                    "Voice booking attempt failed (slot_id=%s, code=%s).",
                    details["slot_id"],
                    result.get("code", "unknown"),
                )
                return {
                    "handled": True,
                    "status": self._status,
                    "result": result,
                }

            self._result = dict(result)
            self._status = VoiceBookingStatus.BOOKED
            logger.info("Voice booking completed (slot_id=%s).", details["slot_id"])
            return {
                "handled": True,
                "status": self._status,
                "result": dict(result),
            }
