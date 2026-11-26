from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app import models
from app.agent import tools
from app.database import Base
from app.models import Appointment, AppointmentSlot


def test_concurrent_bookings_claim_one_slot_once(monkeypatch, tmp_path):
    database_path = tmp_path / "concurrent-booking.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = factory()
    doctor = models.Doctor(
        name="Dr. Concurrent",
        speciality="Cardiology",
        consultation_minutes=30,
    )
    db.add(doctor)
    db.flush()
    db.add(models.AppointmentSlot(
        doctor_id=doctor.id,
        start_time=datetime(2099, 1, 2, 9),
        end_time=datetime(2099, 1, 2, 9, 30),
        is_booked=False,
    ))
    db.commit()
    db.close()
    monkeypatch.setattr(tools, "SessionLocal", factory)

    update_barrier = Barrier(2)

    def synchronize_slot_updates(
        connection,
        cursor,
        statement,
        parameters,
        context,
        executemany,
    ):
        if statement.lstrip().upper().startswith("UPDATE APPOINTMENT_SLOTS"):
            update_barrier.wait(timeout=5)

    event.listen(engine, "before_cursor_execute", synchronize_slot_updates)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda _: tools.book_appointment(
                    doctor_id=1,
                    slot_id=1,
                    patient_phone="15552345678",
                ),
                range(2),
            ))
    finally:
        event.remove(engine, "before_cursor_execute", synchronize_slot_updates)

    assert sum(result["success"] for result in results) == 1
    assert sorted(result.get("code") for result in results if not result["success"]) == [
        "unavailable",
    ]

    db = factory()
    assert db.query(Appointment).count() == 1
    assert db.query(AppointmentSlot).one().is_booked is True
    db.close()
    engine.dispose()


def test_failed_booking_commit_rolls_back_slot_claim(client):
    engine = client.app.state.test_engine

    def fail_appointment_insert(
        connection,
        cursor,
        statement,
        parameters,
        context,
        executemany,
    ):
        if statement.lstrip().upper().startswith("INSERT INTO APPOINTMENTS"):
            raise OperationalError(
                statement,
                parameters,
                RuntimeError("simulated database failure"),
            )

    event.listen(engine, "before_cursor_execute", fail_appointment_insert)
    try:
        result = tools.book_appointment(
            doctor_id=1,
            slot_id=1,
            patient_phone="15552345678",
        )
    finally:
        event.remove(engine, "before_cursor_execute", fail_appointment_insert)

    assert result["success"] is False
    db = client.app.state.test_session()
    assert db.query(Appointment).count() == 0
    assert db.query(AppointmentSlot).one().is_booked is False
    db.close()


def test_failed_cancellation_commit_preserves_appointment_and_slot(client):
    booking = tools.book_appointment(
        doctor_id=1,
        slot_id=1,
        patient_phone="15552345678",
    )
    assert booking["success"] is True
    engine = client.app.state.test_engine

    def fail_slot_release(
        connection,
        cursor,
        statement,
        parameters,
        context,
        executemany,
    ):
        if statement.lstrip().upper().startswith("UPDATE APPOINTMENT_SLOTS"):
            raise OperationalError(
                statement,
                parameters,
                RuntimeError("simulated database failure"),
            )

    event.listen(engine, "before_cursor_execute", fail_slot_release)
    try:
        result = tools.cancel_appointment(
            appointment_id=booking["appointment_id"],
            patient_phone="15552345678",
        )
    finally:
        event.remove(engine, "before_cursor_execute", fail_slot_release)

    assert result["success"] is False
    db = client.app.state.test_session()
    assert db.query(Appointment).one().status == "booked"
    assert db.query(AppointmentSlot).one().is_booked is True
    db.close()
