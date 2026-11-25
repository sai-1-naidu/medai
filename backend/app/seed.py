from datetime import datetime, timedelta

from app.database import SessionLocal, Base, engine
from app import models
from app.models import Doctor, AppointmentSlot


# --------------------------------------------------
# 1. Create database tables if they don't exist
# --------------------------------------------------

Base.metadata.create_all(bind=engine)


# --------------------------------------------------
# 2. Create database session
# --------------------------------------------------

db = SessionLocal()


try:
    # --------------------------------------------------
    # 3. Seed only when the doctors table is empty
    # --------------------------------------------------

    if db.query(Doctor).count() == 0:

        doctors = [
            Doctor(
                name="Dr. Ananya Rao",
                speciality="Cardiology",
                consultation_minutes=30
            ),
            Doctor(
                name="Dr. Rahul Sharma",
                speciality="Dermatology",
                consultation_minutes=20
            ),
            Doctor(
                name="Dr. Priya Reddy",
                speciality="Neurology",
                consultation_minutes=30
            ),
            Doctor(
                name="Dr. Arjun Kumar",
                speciality="Orthopedics",
                consultation_minutes=30
            ),
        ]

        # Insert doctors into database
        db.add_all(doctors)
        db.commit()

        # Fetch doctors again to get their database IDs
        doctors = db.query(Doctor).all()

        # --------------------------------------------------
        # 4. Generate appointment slots for the next 3 days
        # --------------------------------------------------

        today = datetime.now().replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0
        )

        slots = []

        for doctor in doctors:

            # Generate slots for the next 3 days
            for day in range(1, 4):

                date = today + timedelta(days=day)

                # Available appointment start times
                for hour in [9, 10, 11, 14, 15, 16]:

                    start_time = date.replace(
                        hour=hour,
                        minute=0
                    )

                    end_time = start_time + timedelta(
                        minutes=doctor.consultation_minutes
                    )

                    slot = AppointmentSlot(
                        doctor_id=doctor.id,
                        start_time=start_time,
                        end_time=end_time,
                        is_booked=False
                    )

                    slots.append(slot)

        # Insert all appointment slots into database
        db.add_all(slots)
        db.commit()

        print("Sample doctors and slots created successfully!")

    else:

        print("Seed data already exists. Skipping.")


# --------------------------------------------------
# 5. Handle errors
# --------------------------------------------------

except Exception as e:

    db.rollback()

    print("Error while seeding database:", e)


# --------------------------------------------------
# 6. Always close database session
# --------------------------------------------------

finally:

    db.close()