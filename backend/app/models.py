from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Boolean,
    ForeignKey,
)
from sqlalchemy.orm import relationship

from app.database import Base


class Doctor(Base):
    __tablename__ = "doctors"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    speciality = Column(String, nullable=False)
    consultation_minutes = Column(Integer, default=20)

    slots = relationship("AppointmentSlot", back_populates="doctor")
    appointments = relationship("Appointment", back_populates="doctor")


class AppointmentSlot(Base):
    __tablename__ = "appointment_slots"

    id = Column(Integer, primary_key=True, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    is_booked = Column(Boolean, default=False)

    doctor = relationship("Doctor", back_populates="slots")
    appointment = relationship("Appointment", back_populates="slot")


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False)
    slot_id = Column(Integer, ForeignKey("appointment_slots.id"), nullable=False)
    patient_phone = Column(String, nullable=False, index=True)
    status = Column(String, default="booked")
    created_at = Column(DateTime, default=datetime.utcnow)

    doctor = relationship("Doctor", back_populates="appointments")
    slot = relationship("AppointmentSlot", back_populates="appointment")