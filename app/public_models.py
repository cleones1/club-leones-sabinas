from datetime import datetime

from sqlalchemy import Column, Integer, String, Date, DateTime, Float, ForeignKey, Text
from sqlalchemy.orm import relationship

from .database import Base


class PublicRental(Base):
    __tablename__ = "public_rentals"
    id = Column(Integer, primary_key=True)
    client_name = Column(String(180), nullable=False)
    phone = Column(String(40), default="")
    email = Column(String(180), default="")
    hall_key = Column(String(40), nullable=False, index=True)
    hall_name = Column(String(120), nullable=False)
    event_date = Column(Date, nullable=False, index=True)
    rental_price = Column(Float, nullable=False, default=0)
    deposit_required = Column(Float, nullable=False, default=0)
    deposit_status = Column(String(30), nullable=False, default="Pendiente")
    status = Column(String(20), nullable=False, default="Activa")
    notes = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    payments = relationship(
        "PublicRentalPayment",
        back_populates="rental",
        cascade="all, delete-orphan",
        order_by="PublicRentalPayment.id",
    )


class PublicRentalPayment(Base):
    __tablename__ = "public_rental_payments"
    id = Column(Integer, primary_key=True)
    rental_id = Column(Integer, ForeignKey("public_rentals.id"), nullable=False, index=True)
    payment_type = Column(String(20), nullable=False)  # renta / garantia
    amount = Column(Float, nullable=False)
    method = Column(String(60), nullable=False, default="Efectivo")
    reference = Column(String(160), default="")
    paid_at = Column(DateTime, default=datetime.utcnow)
    rental = relationship("PublicRental", back_populates="payments")
