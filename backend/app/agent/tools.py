import logging
import re
from datetime import datetime, time, timedelta

from sqlalchemy.exc import SQLAlchemyError

from app.database import SessionLocal
from app.models import Appointment, AppointmentSlot, Doctor

logger = logging.getLogger(__name__)

_SPECIALITY_ALIASES = {
    "cardio": "cardiology",
    "cardiologist": "cardiology",
    "cardiologists": "cardiology",
    "cardiology": "cardiology",
    "dermatologist": "dermatology",
    "dermatologists": "dermatology",
    "dermatology": "dermatology",
    "neurologist": "neurology",
    "neurologists": "neurology",
    "neurology": "neurology",
    "orthopaedic": "orthopedics",
    "orthopaedics": "orthopedics",
    "orthopedics": "orthopedics",
    "orthopedist": "orthopedics",
    "orthopedists": "orthopedics",
    "orthopedic": "orthopedics",
    "orthopaedist": "orthopedics",
}


def normalize_phone(phone: str) -> str | None:
    cleaned = re.sub(r"\D", "", phone)
    return cleaned if 10 <= len(cleaned) <= 15 else None


def normalize_speciality_query(speciality: str) -> str:
    tokens = re.findall(r"[a-z]+", speciality.casefold())
    for token in tokens:
        canonical = _SPECIALITY_ALIASES.get(token)
        if canonical:
            return canonical
    return " ".join(tokens) or speciality.strip()


def _parse_availability_date(value: str) -> datetime:
    normalized = re.sub(r"\s+", " ", value.casefold().strip())
    normalized = re.sub(r"\btommorrow\b", "tomorrow", normalized)
    today = datetime.now().date()
    if "day after tomorrow" in normalized:
        requested_date = today + timedelta(days=2)
    elif re.search(r"\btomorrow\b", normalized):
        requested_date = today + timedelta(days=1)
    elif re.search(r"\btoday\b", normalized):
        requested_date = today
    else:
        return datetime.strptime(value.strip(), "%Y-%m-%d")
    return datetime.combine(requested_date, time.min)


