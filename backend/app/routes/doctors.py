from datetime import date as Date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.models import AppointmentSlot, Doctor

router = APIRouter(prefix="/doctors", tags=["doctors"])


@router.get("/")
def get_doctors(db: Session = Depends(get_db)):
    return db.query(Doctor).order_by(Doctor.name).all()


@router.get("/availability")
def get_availability(
    date: Date,
    speciality: str = Query(min_length=2, max_length=80),
    db: Session = Depends(get_db),
):
    start = datetime.combine(date, time.min)
    end = start + timedelta(days=1)
    doctors = db.query(Doctor).filter(
        Doctor.speciality.ilike(f"%{speciality.strip()}%")
    ).order_by(Doctor.name).all()

    results = []
    for doctor in doctors:
        slots = db.query(AppointmentSlot).filter(
            AppointmentSlot.doctor_id == doctor.id,
            AppointmentSlot.start_time >= start,
            AppointmentSlot.start_time < end,
            AppointmentSlot.start_time >= datetime.now(),
            AppointmentSlot.is_booked.is_(False),
        ).order_by(AppointmentSlot.start_time).all()
        if slots:
            results.append({
                "doctor_id": doctor.id,
                "doctor_name": doctor.name,
                "speciality": doctor.speciality,
                "available_slots": [
                    {
                        "slot_id": slot.id,
                        "start_time": slot.start_time.isoformat(),
                        "end_time": slot.end_time.isoformat(),
                    }
                    for slot in slots
                ],
            })
    return {"date": date.isoformat(), "doctors": results}


@router.get("/{doctor_id}/slots")
def get_doctor_slots(doctor_id: int, db: Session = Depends(get_db)):
    doctor = db.query(Doctor).filter(Doctor.id == doctor_id).first()
    if doctor is None:
        raise HTTPException(status_code=404, detail="Doctor not found")

    return db.query(AppointmentSlot).filter(
        AppointmentSlot.doctor_id == doctor_id,
        AppointmentSlot.is_booked.is_(False),
        AppointmentSlot.start_time >= datetime.now(),
    ).order_by(AppointmentSlot.start_time).all()
