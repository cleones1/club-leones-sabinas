from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from datetime import date
import io

from reportlab.pdfgen import canvas
from reportlab.lib.units import mm

from .database import get_db, SessionLocal
from .models import ClubSetting, MonthlyCharge, Payment
from .core import auth_user, member_billing_type, period_label

router = APIRouter()

REGULAR_BREAKDOWN = [
    ("Cuota", 500.00),
    ("Rifa y servicios", 30.00),
    ("Cuota Damas", 40.00),
    ("Cuota Internacional", 150.00),
    ("Servicios sociales", 40.00),
    ("Cuota Alberca", 150.00),
    ("Cuota Lote 50", 10.00),
]

JUBILADO_BREAKDOWN = [
    ("Cuota", 250.00),
    ("Cuota Internacional", 150.00),
    ("Alberca", 50.00),
    ("Cuota Lote 50", 10.00),
]

REGULAR_TOTAL = round(sum(v for _, v in REGULAR_BREAKDOWN), 2)  # 920
JUBILADO_TOTAL = round(sum(v for _, v in JUBILADO_BREAKDOWN), 2)  # 460
MIGRATION_KEY = "dues_breakdown_2026_v1"


def _set_setting(db: Session, key: str, value: str):
    row = db.query(ClubSetting).filter(ClubSetting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(ClubSetting(key=key, value=value))


def apply_dues_structure_once():
    """Apply the photographed 2026 dues structure only once.

    Existing paid charges are never changed. For the current month, only an
    unpaid charge is aligned to the new fixed monthly amount.
    """
    db = SessionLocal()
    try:
        if db.query(ClubSetting).filter(ClubSetting.key == MIGRATION_KEY).first():
            return

        _set_setting(db, "monthly_fee_amount", f"{REGULAR_TOTAL:.2f}")
        _set_setting(db, "monthly_fee_pensionado_amount", f"{JUBILADO_TOTAL:.2f}")
        _set_setting(db, MIGRATION_KEY, "1")

        today = date.today()
        current_period = f"{today.year:04d}-{today.month:02d}"
        charges = db.query(MonthlyCharge).filter(MonthlyCharge.period == current_period).all()
        for charge in charges:
            if round(charge.paid_amount or 0, 2) != 0:
                continue
            kind = member_billing_type(db, charge.member_id)
            charge.base_amount = JUBILADO_TOTAL if kind == "Pensionado" else REGULAR_TOTAL
            if today <= charge.due_date:
                charge.late_fee = 0
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# app.main imports this module after app.core has finished loading.
apply_dues_structure_once()


def _wrap(text: str, limit: int = 37):
    words = str(text or "—").replace("·", " · ").split()
    lines, current = [], ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                lines.append(current)
            while len(word) > limit:
                lines.append(word[:limit])
                word = word[limit:]
            current = word
    if current:
        lines.append(current)
    return lines or ["—"]


def _plan_from_payment(payment: Payment):
    text = f"{payment.concept or ''} {payment.reference or ''}".lower()
    if "anual" in text:
        return "Anual", 12
    if "trimestral" in text:
        return "Trimestral", 3
    return "Mensual", 1


@router.get("/recibo-cuota/{payment_id}.pdf")
def dues_receipt(payment_id: int, request: Request, db: Session = Depends(get_db)):
    user = auth_user(request, db)
    payment = db.get(Payment, payment_id)
    if not payment:
        raise HTTPException(404)
    if not user or (user.role != "admin" and user.member_id != payment.member_id):
        raise HTTPException(403)
    if not (payment.concept or "").startswith("Cuota de socio"):
        raise HTTPException(400, "Este recibo especial sólo corresponde a cuotas de socio.")

    member = payment.member
    kind = member_billing_type(db, member.id)
    is_retired = kind == "Pensionado"
    breakdown = JUBILADO_BREAKDOWN if is_retired else REGULAR_BREAKDOWN
    monthly_total = JUBILADO_TOTAL if is_retired else REGULAR_TOTAL
    member_label = "Socio jubilado" if is_retired else "Socio normal"
    plan_name, inferred_months = _plan_from_payment(payment)

    monthly_allocations = [a for a in (payment.debt_allocations or []) if a.target_type == "monthly"]
    months = len(monthly_allocations) or inferred_months
    months = max(1, months)

    expected = round(monthly_total * months, 2)
    adjustment = round(payment.amount - expected, 2)

    period_lines = []
    if monthly_allocations:
        labels = []
        for alloc in monthly_allocations:
            target = db.get(MonthlyCharge, alloc.target_id)
            if target:
                labels.append(period_label(target.period))
        if labels:
            if len(labels) == 1:
                period_lines = [labels[0]]
            else:
                period_lines = [f"{labels[0]} a {labels[-1]}"]

    ticket_width = 80 * mm
    margin = 5 * mm
    extra_lines = len(breakdown) + (1 if abs(adjustment) >= 0.01 else 0)
    ticket_height = max(175 * mm, (145 + extra_lines * 14 + len(period_lines) * 10) * 1.0)

    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(ticket_width, ticket_height))
    c.setTitle(f"Recibo cuota {payment.folio}")
    w, h = ticket_width, ticket_height
    y = h - 8 * mm

    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(w / 2, y, "CLUB DE LEONES")
    y -= 12
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(w / 2, y, "DE SABINAS")
    y -= 12
    c.setFont("Helvetica", 8)
    c.drawCentredString(w / 2, y, "RECIBO DE CUOTA DE SOCIO")
    y -= 11
    c.line(margin, y, w - margin, y)
    y -= 14

    c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(w / 2, y, f"Folio: {payment.folio}")
    y -= 15

    details = [
        ("Fecha", payment.paid_at.strftime("%d/%m/%Y %H:%M")),
        ("Socio", f"{member.first_name} {member.last_name}"),
        ("Número", member.member_number),
        ("Tipo", member_label),
        ("Periodicidad", f"{plan_name} · {months} mes(es)"),
    ]
    if period_lines:
        details.append(("Periodo cubierto", period_lines[0]))
    if payment.reference:
        details.append(("Referencia", payment.reference))

    for label, value in details:
        c.setFont("Helvetica-Bold", 7.2)
        c.drawString(margin, y, label.upper())
        y -= 9
        c.setFont("Helvetica", 8)
        for line in _wrap(value):
            c.drawString(margin, y, line)
            y -= 9
        y -= 3

    c.line(margin, y, w - margin, y)
    y -= 12
    c.setFont("Helvetica-Bold", 8)
    c.drawString(margin, y, "DESGLOSE DE CUOTA MENSUAL")
    y -= 12

    c.setFont("Helvetica", 7.7)
    for label, unit in breakdown:
        if months == 1:
            c.drawString(margin, y, label[:27])
            c.drawRightString(w - margin, y, f"${unit:,.2f}")
        else:
            c.drawString(margin, y, label[:23])
            c.drawRightString(w - margin, y, f"${unit:,.2f} x{months} = ${unit * months:,.2f}")
        y -= 11

    y -= 2
    c.setFont("Helvetica-Bold", 8)
    c.drawString(margin, y, "BASE MENSUAL")
    c.drawRightString(w - margin, y, f"${monthly_total:,.2f}")
    y -= 12

    if abs(adjustment) >= 0.01:
        c.setFont("Helvetica", 7.3)
        label = "Recargos / ajustes" if adjustment > 0 else "Abonos previos / ajuste"
        c.drawString(margin, y, label)
        c.drawRightString(w - margin, y, f"${adjustment:,.2f}")
        y -= 12

    c.line(margin, y, w - margin, y)
    y -= 13
    c.setFont("Helvetica-Bold", 9)
    c.drawString(margin, y, "TOTAL PAGADO")
    c.setFont("Helvetica-Bold", 15)
    c.drawRightString(w - margin, y - 1, f"${payment.amount:,.2f}")
    y -= 18
    c.line(margin, y, w - margin, y)

    y -= 18
    half = w / 2
    c.setLineWidth(0.6)
    c.line(margin, y, half - 5 * mm, y)
    c.line(half + 5 * mm, y, w - margin, y)
    y -= 10
    c.setFont("Helvetica", 6.5)
    c.drawCentredString((margin + half - 5 * mm) / 2, y, "Caja")
    c.drawCentredString((half + 5 * mm + w - margin) / 2, y, "Socio")
    y -= 18
    c.setFont("Helvetica", 6.2)
    c.drawCentredString(w / 2, y, "Comprobante generado por el sistema")
    y -= 8
    c.drawCentredString(w / 2, y, "Club de Leones de Sabinas")
    y -= 8
    c.setFont("Helvetica-Bold", 6)
    c.drawCentredString(w / 2, y, "Conserve este ticket como comprobante")

    c.showPage()
    c.save()
    out.seek(0)
    return StreamingResponse(
        out,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="recibo_cuota_{payment.folio}.pdf"'},
    )
