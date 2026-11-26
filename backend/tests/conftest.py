from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, enable_sqlite_foreign_keys
from app.dependencies import get_db
from app.main import app


@pytest.fixture
def client(monkeypatch):
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    event.listen(test_engine, "connect", enable_sqlite_foreign_keys)
    test_session = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    monkeypatch.setattr("app.main.engine", test_engine)
    monkeypatch.setattr("app.main.seed_database", lambda: None)
    monkeypatch.setattr("app.dependencies.SessionLocal", test_session)
    monkeypatch.setattr("app.agent.tools.SessionLocal", test_session)

    def override_get_db():
        db = test_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.state.test_session = test_session
    app.state.test_engine = test_engine

    with TestClient(app) as test_client:
        db = test_session()
        doctor = models.Doctor(
            name="Dr. Test",
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
        yield test_client

    app.dependency_overrides.clear()
    test_engine.dispose()

