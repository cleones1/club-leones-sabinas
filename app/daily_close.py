from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .database import get_db
from .models import DebtPaymentAllocation, ManualDebt, Member, MonthlyCharge, Payment, User
from .public_models import PublicRentalPayment

BASE_DIR = Path(__file__).resolve().parent
router = APIRouter()
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# El sistema guarda paid_at con datetime.utcnow() sin zona.
# Coahuila usa UTC-6; convertimos los límites del corte a UTC para que
# los cobros cercanos a medianoche queden en el día local correcto.
LOCAL_TZ = timezone(timedelta(hours=-6))

SPANISH_MONTHS = (
    "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
)


def require_admin(request: Request, db: Session):
    uid = request.session.get("user_id")
    user = db.get(User, uid) if uid else None
    if not user or user.role != "admin" or not user.active:
        raise HTTPException(403)
    return user


def period_label(period: str):
    try:
        year, month = [int(x) for x in (period or "").split("-")]
        return f"{SPANISH_MONTHS[month]} {year}"
    except Exception:
        return period or "—"


def local_bounds(selected: date):
    local_start = datetime(selected.year, selected.month, selected.day, tzinfo=LOCAL_TZ)
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(timezone.utc).replace(tzinfo=None),
        local_end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def local_time_text(value: datetime | None):
    if not value:
        return "—"
    utc_value = value.replace(tzinfo=timezone.utc)
    return utc_value.astimezone(LOCAL_TZ).strftime("%H:%M")


def optional_dues_charges(concept: str):
    found = {"Coronación": 0.0, "Posada": 0.0}
    for piece in (concept or "").split("·"):
        item = piece.strip()
        for label in ("Coronación", "Posada"):
            prefix = f"{label} $"
            if item.startswith(prefix):
                try:
                    found[label] = max(0.0, round(float(item[len(prefix):].strip()), 2))
                except (TypeError, ValueError):
                    pass
    return found


def income_category(concept: str):
    text = (concept or "").strip().lower()
    if text.startswith("renta "):
        return "Renta a socio"
    if text.startswith("venta a socio"):
        return "Venta a socio"
    if text.startswith("actividad damas y leones"):
        return "Actividad Damas y Leones"
    if text.startswith("cargo extra"):
        return "Cargo adicional"
    if "coronación" in text or "coronacion" in text:
        return "Coronación"
    if "posada" in text:
        return "Posada"
    return "Otro ingreso"


def _member_name(payment: Payment):
    member = payment.member
    if not member:
        return "Socio"
    return f"{member.first_name} {member.last_name}".strip()


def _member_number(payment: Payment):
    return payment.member.member_number if payment.member else "—"


