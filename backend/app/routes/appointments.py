from fastapi import APIRouter, HTTPException

from app.agent.tools import book_appointment as create_appointment
from app.agent.tools import cancel_appointment as cancel_patient_appointment
from app.schemas import BookingRequest, CancellationRequest

router = APIRouter(prefix="/appointments", tags=["appointments"])


def raise_result_error(result: dict) -> None:
    status_code = {
        "not_found": 404,
        "forbidden": 403,
        "unavailable": 409,
    }.get(result.get("code"), 500)
    raise HTTPException(status_code=status_code, detail=result["error"])


@router.post("/book")
def book_appointment(request: BookingRequest):
    result = create_appointment(
        doctor_id=request.doctor_id,
        slot_id=request.slot_id,
        patient_phone=request.patient_phone,
    )
    if not result["success"]:
        raise_result_error(result)
    return result


@router.post("/{appointment_id}/cancel")
def cancel_appointment(appointment_id: int, request: CancellationRequest):
    result = cancel_patient_appointment(
        appointment_id=appointment_id,
        patient_phone=request.patient_phone,
    )
    if not result["success"]:
        raise_result_error(result)
    return result
