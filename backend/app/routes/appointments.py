from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.models import Doctor, AppointmentSlot, Appointment
from app.schemas import BookingRequest, CancellationRequest


router = APIRouter(
    prefix="/appointments",
    tags=["Appointments"]
)


@router.post("/book")
def book_appointment(
    request: BookingRequest,
    db: Session = Depends(get_db)
):
    # 1. Check if doctor exists
    doctor = db.query(Doctor).filter(
        Doctor.id == request.doctor_id
    ).first()

    if not doctor:
        raise HTTPException(
            status_code=404,
            detail="Doctor not found"
        )

    # 2. Atomically claim the available slot
    result = db.query(AppointmentSlot).filter(
        AppointmentSlot.id == request.slot_id,
        AppointmentSlot.doctor_id == request.doctor_id,
        AppointmentSlot.is_booked == False
    ).update(
        {AppointmentSlot.is_booked: True},
        synchronize_session=False
    )

    # 3. If no slot was updated, it is unavailable
    if result == 0:
        db.rollback()

        raise HTTPException(
            status_code=409,
            detail="Appointment slot is unavailable"
        )

    try:
        # 4. Create the appointment record
        appointment = Appointment(
            doctor_id=request.doctor_id,
            slot_id=request.slot_id,
            patient_phone=request.patient_phone,
            status="booked"
        )

        db.add(appointment)
        db.commit()
        db.refresh(appointment)

        # 5. Return confirmation details
        return {
            "message": "Appointment booked successfully",
            "appointment_id": appointment.id,
            "doctor": doctor.name,
            "speciality": doctor.speciality,
            "slot_id": appointment.slot_id,
            "patient_phone": appointment.patient_phone,
            "status": appointment.status
        }

    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Booking failed. Please try again."
        )


@router.post("/{appointment_id}/cancel")
def cancel_appointment(
    appointment_id: int,
    request: CancellationRequest,
    db: Session = Depends(get_db)
):
    # 1. Find appointment
    appointment = db.query(Appointment).filter(
        Appointment.id == appointment_id
    ).first()

    if not appointment:
        raise HTTPException(
            status_code=404,
            detail="Appointment not found"
        )

    # 2. Verify the patient's phone number
    if appointment.patient_phone != request.patient_phone:
        raise HTTPException(
            status_code=403,
            detail="Phone number does not match appointment"
        )

    # 3. Check appointment status
    if appointment.status != "booked":
        raise HTTPException(
            status_code=409,
            detail="Appointment is already cancelled"
        )

    try:
        # 4. Mark appointment as cancelled
        appointment.status = "cancelled"

        # 5. Release the slot
        slot = db.query(AppointmentSlot).filter(
            AppointmentSlot.id == appointment.slot_id
        ).first()

        if slot:
            slot.is_booked = False

        db.commit()

        return {
            "message": "Appointment cancelled successfully",
            "appointment_id": appointment.id,
            "status": appointment.status
        }

    except Exception:
        db.rollback()

        raise HTTPException(
            status_code=500,
            detail="Cancellation failed. Please try again."
        )