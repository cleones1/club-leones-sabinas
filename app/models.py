from sqlalchemy import Column, Integer, String, Date, DateTime, Float, ForeignKey, Boolean, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime
from .database import Base


class Member(Base):
    __tablename__ = "members"
    id = Column(Integer, primary_key=True)
    member_number = Column(String(30), unique=True, index=True, nullable=False)
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(160), nullable=False)
    email = Column(String(180), unique=True, index=True, nullable=False)
    phone = Column(String(40), default="")
    birth_date = Column(Date, nullable=True)
    address = Column(Text, default="")
    emergency_contact = Column(String(180), default="")
    notes = Column(Text, default="")
    photo_url = Column(String(500), default="")
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    qr_token = Column(String(80), unique=True, index=True, nullable=False)
    memberships = relationship("Membership", back_populates="member", cascade="all, delete-orphan")
    payments = relationship("Payment", back_populates="member", cascade="all, delete-orphan")
    user = relationship("User", back_populates="member", uselist=False, cascade="all, delete-orphan")
    photo = relationship("MemberPhoto", back_populates="member", uselist=False, cascade="all, delete-orphan")
    family_members = relationship("FamilyMember", back_populates="member", cascade="all, delete-orphan", order_by="FamilyMember.id")
    pool_passes = relationship("PoolPass", back_populates="member", cascade="all, delete-orphan")
    monthly_charges = relationship("MonthlyCharge", back_populates="member", cascade="all, delete-orphan")
    manual_debts = relationship("ManualDebt", back_populates="member", cascade="all, delete-orphan")
    billing_profile = relationship("MemberBillingProfile", back_populates="member", uselist=False, cascade="all, delete-orphan")


class MemberPhoto(Base):
    __tablename__ = "member_photos"
    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey("members.id"), unique=True, nullable=False)
    data = Column(Text, nullable=False)
    member = relationship("Member", back_populates="photo")


class FamilyMember(Base):
    __tablename__ = "family_members"
    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False, index=True)
    relationship_type = Column(String(40), nullable=False)
    full_name = Column(String(180), nullable=False)
    birth_date = Column(Date, nullable=True)
    photo_data = Column(Text, default="")
    qr_token = Column(String(80), unique=True, index=True, nullable=False)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    member = relationship("Member", back_populates="family_members")


class Membership(Base):
    __tablename__ = "memberships"
    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    membership_type = Column(String(100), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    amount = Column(Float, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    member = relationship("Member", back_populates="memberships")


class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    folio = Column(String(40), unique=True, index=True, nullable=False)
    concept = Column(String(160), nullable=False)
    amount = Column(Float, nullable=False)
    method = Column(String(60), nullable=False)
    reference = Column(String(120), default="")
    paid_at = Column(DateTime, default=datetime.utcnow)
    member = relationship("Member", back_populates="payments")
    pool_pass = relationship("PoolPass", back_populates="payment", uselist=False)
    debt_allocations = relationship("DebtPaymentAllocation", back_populates="payment", cascade="all, delete-orphan")


class PoolPass(Base):
    __tablename__ = "pool_passes"
    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False, index=True)
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=True, unique=True)
    plan_type = Column(String(20), nullable=False)  # Mensual / Anual
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    amount = Column(Float, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    member = relationship("Member", back_populates="pool_passes")
    payment = relationship("Payment", back_populates="pool_pass")


class ClubSetting(Base):
    __tablename__ = "club_settings"
    id = Column(Integer, primary_key=True)
    key = Column(String(80), unique=True, index=True, nullable=False)
    value = Column(String(255), default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MemberBillingProfile(Base):
    __tablename__ = "member_billing_profiles"
    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey("members.id"), unique=True, nullable=False, index=True)
    member_type = Column(String(20), nullable=False, default="Regular")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    member = relationship("Member", back_populates="billing_profile")


class MonthlyCharge(Base):
    __tablename__ = "monthly_charges"
    __table_args__ = (UniqueConstraint("member_id", "period", name="uq_monthly_charge_member_period"),)
    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False, index=True)
    period = Column(String(7), nullable=False, index=True)  # YYYY-MM
    base_amount = Column(Float, nullable=False, default=0)
    late_fee = Column(Float, nullable=False, default=0)
    paid_amount = Column(Float, nullable=False, default=0)
    due_date = Column(Date, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    member = relationship("Member", back_populates="monthly_charges")


class ManualDebt(Base):
    __tablename__ = "manual_debts"
    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False, index=True)
    concept = Column(String(180), nullable=False)
    amount = Column(Float, nullable=False)
    paid_amount = Column(Float, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    member = relationship("Member", back_populates="manual_debts")


class DebtPaymentAllocation(Base):
    __tablename__ = "debt_payment_allocations"
    id = Column(Integer, primary_key=True)
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=False, index=True)
    target_type = Column(String(20), nullable=False)  # monthly / manual
    target_id = Column(Integer, nullable=False)
    amount = Column(Float, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    payment = relationship("Payment", back_populates="debt_allocations")


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(180), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default="member")
    member_id = Column(Integer, ForeignKey("members.id"), nullable=True)
    active = Column(Boolean, default=True)
    member = relationship("Member", back_populates="user")
