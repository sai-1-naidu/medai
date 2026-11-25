import re

from pydantic import BaseModel, field_validator


class BookingRequest(BaseModel):
    doctor_id: int
    slot_id: int
    patient_phone: str

    @field_validator("patient_phone")
    @classmethod
    def validate_phone(cls, value: str):
        cleaned = re.sub(r"\D", "", value)

        if len(cleaned) < 10 or len(cleaned) > 15:
            raise ValueError("Enter a valid phone number")

        return cleaned


class CancellationRequest(BaseModel):
    patient_phone: str

    @field_validator("patient_phone")
    @classmethod
    def validate_phone(cls, value: str):
        cleaned = re.sub(r"\D", "", value)

        if len(cleaned) < 10 or len(cleaned) > 15:
            raise ValueError("Enter a valid phone number")

        return cleaned