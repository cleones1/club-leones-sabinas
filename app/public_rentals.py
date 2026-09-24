from datetime import date
from pathlib import Path

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from .database import get_db
from .models import ClubSetting, PaymentAudit, User
from .public_models import PublicRental, PublicRentalPayment
from .pricing_tools import PUBLIC_RENTAL_PRICES, ensure_price_settings

BASE_DIR = Path(__file__).resolve().parent
router = APIRouter()
templates = Jinja2Templates(directory=BASE_DIR / "templates")

PUBLIC_HALLS = [
    {"key":"fundadores","name":"Salón Fundadores","price_key":"price_public_fundadores","deposit_key":"deposit_public_fundadores"},
    {"key":"damas","name":"Salón Damas","price_key":"price_public_damas","deposit_key":"deposit_public_damas"},
    {"key":"presidentes","name":"Salón Presidentes","price_key":"price_public_presidentes","deposit_key":"deposit_public_presidentes"},
]


def require_admin(request: Request, db: Session):
    uid = request.session.get("user_id")
    user = db.get(User, uid) if uid else None
    if not user or user.role != "admin" or not user.active:
        raise HTTPException(403)
    return user


def setting_amount(db: Session, key: str, default: float = 0.0):
    row = db.query(ClubSetting).filter(ClubSetting.key == key).first()
    try:
        return round(float(row.value), 2) if row else round(float(default), 2)
    except (TypeError, ValueError):
        return round(float(default), 2)


def hall_definition(key: str):
    return next((h for h in PUBLIC_HALLS if h["key"] == (key or "").strip().lower()), None)


def cancelled_public_ids(db: Session):
    return {
        payment_id for (payment_id,) in db.query(PaymentAudit.payment_id).filter(
            PaymentAudit.payment_kind == "public",
            PaymentAudit.action == "cancelled",
        ).all()
    }


def rent_paid(db: Session, rental: PublicRental):
    cancelled = cancelled_public_ids(db)
    return round(sum((p.amount or 0) for p in rental.payments if p.payment_type == "renta" and p.id not in cancelled), 2)


def deposit_paid(db: Session, rental: PublicRental):
    cancelled = cancelled_public_ids(db)
    return round(sum((p.amount or 0) for p in rental.payments if p.payment_type == "garantia" and p.id not in cancelled), 2)


def rent_balance(db: Session, rental: PublicRental):
    return max(0.0, round((rental.rental_price or 0) - rent_paid(db, rental), 2))


def deposit_balance(db: Session, rental: PublicRental):
    return max(0.0, round((rental.deposit_required or 0) - deposit_paid(db, rental), 2))


def rental_rows(db: Session, rentals):
    rows = []
    for rental in rentals:
        rows.append({
            "rental": rental,
            "rent_paid": rent_paid(db, rental),
            "rent_balance": rent_balance(db, rental),
            "deposit_paid": deposit_paid(db, rental),
            "deposit_balance": deposit_balance(db, rental),
        })
    return rows


def render_page(request: Request, db: Session, error: str = ""):
    ensure_price_settings(db)
    halls = []
    for hall in PUBLIC_HALLS:
        halls.append({**hall,"price":setting_amount(db,hall["price_key"]),"deposit":setting_amount(db,hall["deposit_key"])})
    rentals = db.query(PublicRental).order_by(PublicRental.event_date.desc(), PublicRental.id.desc()).all()
    active = sum(1 for r in rentals if r.status == "Activa")
    cancelled = cancelled_public_ids(db)
    rental_income = round(sum(
        float(p.amount or 0)
        for p in db.query(PublicRentalPayment).filter(PublicRentalPayment.payment_type == "renta").all()
        if p.id not in cancelled
    ), 2)
    held_deposits = 0.0
    for r in rentals:
        if r.deposit_status == "En resguardo":
            held_deposits += deposit_paid(db, r)
    return templates.TemplateResponse("public_rentals.html", {
        "request":request,"halls":halls,"rows":rental_rows(db, rentals),"active_count":active,
        "rental_income":round(float(rental_income),2),"held_deposits":round(float(held_deposits),2),
        "cancelled_public_ids":cancelled,
        "saved":request.query_params.get("creada")=="1","payment_saved":request.query_params.get("pago")=="1",
        "deposit_updated":request.query_params.get("deposito")=="1","cancelled":request.query_params.get("cancelada")=="1",
        "error":error,
    })


