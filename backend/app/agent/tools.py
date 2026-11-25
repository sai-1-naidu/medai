from datetime import datetime, timedelta

from app.database import SessionLocal
from app.models import Doctor, AppointmentSlot, Appointment


import re

def normalize_phone(phone:str) -> str | None :
    cleaned = re.sub(r"\D", "", phone
    )
    if not (10 <= len(cleaned) <= 15):
        return None
    return cleaned

def check_doctor_availability(speciality: str, date: str):
    """
    Find doctors of a speciality and their available slots
    on a requested date.
    """

    db = SessionLocal()

    try:
        requested_date = datetime.strptime(date, "%Y-%m-%d")

        next_day = requested_date + timedelta(days=1)

        doctors = db.query(Doctor).filter(
            Doctor.speciality.ilike(speciality)
        ).all()

        results = []

        for doctor in doctors:

            slots = db.query(AppointmentSlot).filter(
                AppointmentSlot.doctor_id == doctor.id,
                AppointmentSlot.start_time >= requested_date,
                AppointmentSlot.start_time < next_day,
                AppointmentSlot.is_booked == False
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
                            "end_time": slot.end_time.isoformat()
                        }
                        for slot in slots
                    ]
                })

        return {
            "success": True,
            "date": date,
            "doctors": results
        }

    except ValueError:
        return {
            "success": False,
            "error": "Invalid date format. Use YYYY-MM-DD."
        }

    except Exception as e:
        return {
            "success": False,
            "error": "Unable to check availability."
        }

    finally:
        db.close()

from app.models import Appointment


def book_appointment(
    doctor_id: int,
    slot_id: int,
    patient_phone: str
):
    """
    Book a doctor's appointment for a patient.
    """
    patient_phone = normalize_phone(patient_phone)
    if patient_phone is None:
        return {
            "success": False,
            "error": "Invalid phone number format."
        }
    db = SessionLocal()

    try:
        # Claim the slot only if it is still available.
        result = db.query(AppointmentSlot).filter(
            AppointmentSlot.id == slot_id,
            AppointmentSlot.doctor_id == doctor_id,
            AppointmentSlot.is_booked == False
        ).update(
            {AppointmentSlot.is_booked: True},
            synchronize_session=False
        )

        if result == 0:
            db.rollback()

            return {
                "success": False,
                "error": "This appointment slot is no longer available."
            }

        appointment = Appointment(
            doctor_id=doctor_id,
            slot_id=slot_id,
            patient_phone=patient_phone,
            status="booked"
        )

        db.add(appointment)
        db.commit()
        db.refresh(appointment)

        doctor = db.query(Doctor).filter(
            Doctor.id == doctor_id
        ).first()

        slot = db.query(AppointmentSlot).filter(
            AppointmentSlot.id == slot_id
        ).first()

        return {
            "success": True,
            "message": "Appointment booked successfully.",
            "appointment_id": appointment.id,
            "doctor_name": doctor.name,
            "speciality": doctor.speciality,
            "start_time": slot.start_time.isoformat(),
            "end_time": slot.end_time.isoformat(),
            "patient_phone": patient_phone
        }

    except Exception:
        db.rollback()

        return {
            "success": False,
            "error": "Booking failed. Please try again."
        }

    finally:
        db.close()


def cancel_appointment(
    appointment_id: int,
    patient_phone: str
):
    """
    Cancel an appointment after verifying the patient's phone number.
    """
    patient_phone = normalize_phone(patient_phone)
    if patient_phone is None:
        return {
                "success": False,
                "error": "Invalid phone number format."
            }
    db = SessionLocal()

    try:
        appointment = db.query(Appointment).filter(
            Appointment.id == appointment_id
        ).first()

        if not appointment:
            return {
                "success": False,
                "error": "Appointment not found."
            }

        # Verify the patient's phone number
        if appointment.patient_phone != patient_phone:
            return {
                "success": False,
                "error": "Phone number does not match this appointment."
            }

        # Prevent cancelling an already-cancelled appointment
        if appointment.status != "booked":
            return {
                "success": False,
                "error": "Appointment is already cancelled."
            }

        slot = db.query(AppointmentSlot).filter(
            AppointmentSlot.id == appointment.slot_id
        ).first()

        appointment.status = "cancelled"

        if slot:
            slot.is_booked = False

        db.commit()

        return {
            "success": True,
            "message": "Appointment cancelled successfully.",
            "appointment_id": appointment.id,
            "status": appointment.status
        }

    except Exception:
        db.rollback()

        return {
            "success": False,
            "error": "Cancellation failed. Please try again."
        }

    finally:
        db.close()