def check_doctor_availability(speciality: str, date: str) -> dict:
    try:
        requested_date = _parse_availability_date(date)
    except ValueError:
        return {
            "success": False,
            "error": "Use YYYY-MM-DD or say today, tomorrow, or the day after tomorrow.",
        }

    next_day = requested_date + timedelta(days=1)
    speciality_query = normalize_speciality_query(speciality)
    if not speciality_query:
        return {"success": False, "error": "Please specify a medical speciality."}
    db = SessionLocal()
    try:
        doctors = db.query(Doctor).filter(
            Doctor.speciality.ilike(f"%{speciality_query}%")
        ).all()
        results = []
        for doctor in doctors:
            slots = db.query(AppointmentSlot).filter(
                AppointmentSlot.doctor_id == doctor.id,
                AppointmentSlot.start_time >= requested_date,
                AppointmentSlot.start_time >= datetime.now(),
                AppointmentSlot.start_time < next_day,
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
        return {
            "success": True,
            "date": requested_date.date().isoformat(),
            "doctors": results,
        }
    except SQLAlchemyError as exc:
        logger.error("Availability lookup failed (%s)", type(exc).__name__)
        return {"success": False, "error": "Availability lookup failed."}
    finally:
        db.close()


def prepare_appointment_booking(
    doctor_id: int,
    slot_id: int,
    patient_phone: str,
) -> dict:
    patient_phone = normalize_phone(patient_phone)
    if patient_phone is None:
        return {
            "success": False,
            "error": "Enter a valid phone number.",
            "code": "invalid_phone",
        }

    db = SessionLocal()
    try:
        doctor = db.query(Doctor).filter(Doctor.id == doctor_id).first()
        if doctor is None:
            return {"success": False, "error": "Doctor not found.", "code": "not_found"}

        slot = db.query(AppointmentSlot).filter(
            AppointmentSlot.id == slot_id,
            AppointmentSlot.doctor_id == doctor_id,
        ).first()
        if slot is None:
            return {"success": False, "error": "Appointment slot not found.", "code": "not_found"}
        if slot.is_booked or slot.start_time < datetime.now():
            return {
                "success": False,
                "error": "This appointment slot is no longer available.",
                "code": "unavailable",
            }

        return {
            "success": True,
            "doctor_id": doctor.id,
            "doctor_name": doctor.name,
            "speciality": doctor.speciality,
            "slot_id": slot.id,
            "start_time": slot.start_time.isoformat(),
            "end_time": slot.end_time.isoformat(),
            "patient_phone": patient_phone,
        }
    except SQLAlchemyError as exc:
        logger.error("Appointment confirmation preview failed (%s)", type(exc).__name__)
        return {"success": False, "error": "Booking details could not be verified."}
    finally:
        db.close()


def _booking_result(appointment: Appointment, doctor: Doctor, slot: AppointmentSlot) -> dict:
    return {
        "success": True,
        "message": "Appointment booked successfully.",
        "appointment_id": appointment.id,
        "doctor_name": doctor.name,
        "speciality": doctor.speciality,
        "slot_id": slot.id,
        "start_time": slot.start_time.isoformat(),
        "end_time": slot.end_time.isoformat(),
        "patient_phone": appointment.patient_phone,
        "status": appointment.status,
    }


def lookup_appointment(appointment_id: int, patient_phone: str) -> dict:
    patient_phone = normalize_phone(patient_phone)
    if patient_phone is None:
        return {"success": False, "error": "Enter a valid phone number."}

    db = SessionLocal()
    try:
        appointment = db.query(Appointment).filter(
            Appointment.id == appointment_id,
            Appointment.patient_phone == patient_phone,
            Appointment.status == "booked",
        ).first()
        if appointment is None:
            return {
                "success": False,
                "error": "No active appointment matches those details.",
                "code": "not_found",
            }

        doctor = db.query(Doctor).filter(Doctor.id == appointment.doctor_id).first()
        slot = db.query(AppointmentSlot).filter(
            AppointmentSlot.id == appointment.slot_id,
        ).first()
        if doctor is None or slot is None:
            logger.error("Appointment lookup found incomplete record (id=%s).", appointment_id)
            return {
                "success": False,
                "error": "Appointment details could not be loaded.",
            }
        return _booking_result(appointment, doctor, slot)
    except SQLAlchemyError as exc:
        logger.error("Appointment lookup failed (%s)", type(exc).__name__)
        return {"success": False, "error": "Appointment details could not be loaded."}
    finally:
        db.close()


def book_appointment(
    doctor_id: int,
    slot_id: int,
    patient_phone: str,
    *,
    idempotent: bool = False,
) -> dict:
    patient_phone = normalize_phone(patient_phone)
    if patient_phone is None:
        return {"success": False, "error": "Enter a valid phone number."}

    db = SessionLocal()
    try:
        doctor = db.query(Doctor).filter(Doctor.id == doctor_id).first()
        if doctor is None:
            return {"success": False, "error": "Doctor not found.", "code": "not_found"}

        if idempotent:
            existing = db.query(Appointment).filter(
                Appointment.slot_id == slot_id,
                Appointment.doctor_id == doctor_id,
                Appointment.patient_phone == patient_phone,
                Appointment.status == "booked",
            ).first()
            if existing is not None:
                slot = db.query(AppointmentSlot).filter(
                    AppointmentSlot.id == existing.slot_id,
                ).first()
                if slot is not None:
                    return _booking_result(existing, doctor, slot)

        claimed = db.query(AppointmentSlot).filter(
            AppointmentSlot.id == slot_id,
            AppointmentSlot.doctor_id == doctor_id,
            AppointmentSlot.is_booked.is_(False),
            AppointmentSlot.start_time >= datetime.now(),
        ).update(
            {AppointmentSlot.is_booked: True},
            synchronize_session=False,
        )
        if claimed != 1:
            db.rollback()
            if idempotent:
                existing = db.query(Appointment).filter(
                    Appointment.slot_id == slot_id,
                    Appointment.doctor_id == doctor_id,
                    Appointment.patient_phone == patient_phone,
                    Appointment.status == "booked",
                ).first()
                if existing is not None:
                    slot = db.query(AppointmentSlot).filter(
                        AppointmentSlot.id == existing.slot_id,
                    ).first()
                    if slot is not None:
                        return _booking_result(existing, doctor, slot)
            return {
                "success": False,
                "error": "This appointment slot is no longer available.",
                "code": "unavailable",
            }

        slot = db.query(AppointmentSlot).filter(AppointmentSlot.id == slot_id).first()
        appointment = Appointment(
            doctor_id=doctor_id,
            slot_id=slot_id,
            patient_phone=patient_phone,
            status="booked",
        )
        db.add(appointment)
        db.commit()
        db.refresh(appointment)
        return _booking_result(appointment, doctor, slot)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Appointment booking failed (%s)", type(exc).__name__)
        return {"success": False, "error": "Booking failed. Please try again."}
    finally:
        db.close()


def cancel_appointment(
    appointment_id: int,
    patient_phone: str,
    *,
    idempotent: bool = False,
) -> dict:
    patient_phone = normalize_phone(patient_phone)
    if patient_phone is None:
        return {"success": False, "error": "Enter a valid phone number."}

    db = SessionLocal()
    try:
        appointment = db.query(Appointment).filter(
            Appointment.id == appointment_id
        ).first()
        if appointment is None:
            return {"success": False, "error": "Appointment not found.", "code": "not_found"}
        if appointment.patient_phone != patient_phone:
            return {
                "success": False,
                "error": "Phone number does not match this appointment.",
                "code": "forbidden",
            }
        if appointment.status != "booked":
            if idempotent and appointment.status == "cancelled":
                return {
                    "success": True,
                    "message": "Appointment cancelled successfully.",
                    "appointment_id": appointment_id,
                    "status": "cancelled",
                }
            return {
                "success": False,
                "error": "Appointment is already cancelled.",
                "code": "unavailable",
            }

        updated = db.query(Appointment).filter(
            Appointment.id == appointment_id,
            Appointment.patient_phone == patient_phone,
            Appointment.status == "booked",
        ).update(
            {Appointment.status: "cancelled"},
            synchronize_session=False,
        )
        if updated != 1:
            db.rollback()
            if idempotent:
                existing = db.query(Appointment).filter(
                    Appointment.id == appointment_id,
                    Appointment.patient_phone == patient_phone,
                    Appointment.status == "cancelled",
                ).first()
                if existing is not None:
                    return {
                        "success": True,
                        "message": "Appointment cancelled successfully.",
                        "appointment_id": appointment_id,
                        "status": "cancelled",
                    }
            return {
                "success": False,
                "error": "Appointment is already cancelled.",
                "code": "unavailable",
            }

        db.query(AppointmentSlot).filter(
            AppointmentSlot.id == appointment.slot_id
        ).update(
            {AppointmentSlot.is_booked: False},
            synchronize_session=False,
        )
        db.commit()
        return {
            "success": True,
            "message": "Appointment cancelled successfully.",
            "appointment_id": appointment_id,
            "status": "cancelled",
        }
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Appointment cancellation failed (%s)", type(exc).__name__)
        return {"success": False, "error": "Cancellation failed. Please try again."}
    finally:
        db.close()


def prepare_appointment_cancellation(
    appointment_id: int,
    patient_phone: str,
) -> dict:
    patient_phone = normalize_phone(patient_phone)
    if patient_phone is None:
        return {
            "success": False,
            "error": "Enter a valid phone number.",
            "code": "invalid_phone",
        }

    db = SessionLocal()
    try:
        appointment = db.query(Appointment).filter(
            Appointment.id == appointment_id
        ).first()
        if appointment is None:
            return {"success": False, "error": "Appointment not found.", "code": "not_found"}
        if appointment.patient_phone != patient_phone:
            return {
                "success": False,
                "error": "Phone number does not match this appointment.",
                "code": "forbidden",
            }
        if appointment.status != "booked":
            return {
                "success": False,
                "error": "Appointment is already cancelled.",
                "code": "unavailable",
            }

        doctor = db.query(Doctor).filter(Doctor.id == appointment.doctor_id).first()
        slot = db.query(AppointmentSlot).filter(
            AppointmentSlot.id == appointment.slot_id
        ).first()
        if doctor is None or slot is None:
            return {"success": False, "error": "Appointment details could not be verified."}

        return {
            "success": True,
            "appointment_id": appointment.id,
            "doctor_name": doctor.name,
            "speciality": doctor.speciality,
            "slot_id": slot.id,
            "start_time": slot.start_time.isoformat(),
            "end_time": slot.end_time.isoformat(),
            "patient_phone": patient_phone,
        }
    except SQLAlchemyError as exc:
        logger.error("Appointment cancellation preview failed (%s)", type(exc).__name__)
        return {"success": False, "error": "Cancellation details could not be verified."}
    finally:
        db.close()
