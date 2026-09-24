from datetime import date, datetime
from pathlib import Path
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .database import get_db
from .models import ManualDebt, MonthlyCharge, Payment, PaymentAudit, User
from .public_models import PublicRentalPayment
from .core import member_monthly_fee, refresh_late_fees

BASE_DIR = Path(__file__).resolve().parent
router = APIRouter()
templates = Jinja2Templates(directory=BASE_DIR / "templates")
ALLOWED_METHODS = ("Efectivo", "Transferencia", "Tarjeta", "Otro")


def require_admin(request: Request, db: Session):
    uid = request.session.get("user_id")
    user = db.get(User, uid) if uid else None
    if not user or user.role != "admin" or not user.active:
        raise HTTPException(403)
    return user


def cancelled(db: Session, kind: str, payment_id: int):
    return db.query(PaymentAudit).filter(
        PaymentAudit.payment_kind == kind,
        PaymentAudit.payment_id == payment_id,
        PaymentAudit.action == "cancelled",
    ).first()


def audit_snapshot(payment):
    return json.dumps({
        "amount": round(float(payment.amount or 0), 2),
        "method": payment.method or "",
        "reference": payment.reference or "",
        "concept": getattr(payment, "concept", "") or "",
        "payment_type": getattr(payment, "payment_type", "") or "",
        "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
    }, ensure_ascii=False)


def clean_reason(value: str):
    value = (value or "").strip()
    if len(value) < 3:
        raise HTTPException(400, "Escribe el motivo de la corrección.")
    return value[:500]


def normalized_method(value: str):
    value = (value or "").strip()
    if value not in ALLOWED_METHODS:
        raise HTTPException(400, "Método de pago inválido.")
    return value


def render_page(request: Request, db: Session):
    text_query = (request.query_params.get("q") or "").strip().lower()
    date_query = (request.query_params.get("fecha") or "").strip()
    selected_date = None
    if date_query:
        try:
            selected_date = date.fromisoformat(date_query)
        except ValueError:
            raise HTTPException(400, "La fecha no es válida.")

    audits = db.query(PaymentAudit).order_by(PaymentAudit.created_at.desc(), PaymentAudit.id.desc()).all()
    cancelled_keys = {(a.payment_kind, a.payment_id) for a in audits if a.action == "cancelled"}
    rows = []

    for p in db.query(Payment).order_by(Payment.paid_at.desc(), Payment.id.desc()).all():
        member = p.member
        person = f"{member.first_name} {member.last_name}".strip() if member else "Socio"
        number = member.member_number if member else ""
        haystack = " ".join([p.folio or "", person, number, p.concept or "", p.method or "", p.reference or ""]).lower()
        if text_query and text_query not in haystack:
            continue
        if selected_date and (not p.paid_at or p.paid_at.date() != selected_date):
            continue
        rows.append({
            "kind":"member","id":p.id,"paid_at":p.paid_at,"person":person,"identifier":number,
            "folio":p.folio,"concept":p.concept,"amount":round(float(p.amount or 0),2),
            "method":p.method,"reference":p.reference or "","cancelled":("member",p.id) in cancelled_keys,
        })

    for p in db.query(PublicRentalPayment).order_by(PublicRentalPayment.paid_at.desc(), PublicRentalPayment.id.desc()).all():
        rental = p.rental
        person = rental.client_name if rental else "Cliente"
        hall = rental.hall_name if rental else "Salón"
        event = rental.event_date.strftime("%d/%m/%Y") if rental and rental.event_date else ""
        concept = ("Renta" if p.payment_type == "renta" else "Depósito en garantía") + f" · {hall}"
        haystack = " ".join([person,hall,event,concept,p.method or "",p.reference or "",str(p.id)]).lower()
        if text_query and text_query not in haystack:
            continue
        if selected_date and (not p.paid_at or p.paid_at.date() != selected_date):
            continue
        rows.append({
            "kind":"public","id":p.id,"paid_at":p.paid_at,"person":person,"identifier":f"Renta #{p.rental_id}",
            "folio":p.reference or f"Movimiento #{p.id}","concept":concept,"amount":round(float(p.amount or 0),2),
            "method":p.method,"reference":p.reference or "","cancelled":("public",p.id) in cancelled_keys,
        })

    rows.sort(key=lambda row:(row["paid_at"] or datetime.min,row["id"]),reverse=True)
    admin_ids={a.admin_user_id for a in audits if a.admin_user_id}
    admins={u.id:u.email for u in db.query(User).filter(User.id.in_(admin_ids)).all()} if admin_ids else {}
    history=[{
        "created_at":a.created_at,"kind":a.payment_kind,"payment_id":a.payment_id,
        "action":a.action,"reason":a.reason,
        "admin":admins.get(a.admin_user_id, f"Admin #{a.admin_user_id}" if a.admin_user_id else "Administrador"),
    } for a in audits[:150]]

    return templates.TemplateResponse("payment_corrections.html", {
        "request":request,"rows":rows[:500],"history":history,"q":request.query_params.get("q",""),
        "fecha":date_query,"saved":request.query_params.get("guardado")=="1",
        "cancelled_ok":request.query_params.get("anulado")=="1",
    })


