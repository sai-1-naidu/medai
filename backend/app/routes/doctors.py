from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.models import Doctor, AppointmentSlot
from app.dependencies import get_db

router = APIRouter(
    prefix="/doctors",
    tags=["doctors"]
)

@router.get("/")
def get_doctors(db: Session = Depends(get_db)):

    doctors = db.query(Doctor).all()

    return doctors

@router.get("/{doctor_id}/slots")

def get_doctor_slots(doctor_id: int, db: Session = Depends(get_db)):

    doctor = db.query(Doctor).filter(Doctor.id == doctor_id).first()

    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")

    slots = db.query(AppointmentSlot).filter(AppointmentSlot.doctor_id == doctor_id, AppointmentSlot.is_booked == False).all()

    return slots

