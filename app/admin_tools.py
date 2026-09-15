from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, func
from pathlib import Path

from .database import get_db
from .models import (
    Member, MemberPhoto, FamilyMember, Membership, Payment, PoolPass,
    ClubSetting, MemberBillingProfile, MonthlyCharge, ManualDebt,
    DebtPaymentAllocation, User,
)
from .auth import hash_password

BASE_DIR = Path(__file__).resolve().parent
router = APIRouter()
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def require_admin(request: Request, db: Session):
    uid = request.session.get("user_id")
    user = db.get(User, uid) if uid else None
    if not user or user.role != "admin" or not user.active:
        raise HTTPException(403)
    return user


def database_stats(db: Session):
    return {
        "Socios": db.query(Member).count(),
        "Administradores": db.query(User).filter(User.role == "admin").count(),
        "Usuarios de socios": db.query(User).filter(User.role == "member").count(),
        "Pagos": db.query(Payment).count(),
        "Mensualidades": db.query(MonthlyCharge).count(),
        "Adeudos manuales": db.query(ManualDebt).count(),
        "Asignaciones de pagos": db.query(DebtPaymentAllocation).count(),
        "Familiares": db.query(FamilyMember).count(),
        "Membresías": db.query(Membership).count(),
        "Pases de alberca": db.query(PoolPass).count(),
    }


def orphan_counts(db: Session):
    member_ids = db.query(Member.id)
    payment_ids = db.query(Payment.id)
    monthly_ids = db.query(MonthlyCharge.id)
    manual_ids = db.query(ManualDebt.id)

    allocations_bad_target = db.query(DebtPaymentAllocation).filter(or_(
        and_(DebtPaymentAllocation.target_type == "monthly", ~DebtPaymentAllocation.target_id.in_(monthly_ids)),
        and_(DebtPaymentAllocation.target_type == "manual", ~DebtPaymentAllocation.target_id.in_(manual_ids)),
        ~DebtPaymentAllocation.target_type.in_(["monthly", "manual"]),
    )).count()

    return {
        "Fotos sin socio": db.query(MemberPhoto).filter(~MemberPhoto.member_id.in_(member_ids)).count(),
        "Familiares sin socio": db.query(FamilyMember).filter(~FamilyMember.member_id.in_(member_ids)).count(),
        "Membresías sin socio": db.query(Membership).filter(~Membership.member_id.in_(member_ids)).count(),
        "Perfiles de cobro sin socio": db.query(MemberBillingProfile).filter(~MemberBillingProfile.member_id.in_(member_ids)).count(),
        "Mensualidades sin socio": db.query(MonthlyCharge).filter(~MonthlyCharge.member_id.in_(member_ids)).count(),
        "Adeudos sin socio": db.query(ManualDebt).filter(~ManualDebt.member_id.in_(member_ids)).count(),
        "Pases de alberca sin socio": db.query(PoolPass).filter(~PoolPass.member_id.in_(member_ids)).count(),
        "Pagos sin socio": db.query(Payment).filter(~Payment.member_id.in_(member_ids)).count(),
        "Usuarios de socio sin socio": db.query(User).filter(User.role == "member", User.member_id.isnot(None), ~User.member_id.in_(member_ids)).count(),
        "Asignaciones sin pago": db.query(DebtPaymentAllocation).filter(~DebtPaymentAllocation.payment_id.in_(payment_ids)).count(),
        "Asignaciones sin destino": allocations_bad_target,
    }


def render_settings(request: Request, db: Session, current: User, error: str = ""):
    admins = db.query(User).filter(User.role == "admin").order_by(User.id.asc()).all()
    orphans = orphan_counts(db)
    return templates.TemplateResponse("admin_settings.html", {
        "request": request,
        "current": current,
        "admins": admins,
        "stats": database_stats(db),
        "orphans": orphans,
        "orphan_total": sum(orphans.values()),
        "created": request.query_params.get("creado") == "1",
        "updated": request.query_params.get("actualizado") == "1",
        "cleaned": request.query_params.get("limpieza") == "1",
        "cleaned_count": request.query_params.get("eliminados", "0"),
        "error": error,
    })


@router.get("/admin/configuracion", response_class=HTMLResponse)
def admin_settings(request: Request, db: Session = Depends(get_db)):
    current = require_admin(request, db)
    return render_settings(request, db, current)


