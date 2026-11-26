from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agent.tools import cancel_appointment as cancel_service
from app.database import Base
from app.models import Appointment, AppointmentSlot, Doctor
from app.seed import seed_database


def test_health_and_dashboard_are_available(client):
    assert client.get("/health").json() == {"status": "healthy"}
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "MediVoice assistant" in dashboard.text
    assert "appointment-lookup-form" in dashboard.text


def test_appointment_lookup_requires_matching_id_and_phone(client):
    availability = client.get(
        "/doctors/availability",
        params={"speciality": "Cardiology", "date": "2099-01-02"},
    ).json()
    slot = availability["doctors"][0]["available_slots"][0]
    booking = client.post(
        "/appointments/book",
        json={
            "doctor_id": 1,
            "slot_id": slot["slot_id"],
            "patient_phone": "15552345678",
        },
    ).json()

    lookup = client.post(
        "/appointments/lookup",
        json={
            "appointment_id": booking["appointment_id"],
            "patient_phone": "15552345678",
        },
    )
    assert lookup.status_code == 200
    assert lookup.json()["appointment_id"] == booking["appointment_id"]
    assert lookup.json()["doctor_name"] == "Dr. Test"

    wrong_phone = client.post(
        "/appointments/lookup",
        json={
            "appointment_id": booking["appointment_id"],
            "patient_phone": "15550000000",
        },
    )
    assert wrong_phone.status_code == 404

    client.post(
        f"/appointments/{booking['appointment_id']}/cancel",
        json={"patient_phone": "15552345678"},
    )
    cancelled = client.post(
        "/appointments/lookup",
        json={
            "appointment_id": booking["appointment_id"],
            "patient_phone": "15552345678",
        },
    )
    assert cancelled.status_code == 404


def test_availability_accepts_doctor_title_and_spoken_relative_date(client, monkeypatch):
    from app.agent.tools import check_doctor_availability

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2099, 1, 1, 12, 0)

    monkeypatch.setattr("app.agent.tools.datetime", FixedDateTime)
    result = check_doctor_availability("cardiologist frio", "tommorrow")

    assert result["success"] is True
    assert result["date"] == "2099-01-02"
    assert result["doctors"][0]["doctor_name"] == "Dr. Test"

    dashboard_search = client.get(
        "/doctors/availability",
        params={"speciality": "cardiologist", "date": "2099-01-02"},
    )
    assert dashboard_search.status_code == 200
    assert dashboard_search.json()["doctors"][0]["doctor_name"] == "Dr. Test"


def test_availability_booking_double_booking_and_cancellation(client):
    availability = client.get(
        "/doctors/availability",
        params={"speciality": "cardiology", "date": "2099-01-02"},
    )
    assert availability.status_code == 200
    slot = availability.json()["doctors"][0]["available_slots"][0]

    booking = client.post(
        "/appointments/book",
        json={
            "doctor_id": 1,
            "slot_id": slot["slot_id"],
            "patient_phone": "+1 (555) 234-5678",
        },
    )
    assert booking.status_code == 200
    booking_data = booking.json()
    assert booking_data["status"] == "booked"
    assert booking_data["patient_phone"] == "15552345678"

    duplicate = client.post(
        "/appointments/book",
        json={
            "doctor_id": 1,
            "slot_id": slot["slot_id"],
            "patient_phone": "15552345678",
        },
    )
    assert duplicate.status_code == 409

    wrong_phone = client.post(
        f"/appointments/{booking_data['appointment_id']}/cancel",
        json={"patient_phone": "15550000000"},
    )
    assert wrong_phone.status_code == 403

    cancelled = client.post(
        f"/appointments/{booking_data['appointment_id']}/cancel",
        json={"patient_phone": "15552345678"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    repeated_cancel = client.post(
        f"/appointments/{booking_data['appointment_id']}/cancel",
        json={"patient_phone": "15552345678"},
    )
    assert repeated_cancel.status_code == 409
    cancellation_retry = cancel_service(
        appointment_id=booking_data["appointment_id"],
        patient_phone="15552345678",
        idempotent=True,
    )
    assert cancellation_retry["success"] is True
    assert cancellation_retry["status"] == "cancelled"

    available_again = client.get(
        "/doctors/availability",
        params={"speciality": "Cardiology", "date": "2099-01-02"},
    )
    assert len(available_again.json()["doctors"][0]["available_slots"]) == 1

    db = client.app.state.test_session()
    stored = db.query(Appointment).filter_by(id=booking_data["appointment_id"]).one()
    assert stored.status == "cancelled"
    assert stored.patient_phone == "15552345678"
    db.close()


def test_invalid_phone_and_date_are_rejected(client):
    invalid_booking = client.post(
        "/appointments/book",
        json={"doctor_id": 1, "slot_id": 1, "patient_phone": "123"},
    )
    assert invalid_booking.status_code == 422

    invalid_date = client.get(
        "/doctors/availability",
        params={"speciality": "Cardiology", "date": "not-a-date"},
    )
    assert invalid_date.status_code == 422


def test_sqlite_foreign_keys_reject_orphan_appointments(client):
    db = client.app.state.test_session()
    db.add(Appointment(
        doctor_id=999,
        slot_id=1,
        patient_phone="15552345678",
        status="booked",
    ))

    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.close()


def test_chat_rejects_multiple_pending_confirmation_tokens(client):
    response = client.post(
        "/agent/chat",
        json={
            "messages": [{"role": "user", "content": "Yes"}],
            "pending_booking_id": "b" * 24,
            "pending_cancellation_id": "c" * 24,
        },
    )

    assert response.status_code == 422


def test_seed_is_idempotent_and_creates_demo_data(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("app.seed.engine", engine)
    monkeypatch.setattr("app.seed.SessionLocal", factory)

    seed_database()
    seed_database()

    db = factory()
    assert db.query(Doctor).count() == 4
    assert db.query(AppointmentSlot).count() == 168
    db.close()
    engine.dispose()


def test_seed_fills_missing_future_slots_without_changing_existing_bookings(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("app.seed.engine", engine)
    monkeypatch.setattr("app.seed.SessionLocal", factory)
    Base.metadata.create_all(bind=engine)

    tomorrow = datetime.now().replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    ) + timedelta(days=1)
    db = factory()
    doctor = Doctor(name="Dr. Existing", speciality="Cardiology", consultation_minutes=30)
    db.add(doctor)
    db.flush()
    booked_slot = AppointmentSlot(
        doctor_id=doctor.id,
        start_time=tomorrow.replace(hour=9),
        end_time=tomorrow.replace(hour=9, minute=30),
        is_booked=True,
    )
    db.add(booked_slot)
    db.flush()
    existing_appointment = Appointment(
        doctor_id=doctor.id,
        slot_id=booked_slot.id,
        patient_phone="15551234567",
        status="booked",
    )
    db.add(existing_appointment)
    db.flush()
    appointment_id = existing_appointment.id
    slot_id = booked_slot.id
    db.commit()
    db.close()

    seed_database()

    db = factory()
    assert db.query(Doctor).count() == 1
    assert db.query(AppointmentSlot).count() == 42
    assert db.query(Appointment).filter_by(id=appointment_id).one().status == "booked"
    assert db.query(AppointmentSlot).filter_by(id=slot_id).one().is_booked is True
    db.close()
    engine.dispose()
