import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def normalize_phone(value: str) -> str:
    cleaned = re.sub(r"\D", "", value)
    if not 10 <= len(cleaned) <= 15:
        raise ValueError("Enter a valid phone number")
    return cleaned


class BookingRequest(BaseModel):
    doctor_id: int = Field(gt=0)
    slot_id: int = Field(gt=0)
    patient_phone: str

    @field_validator("patient_phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        return normalize_phone(value)


class CancellationRequest(BaseModel):
    patient_phone: str

    @field_validator("patient_phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        return normalize_phone(value)


class CancellationToolRequest(BaseModel):
    appointment_id: int = Field(gt=0)
    patient_phone: str

    @field_validator("patient_phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        return normalize_phone(value)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message cannot be empty")
        return value


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=20)
    pending_booking_id: str | None = Field(default=None, min_length=20, max_length=100)
    pending_cancellation_id: str | None = Field(
        default=None,
        min_length=20,
        max_length=100,
    )

    @model_validator(mode="after")
    def validate_single_pending_action(self) -> "ChatRequest":
        if self.pending_booking_id and self.pending_cancellation_id:
            raise ValueError("Only one appointment confirmation may be pending")
        return self

    @field_validator("messages")
    @classmethod
    def validate_message_order(cls, messages: list[ChatMessage]) -> list[ChatMessage]:
        if messages[-1].role != "user":
            raise ValueError("The latest message must be from the patient")
        return messages