def build_daily_close(db: Session, selected: date):
    start_utc, end_utc = local_bounds(selected)

    member_payments = (
        db.query(Payment)
        .filter(Payment.paid_at >= start_utc, Payment.paid_at < end_utc)
        .order_by(Payment.paid_at.asc(), Payment.id.asc())
        .all()
    )
    public_payments = (
        db.query(PublicRentalPayment)
        .filter(PublicRentalPayment.paid_at >= start_utc, PublicRentalPayment.paid_at < end_utc)
        .order_by(PublicRentalPayment.paid_at.asc(), PublicRentalPayment.id.asc())
        .all()
    )

    dues_rows = []
    other_rows = []
    guarantee_rows = []
    method_totals = {}

    def add_method(method, amount):
        method = (method or "Sin método").strip() or "Sin método"
        method_totals[method] = round(method_totals.get(method, 0.0) + amount, 2)

    for payment in member_payments:
        payment_amount = max(0.0, round(float(payment.amount or 0), 2))
        if payment_amount <= 0:
            continue

        add_method(payment.method, payment_amount)

        allocations = list(payment.debt_allocations or [])
        monthly_total = 0.0
        manual_total = 0.0
        periods = []

        for allocation in allocations:
            part = max(0.0, round(float(allocation.amount or 0), 2))
            if part <= 0:
                continue

            if allocation.target_type == "monthly":
                charge = db.get(MonthlyCharge, allocation.target_id)
                monthly_total = round(monthly_total + part, 2)
                if charge and charge.period and charge.period not in periods:
                    periods.append(charge.period)
            elif allocation.target_type == "manual":
                debt = db.get(ManualDebt, allocation.target_id)
                manual_total = round(manual_total + part, 2)
                concept = debt.concept if debt else "Adeudo anterior"
                other_rows.append({
                    "time": local_time_text(payment.paid_at),
                    "category": income_category(concept),
                    "person": _member_name(payment),
                    "member_number": _member_number(payment),
                    "concept": concept,
                    "folio": payment.folio,
                    "method": payment.method,
                    "amount": part,
                })

        extras = optional_dues_charges(payment.concept)
        extras_total = 0.0
        for label in ("Coronación", "Posada"):
            amount = min(
                max(0.0, round(payment_amount - monthly_total - manual_total - extras_total, 2)),
                extras[label],
            )
            if amount > 0:
                extras_total = round(extras_total + amount, 2)
                other_rows.append({
                    "time": local_time_text(payment.paid_at),
                    "category": label,
                    "person": _member_name(payment),
                    "member_number": _member_number(payment),
                    "concept": label,
                    "folio": payment.folio,
                    "method": payment.method,
                    "amount": amount,
                })

        # Los pagos normales de cuota tienen asignaciones mensuales.
        # Se mantiene un respaldo para recibos antiguos que no las tuvieran.
        if monthly_total <= 0 and (payment.concept or "").startswith("Cuota de socio"):
            monthly_total = max(
                0.0,
                round(payment_amount - manual_total - extras_total, 2),
            )

        if monthly_total > 0:
            labels = [period_label(p) for p in sorted(periods)]
            period_text = ", ".join(labels)
            if not period_text:
                period_text = (payment.reference or "").strip() or "Cuota de socio"
            dues_rows.append({
                "time": local_time_text(payment.paid_at),
                "member": _member_name(payment),
                "member_number": _member_number(payment),
                "periods": period_text,
                "folio": payment.folio,
                "method": payment.method,
                "amount": monthly_total,
            })

        accounted = round(monthly_total + manual_total + extras_total, 2)
        remainder = max(0.0, round(payment_amount - accounted, 2))

        # Si no hubo asignaciones ni cuota, el pago completo es otro ingreso.
        if not allocations and not (payment.concept or "").startswith("Cuota de socio"):
            remainder = payment_amount

        if remainder > 0.009:
            other_rows.append({
                "time": local_time_text(payment.paid_at),
                "category": income_category(payment.concept),
                "person": _member_name(payment),
                "member_number": _member_number(payment),
                "concept": payment.concept or "Otro ingreso",
                "folio": payment.folio,
                "method": payment.method,
                "amount": remainder,
            })

    for payment in public_payments:
        amount = max(0.0, round(float(payment.amount or 0), 2))
        if amount <= 0:
            continue

        rental = payment.rental
        person = rental.client_name if rental else "Cliente"
        hall = rental.hall_name if rental else "Salón"
        event_text = rental.event_date.strftime("%d/%m/%Y") if rental and rental.event_date else "—"

        if payment.payment_type == "garantia":
            guarantee_rows.append({
                "time": local_time_text(payment.paid_at),
                "person": person,
                "concept": f"Depósito en garantía · {hall} · Evento {event_text}",
                "reference": payment.reference or "—",
                "method": payment.method,
                "amount": amount,
            })
        else:
            add_method(payment.method, amount)
            other_rows.append({
                "time": local_time_text(payment.paid_at),
                "category": "Renta salón al público",
                "person": person,
                "member_number": "Público",
                "concept": f"{hall} · Evento {event_text}",
                "folio": payment.reference or f"Renta #{payment.rental_id}",
                "method": payment.method,
                "amount": amount,
            })

    dues_total = round(sum(row["amount"] for row in dues_rows), 2)
    other_total = round(sum(row["amount"] for row in other_rows), 2)
    guarantee_total = round(sum(row["amount"] for row in guarantee_rows), 2)
    total_income = round(dues_total + other_total, 2)

    return {
        "dues_rows": dues_rows,
        "other_rows": other_rows,
        "guarantee_rows": guarantee_rows,
        "dues_total": dues_total,
        "other_total": other_total,
        "guarantee_total": guarantee_total,
        "total_income": total_income,
        "method_totals": sorted(method_totals.items(), key=lambda x: x[0].lower()),
        "payment_count": len(member_payments) + sum(1 for p in public_payments if p.payment_type != "garantia"),
    }


@router.get("/admin/corte-dia", response_class=HTMLResponse)
def daily_close(request: Request, fecha: str = "", db: Session = Depends(get_db)):
    require_admin(request, db)
    if fecha:
        try:
            selected = date.fromisoformat(fecha)
        except ValueError:
            raise HTTPException(400, "La fecha del corte no es válida.")
    else:
        selected = datetime.now(LOCAL_TZ).date()

    close = build_daily_close(db, selected)
    return templates.TemplateResponse("daily_close.html", {
        "request": request,
        "selected_date": selected,
        "selected_iso": selected.isoformat(),
        "selected_label": selected.strftime("%d/%m/%Y"),
        **close,
    })