@router.get("/admin/rentas-publico", response_class=HTMLResponse)
def public_rentals_page(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    return render_page(request, db)


@router.post("/admin/rentas-publico/nueva")
def create_public_rental(
    request: Request,
    client_name: str = Form(...), phone: str = Form(""), email: str = Form(""), hall_key: str = Form(...),
    event_date: str = Form(...), rental_price: float = Form(0), deposit_required: float = Form(0),
    initial_rent_payment: float = Form(0), initial_deposit_payment: float = Form(0),
    method: str = Form("Efectivo"), reference: str = Form(""), notes: str = Form(""),
    db: Session = Depends(get_db),
):
    require_admin(request, db)
    ensure_price_settings(db)
    hall = hall_definition(hall_key)
    if not hall:
        return render_page(request, db, "Selecciona un salón válido.")
    name = (client_name or "").strip()
    if not name:
        return render_page(request, db, "Captura el nombre de la persona que renta.")
    try:
        event_day = date.fromisoformat((event_date or "").strip())
    except ValueError:
        return render_page(request, db, "La fecha del evento no es válida.")
    rental_price = round(float(rental_price or 0), 2)
    deposit_required = round(float(deposit_required or 0), 2)
    initial_rent_payment = round(float(initial_rent_payment or 0), 2)
    initial_deposit_payment = round(float(initial_deposit_payment or 0), 2)
    if min(rental_price, deposit_required, initial_rent_payment, initial_deposit_payment) < 0:
        return render_page(request, db, "Los importes no pueden ser negativos.")
    if initial_rent_payment > rental_price:
        return render_page(request, db, "El pago inicial de renta no puede exceder el precio de renta.")
    if initial_deposit_payment > deposit_required:
        return render_page(request, db, "El depósito recibido no puede exceder el depósito requerido.")
    occupied = db.query(PublicRental).filter(PublicRental.hall_key==hall["key"], PublicRental.event_date==event_day, PublicRental.status=="Activa").first()
    if occupied:
        return render_page(request, db, f'{hall["name"]} ya tiene una renta activa para esa fecha.')
    rental = PublicRental(
        client_name=name, phone=(phone or "").strip(), email=(email or "").strip().lower(),
        hall_key=hall["key"], hall_name=hall["name"], event_date=event_day,
        rental_price=rental_price, deposit_required=deposit_required,
        deposit_status="En resguardo" if initial_deposit_payment > 0 else "Pendiente",
        status="Activa", notes=(notes or "").strip(),
    )
    db.add(rental); db.flush()
    method = (method or "Efectivo").strip() or "Efectivo"
    reference = (reference or "").strip()
    if initial_rent_payment > 0:
        db.add(PublicRentalPayment(rental_id=rental.id,payment_type="renta",amount=initial_rent_payment,method=method,reference=reference))
    if initial_deposit_payment > 0:
        db.add(PublicRentalPayment(rental_id=rental.id,payment_type="garantia",amount=initial_deposit_payment,method=method,reference=reference))
    db.commit()
    return RedirectResponse("/admin/rentas-publico?creada=1", 303)


@router.post("/admin/rentas-publico/{rental_id}/pago")
def add_public_rental_payment(
    rental_id: int, request: Request, payment_type: str = Form(...), amount: float = Form(...),
    method: str = Form("Efectivo"), reference: str = Form(""), db: Session = Depends(get_db),
):
    require_admin(request, db)
    rental = db.get(PublicRental, rental_id)
    if not rental: raise HTTPException(404)
    if rental.status != "Activa": return render_page(request, db, "La renta está cancelada y ya no acepta movimientos.")
    kind = (payment_type or "").strip().lower()
    if kind not in ("renta", "garantia"): return render_page(request, db, "Tipo de pago inválido.")
    amount = round(float(amount or 0), 2)
    if amount <= 0: return render_page(request, db, "El importe debe ser mayor a cero.")
    balance = rent_balance(db, rental) if kind == "renta" else deposit_balance(db, rental)
    if amount > balance + 0.001:
        return render_page(request, db, f'El pago excede el saldo pendiente de {"renta" if kind=="renta" else "depósito en garantía"}.')
    if kind == "garantia" and rental.deposit_status in ("Devuelto", "Retenido"):
        return render_page(request, db, "El depósito ya fue cerrado como devuelto o retenido.")
    db.add(PublicRentalPayment(rental_id=rental.id,payment_type=kind,amount=amount,method=(method or "Efectivo").strip() or "Efectivo",reference=(reference or "").strip()))
    if kind == "garantia": rental.deposit_status = "En resguardo"
    db.commit()
    return RedirectResponse("/admin/rentas-publico?pago=1", 303)


@router.post("/admin/rentas-publico/{rental_id}/deposito")
def update_deposit_status(rental_id: int, request: Request, deposit_status: str = Form(...), db: Session = Depends(get_db)):
    require_admin(request, db)
    rental = db.get(PublicRental, rental_id)
    if not rental: raise HTTPException(404)
    allowed = ("Pendiente", "En resguardo", "Devuelto", "Retenido")
    status = (deposit_status or "").strip()
    if status not in allowed: return render_page(request, db, "Estatus de depósito inválido.")
    if status in ("Devuelto", "Retenido") and deposit_paid(db, rental) <= 0:
        return render_page(request, db, "No hay un depósito recibido para marcarlo como devuelto o retenido.")
    rental.deposit_status = status
    db.commit()
    return RedirectResponse("/admin/rentas-publico?deposito=1", 303)


@router.post("/admin/rentas-publico/{rental_id}/cancelar")
def cancel_public_rental(rental_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    rental = db.get(PublicRental, rental_id)
    if not rental: raise HTTPException(404)
    rental.status = "Cancelada"
    db.commit()
    return RedirectResponse("/admin/rentas-publico?cancelada=1", 303)
