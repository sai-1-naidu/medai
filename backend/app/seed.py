from datetime import datetime, timedelta

from app.database import Base, SessionLocal, engine
from app.models import AppointmentSlot, Doctor


def seed_database() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        doctors = db.query(Doctor).order_by(Doctor.id).all()
        if not doctors:
            doctors = [
                Doctor(name="Dr. Ananya Rao", speciality="Cardiology", consultation_minutes=30),
                Doctor(name="Dr. Rahul Sharma", speciality="Dermatology", consultation_minutes=20),
                Doctor(name="Dr. Priya Reddy", speciality="Neurology", consultation_minutes=30),
                Doctor(name="Dr. Arjun Kumar", speciality="Orthopedics", consultation_minutes=30),
            ]
            db.add_all(doctors)
            db.flush()

        first_day = datetime.now().replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ) + timedelta(days=1)
        last_day = first_day + timedelta(days=7)
        existing_slots = db.query(
            AppointmentSlot.doctor_id,
            AppointmentSlot.start_time,
        ).filter(
            AppointmentSlot.start_time >= first_day,
            AppointmentSlot.start_time < last_day,
        ).all()
        existing_keys = {(doctor_id, start_time) for doctor_id, start_time in existing_slots}

        slots = []
        for doctor in doctors:
            for day in range(7):
                appointment_date = first_day + timedelta(days=day)
                for hour in (9, 10, 11, 14, 15, 16):
                    start_time = appointment_date.replace(hour=hour)
                    if (doctor.id, start_time) in existing_keys:
                        continue
                    slots.append(
                        AppointmentSlot(
                            doctor_id=doctor.id,
                            start_time=start_time,
                            end_time=start_time + timedelta(minutes=doctor.consultation_minutes),
                            is_booked=False,
                        )
                    )

        db.add_all(slots)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_database()