@router.post("/admin/configuracion/administradores/nuevo")
def create_admin(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    current = require_admin(request, db)
    normalized = (email or "").strip().lower()
    if not normalized or "@" not in normalized:
        return render_settings(request, db, current, "Ingresa un correo válido para el administrador.")
    if len(password or "") < 8:
        return render_settings(request, db, current, "La contraseña debe tener al menos 8 caracteres.")
    if db.query(User).filter(func.lower(User.email) == normalized).first():
        return render_settings(request, db, current, "Ya existe un usuario con ese correo.")
    db.add(User(email=normalized, password_hash=hash_password(password), role="admin", member_id=None, active=True))
    db.commit()
    return RedirectResponse("/admin/configuracion?creado=1", 303)


@router.post("/admin/configuracion/administradores/{user_id}/toggle")
def toggle_admin(user_id: int, request: Request, db: Session = Depends(get_db)):
    current = require_admin(request, db)
    target = db.get(User, user_id)
    if not target or target.role != "admin":
        raise HTTPException(404)
    if target.id == current.id and target.active:
        return render_settings(request, db, current, "No puedes desactivar tu propio acceso mientras estás usando el sistema.")
    if target.active:
        active_admins = db.query(User).filter(User.role == "admin", User.active == True).count()
        if active_admins <= 1:
            return render_settings(request, db, current, "Debe quedar al menos un administrador activo.")
    target.active = not target.active
    db.commit()
    return RedirectResponse("/admin/configuracion?actualizado=1", 303)


@router.post("/admin/configuracion/administradores/{user_id}/password")
def reset_admin_password(
    user_id: int,
    request: Request,
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    current = require_admin(request, db)
    target = db.get(User, user_id)
    if not target or target.role != "admin":
        raise HTTPException(404)
    if len(password or "") < 8:
        return render_settings(request, db, current, "La nueva contraseña debe tener al menos 8 caracteres.")
    target.password_hash = hash_password(password)
    db.commit()
    return RedirectResponse("/admin/configuracion?actualizado=1", 303)


@router.post("/admin/configuracion/base-datos/limpieza-segura")
def safe_database_cleanup(
    request: Request,
    confirmation: str = Form(""),
    db: Session = Depends(get_db),
):
    current = require_admin(request, db)
    if (confirmation or "").strip().upper() != "LIMPIAR":
        return render_settings(request, db, current, "Escribe LIMPIAR para confirmar la limpieza segura.")

    member_ids = db.query(Member.id)
    payment_ids = db.query(Payment.id)
    monthly_ids = db.query(MonthlyCharge.id)
    manual_ids = db.query(ManualDebt.id)
    removed = 0

    # Primero elimina referencias técnicas huérfanas. Nunca elimina un socio,
    # un pago válido ni un adeudo que aún pertenezca a un socio existente.
    removed += db.query(DebtPaymentAllocation).filter(~DebtPaymentAllocation.payment_id.in_(payment_ids)).delete(synchronize_session=False)
    removed += db.query(DebtPaymentAllocation).filter(or_(
        and_(DebtPaymentAllocation.target_type == "monthly", ~DebtPaymentAllocation.target_id.in_(monthly_ids)),
        and_(DebtPaymentAllocation.target_type == "manual", ~DebtPaymentAllocation.target_id.in_(manual_ids)),
        ~DebtPaymentAllocation.target_type.in_(["monthly", "manual"]),
    )).delete(synchronize_session=False)

    removed += db.query(PoolPass).filter(~PoolPass.member_id.in_(member_ids)).delete(synchronize_session=False)
    removed += db.query(MemberPhoto).filter(~MemberPhoto.member_id.in_(member_ids)).delete(synchronize_session=False)
    removed += db.query(FamilyMember).filter(~FamilyMember.member_id.in_(member_ids)).delete(synchronize_session=False)
    removed += db.query(Membership).filter(~Membership.member_id.in_(member_ids)).delete(synchronize_session=False)
    removed += db.query(MemberBillingProfile).filter(~MemberBillingProfile.member_id.in_(member_ids)).delete(synchronize_session=False)
    removed += db.query(MonthlyCharge).filter(~MonthlyCharge.member_id.in_(member_ids)).delete(synchronize_session=False)
    removed += db.query(ManualDebt).filter(~ManualDebt.member_id.in_(member_ids)).delete(synchronize_session=False)
    removed += db.query(Payment).filter(~Payment.member_id.in_(member_ids)).delete(synchronize_session=False)
    removed += db.query(User).filter(User.role == "member", User.member_id.isnot(None), ~User.member_id.in_(member_ids)).delete(synchronize_session=False)

    db.commit()
    return RedirectResponse(f"/admin/configuracion?limpieza=1&eliminados={removed}", 303)