@router.get("/admin/correccion-pagos", response_class=HTMLResponse)
def corrections_page(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    return render_page(request, db)


@router.post("/admin/correccion-pagos/socio/{payment_id}/editar")
def edit_member_payment(payment_id:int, request:Request, method:str=Form(...), reference:str=Form(""), reason:str=Form(...), db:Session=Depends(get_db)):
    admin=require_admin(request,db); payment=db.get(Payment,payment_id)
    if not payment: raise HTTPException(404)
    if cancelled(db,"member",payment.id): raise HTTPException(400,"Este pago ya está anulado.")
    before=audit_snapshot(payment); reason=clean_reason(reason)
    payment.method=normalized_method(method); payment.reference=(reference or "").strip()[:120]
    db.add(PaymentAudit(payment_kind="member",payment_id=payment.id,action="edited",reason=reason,before_data=before,after_data=audit_snapshot(payment),admin_user_id=admin.id))
    db.commit()
    return RedirectResponse("/admin/correccion-pagos?guardado=1",303)


@router.post("/admin/correccion-pagos/socio/{payment_id}/anular")
def cancel_member_payment(payment_id:int, request:Request, reason:str=Form(...), db:Session=Depends(get_db)):
    admin=require_admin(request,db); payment=db.get(Payment,payment_id)
    if not payment: raise HTTPException(404)
    if cancelled(db,"member",payment.id): raise HTTPException(400,"Este pago ya está anulado.")
    reason=clean_reason(reason); before=audit_snapshot(payment)

    for allocation in payment.debt_allocations or []:
        amount=max(0.0,round(float(allocation.amount or 0),2))
        if allocation.target_type=="monthly":
            charge=db.get(MonthlyCharge,allocation.target_id)
            if charge:
                charge.paid_amount=max(0.0,round(float(charge.paid_amount or 0)-amount,2))
                if "Cuota especial $" in (payment.concept or "") and charge.paid_amount<=0:
                    charge.base_amount=member_monthly_fee(db,payment.member_id); charge.late_fee=0
        elif allocation.target_type=="manual":
            debt=db.get(ManualDebt,allocation.target_id)
            if debt: debt.paid_amount=max(0.0,round(float(debt.paid_amount or 0)-amount,2))

    if payment.pool_pass: db.delete(payment.pool_pass)
    db.add(PaymentAudit(payment_kind="member",payment_id=payment.id,action="cancelled",reason=reason,before_data=before,after_data='{"status":"ANULADO"}',admin_user_id=admin.id))
    refresh_late_fees(db); db.commit()
    return RedirectResponse("/admin/correccion-pagos?anulado=1",303)


@router.post("/admin/correccion-pagos/publico/{payment_id}/editar")
def edit_public_payment(payment_id:int, request:Request, method:str=Form(...), reference:str=Form(""), reason:str=Form(...), db:Session=Depends(get_db)):
    admin=require_admin(request,db); payment=db.get(PublicRentalPayment,payment_id)
    if not payment: raise HTTPException(404)
    if cancelled(db,"public",payment.id): raise HTTPException(400,"Este movimiento ya está anulado.")
    before=audit_snapshot(payment); reason=clean_reason(reason)
    payment.method=normalized_method(method); payment.reference=(reference or "").strip()[:160]
    db.add(PaymentAudit(payment_kind="public",payment_id=payment.id,action="edited",reason=reason,before_data=before,after_data=audit_snapshot(payment),admin_user_id=admin.id))
    db.commit()
    return RedirectResponse("/admin/correccion-pagos?guardado=1",303)


@router.post("/admin/correccion-pagos/publico/{payment_id}/anular")
def cancel_public_payment(payment_id:int, request:Request, reason:str=Form(...), db:Session=Depends(get_db)):
    admin=require_admin(request,db); payment=db.get(PublicRentalPayment,payment_id)
    if not payment: raise HTTPException(404)
    if cancelled(db,"public",payment.id): raise HTTPException(400,"Este movimiento ya está anulado.")
    reason=clean_reason(reason); before=audit_snapshot(payment)
    db.add(PaymentAudit(payment_kind="public",payment_id=payment.id,action="cancelled",reason=reason,before_data=before,after_data='{"status":"ANULADO"}',admin_user_id=admin.id))
    db.flush()
    if payment.payment_type=="garantia" and payment.rental:
        cancelled_ids={pid for (pid,) in db.query(PaymentAudit.payment_id).filter(PaymentAudit.payment_kind=="public",PaymentAudit.action=="cancelled").all()}
        remaining=sum(float(p.amount or 0) for p in payment.rental.payments if p.payment_type=="garantia" and p.id not in cancelled_ids)
        payment.rental.deposit_status="En resguardo" if remaining>0 else "Pendiente"
    db.commit()
    return RedirectResponse("/admin/correccion-pagos?anulado=1",303)
