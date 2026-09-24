from fastapi import FastAPI, Request, Depends, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Optional
import io, os, secrets, base64, calendar, math
import qrcode
from PIL import Image, ImageOps
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader

from .database import Base, engine, get_db, SessionLocal
from .models import Member, MemberPhoto, FamilyMember, Membership, Payment, PoolPass, ClubSetting, MemberBillingProfile, MonthlyCharge, ManualDebt, DebtPaymentAllocation, PaymentAudit, User
from .public_models import PublicRental, PublicRentalPayment
from .auth import hash_password, verify_password

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Club de Leones de Sabinas")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET") or secrets.token_urlsafe(32), same_site="lax")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Crea tablas nuevas sin eliminar ni alterar los datos existentes.
Base.metadata.create_all(bind=engine)


def seed_admin():
    db = SessionLocal()
    try:
        if not db.query(User).filter(User.role == "admin").first():
            admin_email = os.getenv("ADMIN_EMAIL", "admin@club.local").strip().lower()
            admin_password = os.getenv("ADMIN_PASSWORD")
            if admin_password:
                db.add(User(email=admin_email, password_hash=hash_password(admin_password), role="admin", active=True))
                db.commit()
    finally:
        db.close()


seed_admin()


def member_status(member: Member):
    # La membresía del club es permanente. El estatus sólo depende de si el
    # socio está activo o fue suspendido administrativamente.
    if not member.active:
        return "Suspendido", "danger"
    return "Activo", "success"


def latest_membership(member: Member):
    return max(member.memberships, key=lambda m: m.end_date) if member.memberships else None


def latest_pool_pass(member: Member):
    return max(member.pool_passes, key=lambda p: p.end_date) if member.pool_passes else None


def pool_status(member: Member):
    p = latest_pool_pass(member)
    if not member.active:
        return "Suspendido", "danger", p
    if not p:
        return "Sin acceso", "muted", None
    if p.end_date < date.today():
        return "Vencido", "danger", p
    if p.end_date <= date.today() + timedelta(days=7):
        return "Por vencer", "warning", p
    return "Activo", "success", p


SPANISH_MONTHS = ("", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre")


MEMBER_TYPES = ("Regular", "Pensionado")


def normalize_member_type(value: str):
    return "Pensionado" if (value or "").strip().lower() == "pensionado" else "Regular"


def member_billing_type(db: Session, member_id: int):
    row = db.query(MemberBillingProfile).filter(MemberBillingProfile.member_id == member_id).first()
    return normalize_member_type(row.member_type if row else "Regular")


def set_member_billing_type(db: Session, member_id: int, member_type: str):
    kind = normalize_member_type(member_type)
    row = db.query(MemberBillingProfile).filter(MemberBillingProfile.member_id == member_id).first()
    if not row:
        row = MemberBillingProfile(member_id=member_id, member_type=kind)
        db.add(row)
    else:
        row.member_type = kind
    return row


def monthly_fee_amount(db: Session, member_type: str = "Regular"):
    kind = normalize_member_type(member_type)
    key = "monthly_fee_pensionado_amount" if kind == "Pensionado" else "monthly_fee_amount"
    row = db.query(ClubSetting).filter(ClubSetting.key == key).first()
    try:
        return round(float(row.value), 2) if row else 0.0
    except (TypeError, ValueError):
        return 0.0


def set_monthly_fee_amount(db: Session, amount: float, member_type: str = "Regular"):
    kind = normalize_member_type(member_type)
    key = "monthly_fee_pensionado_amount" if kind == "Pensionado" else "monthly_fee_amount"
    row = db.query(ClubSetting).filter(ClubSetting.key == key).first()
    if not row:
        row = ClubSetting(key=key, value=f"{amount:.2f}")
        db.add(row)
    else:
        row.value = f"{amount:.2f}"
    return row


def member_monthly_fee(db: Session, member_id: int):
    return monthly_fee_amount(db, member_billing_type(db, member_id))


def month_start_from_period(value: str):
    value = (value or "").strip()
    if not value:
        today = date.today()
        return date(today.year, today.month, 1)
    try:
        year, month = [int(x) for x in value.split("-")]
        return date(year, month, 1)
    except Exception:
        raise HTTPException(400, "El mes inicial no es válido.")


def shift_months(start: date, months: int):
    idx = start.year * 12 + (start.month - 1) + months
    return date(idx // 12, (idx % 12) + 1, 1)


def monthly_due_date(year: int, month: int):
    # El socio puede liquidar hasta el día 30 inclusive. En febrero se usa
    # el último día disponible y el recargo se activa al día siguiente.
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(30, last_day))


def monthly_charge_total(charge: MonthlyCharge):
    return round((charge.base_amount or 0) + (charge.late_fee or 0), 2)


def monthly_charge_balance(charge: MonthlyCharge):
    return max(0.0, round(monthly_charge_total(charge) - (charge.paid_amount or 0), 2))


def manual_debt_balance(debt: ManualDebt):
    return max(0.0, round((debt.amount or 0) - (debt.paid_amount or 0), 2))


def member_debt_total(db: Session, member_id: int):
    monthly = db.query(MonthlyCharge).filter(MonthlyCharge.member_id == member_id).all()
    manual = db.query(ManualDebt).filter(ManualDebt.member_id == member_id).all()
    return round(sum(monthly_charge_balance(c) for c in monthly) + sum(manual_debt_balance(d) for d in manual), 2)


def ensure_monthly_charges(db: Session, target_day: date | None = None, update_current_amount: bool = False):
    target_day = target_day or date.today()
    period = f"{target_day.year:04d}-{target_day.month:02d}"
    due = monthly_due_date(target_day.year, target_day.month)
    members = db.query(Member).all()
    existing = {c.member_id: c for c in db.query(MonthlyCharge).filter(MonthlyCharge.period == period).all()}
    changed = False
    for member in members:
        amount = member_monthly_fee(db, member.id)
        charge = existing.get(member.id)
        if not charge and amount > 0:
            db.add(MonthlyCharge(member_id=member.id, period=period, base_amount=amount, late_fee=0, paid_amount=0, due_date=due))
            changed = True
        elif charge and update_current_amount and (charge.paid_amount or 0) == 0:
            if round(charge.base_amount or 0, 2) != round(amount, 2):
                charge.base_amount = amount
                if target_day <= charge.due_date:
                    charge.late_fee = 0
                changed = True
    return changed


def refresh_late_fees(db: Session, target_day: date | None = None):
    target_day = target_day or date.today()
    changed = False
    charges = db.query(MonthlyCharge).all()
    for charge in charges:
        # Si al terminar el día 30 no está liquidada, se carga 10% de la mensualidad base.
        base_pending = round((charge.base_amount or 0) - (charge.paid_amount or 0), 2)
        if target_day > charge.due_date and base_pending > 0 and (charge.late_fee or 0) == 0:
            charge.late_fee = round((charge.base_amount or 0) * 0.10, 2)
            changed = True
    return changed


def sync_finances(db: Session, update_current_amount: bool = False):
    changed = ensure_monthly_charges(db, update_current_amount=update_current_amount)
    changed = refresh_late_fees(db) or changed
    if changed:
        db.commit()
    return changed


def period_label(period: str):
    try:
        y, m = [int(x) for x in period.split("-")]
        return f"{SPANISH_MONTHS[m]} {y}"
    except Exception:
        return period


INCOME_CATEGORIES = (
    "Cuotas y recargos",
    "Coronación",
    "Posada",
    "Rentas a socios",
    "Rentas de salones al público",
    "Ventas a socios",
    "Actividades Damas y Leones",
    "Cargos adicionales",
    "Otros ingresos",
)


def _optional_dues_charges(concept: str):
    found = {"Coronación": 0.0, "Posada": 0.0}
    for piece in (concept or "").split("·"):
        item = piece.strip()
        for label in ("Coronación", "Posada"):
            prefix = f"{label} $"
            if item.startswith(prefix):
                try:
                    found[label] = round(float(item[len(prefix):].strip()), 2)
                except (TypeError, ValueError):
                    pass
    return found


def _income_category_from_concept(concept: str):
    text = (concept or "").strip().lower()
    if text.startswith("renta "):
        return "Rentas a socios"
    if text.startswith("venta a socio"):
        return "Ventas a socios"
    if text.startswith("actividad damas y leones"):
        return "Actividades Damas y Leones"
    if text.startswith("cargo extra"):
        return "Cargos adicionales"
    if "coronación" in text or "coronacion" in text:
        return "Coronación"
    if "posada" in text:
        return "Posada"
    if text.startswith("cuota de socio") or text.startswith("mensualidad"):
        return "Cuotas y recargos"
    return "Otros ingresos"


def income_breakdown(db: Session, start_at: datetime | None = None, end_at: datetime | None = None):
    totals = {name: 0.0 for name in INCOME_CATEGORIES}
    cancelled_member_ids = db.query(PaymentAudit.payment_id).filter(
        PaymentAudit.payment_kind == "member",
        PaymentAudit.action == "cancelled",
    )
    q = db.query(Payment).filter(~Payment.id.in_(cancelled_member_ids))
    if start_at is not None:
        q = q.filter(Payment.paid_at >= start_at)
    if end_at is not None:
        q = q.filter(Payment.paid_at < end_at)

    for payment in q.all():
        amount = round(float(payment.amount or 0), 2)
        if amount <= 0:
            continue

        # Una cuota puede contener Coronación y/o Posada dentro del mismo recibo.
        if (payment.concept or "").startswith("Cuota de socio"):
            extras = _optional_dues_charges(payment.concept)
            coronacion = min(amount, extras["Coronación"])
            posada = min(max(0.0, amount - coronacion), extras["Posada"])
            allocated_monthly = round(sum(
                float(a.amount or 0)
                for a in (payment.debt_allocations or [])
                if a.target_type == "monthly"
            ), 2)
            dues_amount = allocated_monthly or max(0.0, round(amount - coronacion - posada, 2))
            totals["Cuotas y recargos"] += dues_amount
            totals["Coronación"] += coronacion
            totals["Posada"] += posada
            remainder = round(amount - dues_amount - coronacion - posada, 2)
            if remainder > 0.009:
                totals["Otros ingresos"] += remainder
            continue

        # Los abonos a adeudos se clasifican según el rubro original del saldo.
        allocations = list(payment.debt_allocations or [])
        if allocations:
            allocated = 0.0
            for allocation in allocations:
                part = round(float(allocation.amount or 0), 2)
                if part <= 0:
                    continue
                allocated += part
                if allocation.target_type == "monthly":
                    category = "Cuotas y recargos"
                else:
                    debt = db.get(ManualDebt, allocation.target_id)
                    category = _income_category_from_concept(debt.concept if debt else "")
                totals[category] += part
            remainder = round(amount - allocated, 2)
            if remainder > 0.009:
                totals["Otros ingresos"] += remainder
            continue

        totals[_income_category_from_concept(payment.concept)] += amount

    # Rentas al público: sólo los pagos de renta son ingreso.
    cancelled_public_ids = db.query(PaymentAudit.payment_id).filter(
        PaymentAudit.payment_kind == "public",
        PaymentAudit.action == "cancelled",
    )
    public_q = db.query(PublicRentalPayment).filter(
        PublicRentalPayment.payment_type == "renta",
        ~PublicRentalPayment.id.in_(cancelled_public_ids),
    )
    if start_at is not None:
        public_q = public_q.filter(PublicRentalPayment.paid_at >= start_at)
    if end_at is not None:
        public_q = public_q.filter(PublicRentalPayment.paid_at < end_at)
    totals["Rentas de salones al público"] += round(sum(float(p.amount or 0) for p in public_q.all()), 2)

    return {name: round(value, 2) for name, value in totals.items()}


def guarantee_balances(db: Session):
    held = 0.0
    retained = 0.0
    cancelled_public_ids = {
        payment_id for (payment_id,) in db.query(PaymentAudit.payment_id).filter(
            PaymentAudit.payment_kind == "public",
            PaymentAudit.action == "cancelled",
        ).all()
    }
    rentals = db.query(PublicRental).all()
    for rental in rentals:
        received = round(sum(
            float(p.amount or 0)
            for p in rental.payments
            if p.payment_type == "garantia" and p.id not in cancelled_public_ids
        ), 2)
        if rental.deposit_status == "En resguardo":
            held += received
        elif rental.deposit_status == "Retenido":
            retained += received
    return round(held, 2), round(retained, 2)


def auth_user(request: Request, db: Session):
    uid = request.session.get("user_id")
    return db.get(User, uid) if uid else None


def require_admin(request: Request, db: Session):
    u = auth_user(request, db)
    if not u or u.role != "admin":
        raise HTTPException(403)
    return u


def require_member(request: Request, db: Session):
    u = auth_user(request, db)
    if not u or u.role != "member" or not u.member:
        raise HTTPException(403)
    return u


def parse_optional_date(value: str):
    value = (value or "").strip()
    return date.fromisoformat(value) if value else None


def next_month_end(start: date):
    month = start.month + 1
    year = start.year
    if month == 13:
        month = 1
        year += 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day) - timedelta(days=1)


def annual_end(start: date):
    try:
        return start.replace(year=start.year + 1) - timedelta(days=1)
    except ValueError:
        return date(start.year + 1, 2, 28) - timedelta(days=1)


async def image_to_data_url(upload: Optional[UploadFile]):
    if not upload or not upload.filename:
        return ""
    raw = await upload.read()
    if not raw:
        return ""
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(400, "La fotografía no puede exceder 8 MB.")
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img = ImageOps.exif_transpose(img)
        img.thumbnail((700, 700))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=82, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")
    except Exception:
        raise HTTPException(400, "La fotografía debe ser una imagen válida JPG, PNG o similar.")


def image_reader_from_data_url(data_url: str):
    if not data_url or "," not in data_url:
        return None
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1])
        return ImageReader(io.BytesIO(raw))
    except Exception:
        return None


def draw_photo(c, data_url, x, y, w, h):
    reader = image_reader_from_data_url(data_url)
    if reader:
        c.drawImage(reader, x, y, w, h, preserveAspectRatio=True, anchor="c", mask="auto")
    else:
        c.setFillColorRGB(.90, .92, .95)
        c.roundRect(x, y, w, h, 2*mm, fill=1, stroke=0)
        c.setFillColorRGB(.25, .32, .45)
        c.setFont("Helvetica-Bold", 7)
        c.drawCentredString(x + w/2, y + h/2, "SIN FOTO")


def get_family_slot(member: Member, relationship_type: str):
    return next((f for f in member.family_members if f.relationship_type == relationship_type), None)


async def upsert_family_slot(db: Session, member: Member, relationship_type: str, full_name: str, birth_date: str, photo: Optional[UploadFile]):
    # Consulta directamente la tabla para evitar depender del caché de la relación
    # SQLAlchemy cuando se agregan varios familiares en una misma petición.
    current = db.query(FamilyMember).filter(
        FamilyMember.member_id == member.id,
        FamilyMember.relationship_type == relationship_type,
    ).first()
    name = (full_name or "").strip()
    if not name:
        if current:
            db.delete(current)
        return
    photo_data = await image_to_data_url(photo)
    if current:
        current.full_name = name
        current.birth_date = parse_optional_date(birth_date)
        current.active = True
        if photo_data:
            current.photo_data = photo_data
    else:
        db.add(FamilyMember(
            member_id=member.id,
            relationship_type=relationship_type,
            full_name=name,
            birth_date=parse_optional_date(birth_date),
            photo_data=photo_data,
            qr_token=secrets.token_urlsafe(24),
            active=True,
        ))


def regular_credential_page(c, member: Member, latest, status, base_url: str):
    width, height = 86*mm, 54*mm
    c.setFillColorRGB(0.00,0.20,0.55); c.rect(0,0,width,height,fill=1,stroke=0)
    c.setFillColorRGB(1.00,0.80,0.00); c.rect(0,43*mm,width,11*mm,fill=1,stroke=0)
    c.setFillColorRGB(0.00,0.20,0.55); c.circle(8*mm,48.5*mm,4.1*mm,fill=1,stroke=0)
    c.setFillColorRGB(1,1,1); c.setFont("Helvetica-Bold", 9); c.drawCentredString(8*mm,46.5*mm,"L")
    c.setFillColorRGB(0.00,0.20,0.55); c.setFont("Helvetica-Bold", 8.4); c.drawString(14*mm,47*mm,"CLUB DE LEONES DE SABINAS")
    draw_photo(c, member.photo.data if member.photo else "", 6*mm, 16*mm, 22*mm, 24*mm)
    c.setFillColorRGB(1,1,1); c.setFont("Helvetica-Bold",9); c.drawString(31*mm,35*mm,f"{member.first_name} {member.last_name}"[:30])
    c.setFont("Helvetica",7.5); c.drawString(31*mm,29*mm,f"Socio: {member.member_number}")
    c.drawString(31*mm,24*mm,"Membresía: Permanente")
    c.drawString(31*mm,19*mm,f"Estatus: {status}")
    qr = qrcode.make(f"{base_url}/verificar/{member.qr_token}")
    qbuf = io.BytesIO(); qr.save(qbuf, format="PNG"); qbuf.seek(0)
    c.drawImage(ImageReader(qbuf), 62*mm, 3.5*mm, 19*mm, 19*mm, preserveAspectRatio=True, mask='auto')
    c.setFillColorRGB(1.00,0.80,0.00); c.setFont("Helvetica-Bold",5.2); c.drawString(6*mm,5*mm,"QR de validación de membresía")


def pool_credential_page(c, member: Member, person_name: str, relation: str, token_kind: str, token: str, photo_data: str, p: PoolPass, status: str, base_url: str):
    width, height = 86*mm, 54*mm
    c.setFillColorRGB(0.00,0.32,0.60); c.rect(0,0,width,height,fill=1,stroke=0)
    c.setFillColorRGB(0.15,0.72,0.86); c.rect(0,43*mm,width,11*mm,fill=1,stroke=0)
    c.setFillColorRGB(1,1,1); c.setFont("Helvetica-Bold",9); c.drawString(6*mm,47*mm,"CREDENCIAL DE ALBERCA")
    c.setFont("Helvetica-Bold",6.5); c.drawRightString(80*mm,47*mm,f"{p.plan_type.upper()}")
    draw_photo(c, photo_data, 6*mm, 15*mm, 22*mm, 25*mm)
    c.setFillColorRGB(1,1,1); c.setFont("Helvetica-Bold",9); c.drawString(31*mm,35*mm,person_name[:30])
    c.setFont("Helvetica",7.2); c.drawString(31*mm,29*mm,f"{relation} · Socio {member.member_number}")
    c.drawString(31*mm,24*mm,f"Vigencia: {p.start_date.strftime('%d/%m/%Y')} - {p.end_date.strftime('%d/%m/%Y')}")
    c.drawString(31*mm,19*mm,f"Estatus: {status}")
    qr = qrcode.make(f"{base_url}/verificar-alberca/{token_kind}/{token}")
    qbuf = io.BytesIO(); qr.save(qbuf, format="PNG"); qbuf.seek(0)
    c.drawImage(ImageReader(qbuf), 62*mm, 3.5*mm, 19*mm, 19*mm, preserveAspectRatio=True, mask='auto')
    c.setFillColorRGB(.72,.93,1); c.setFont("Helvetica-Bold",5.2); c.drawString(6*mm,5*mm,"Uso exclusivo de alberca · Validación por QR")


@app.get("/health")
def health():
    return {"status": "ok", "app": "Club de Leones de Sabinas"}


@app.get("/", response_class=HTMLResponse)
def root(request: Request, db: Session = Depends(get_db)):
    u = auth_user(request, db)
    if u:
        return RedirectResponse("/admin" if u.role == "admin" else "/mi-cuenta", 303)
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(func.lower(User.email) == email.strip().lower()).first()
    if not user or not user.active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Correo o contraseña incorrectos."}, status_code=400)
    request.session["user_id"] = user.id
    return RedirectResponse("/admin" if user.role == "admin" else "/mi-cuenta", 303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", 303)


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    sync_finances(db)
    members = db.query(Member).order_by(Member.created_at.desc()).all()
    active = sum(1 for m in members if m.active)
    suspended = len(members) - active
    debt_map = {m.id: member_debt_total(db, m.id) for m in members}
    month_start = date.today().replace(day=1)
    next_month_date = shift_months(month_start, 1)
    income_map = income_breakdown(
        db,
        datetime.combine(month_start, datetime.min.time()),
        datetime.combine(next_month_date, datetime.min.time()),
    )
    income = round(sum(income_map.values()), 2)
    total_debt = round(sum(debt_map.values()), 2)
    return templates.TemplateResponse("admin_dashboard.html", {
        "request": request, "members": members, "status_fn": member_status, "latest_fn": latest_membership,
        "total": len(members), "active": active, "suspended": suspended, "expiring": 0, "expired": 0, "income": income,
        "debt_map": debt_map, "total_debt": total_debt,
        "monthly_fee": monthly_fee_amount(db, "Regular"),
        "monthly_fee_pensionado": monthly_fee_amount(db, "Pensionado"),
        "member_type_map": {m.id: member_billing_type(db, m.id) for m in members},
    })


@app.get("/admin/socios/nuevo", response_class=HTMLResponse)
def new_member_form(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    return templates.TemplateResponse("member_form.html", {"request": request})


@app.post("/admin/socios/nuevo")
async def create_member(
    request: Request,
    member_number: str = Form(""), first_name: str = Form(...), last_name: str = Form(...), email: str = Form(...), phone: str = Form(""), birth_date: str = Form(""),
    address: str = Form(""), emergency_contact: str = Form(""), notes: str = Form(""),
    member_type: str = Form("Regular"),
    membership_type: str = Form("Permanente"), start_date: str = Form(""), end_date: str = Form(""),
    amount: float = Form(0), password: str = Form("Socio123!"),
    spouse_name: str = Form(""), spouse_birth_date: str = Form(""),
    child1_name: str = Form(""), child1_birth_date: str = Form(""), child2_name: str = Form(""), child2_birth_date: str = Form(""),
    child3_name: str = Form(""), child3_birth_date: str = Form(""), child4_name: str = Form(""), child4_birth_date: str = Form(""),
    member_photo: Optional[UploadFile] = File(None), spouse_photo: Optional[UploadFile] = File(None),
    child1_photo: Optional[UploadFile] = File(None), child2_photo: Optional[UploadFile] = File(None),
    child3_photo: Optional[UploadFile] = File(None), child4_photo: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db)
):
    require_admin(request, db)
    if db.query(Member).filter(func.lower(Member.email)==email.strip().lower()).first():
        return templates.TemplateResponse("member_form.html", {"request": request, "error": "Ese correo ya está registrado."}, status_code=400)
    next_id = (db.query(func.max(Member.id)).scalar() or 0) + 1
    number = member_number.strip() or f"SOC-{next_id:05d}"
    if db.query(Member).filter(func.lower(Member.member_number)==number.lower()).first():
        return templates.TemplateResponse("member_form.html", {"request": request, "error": "Ese número interno de socio ya está registrado."}, status_code=400)
    m = Member(member_number=number, first_name=first_name.strip(), last_name=last_name.strip(), email=email.strip().lower(), phone=phone.strip(), birth_date=parse_optional_date(birth_date), address=address.strip(), emergency_contact=emergency_contact.strip(), notes=notes.strip(), qr_token=secrets.token_urlsafe(24))
    db.add(m); db.flush()
    set_member_billing_type(db, m.id, member_type)
    db.flush()
    member_photo_data = await image_to_data_url(member_photo)
    if member_photo_data:
        db.add(MemberPhoto(member_id=m.id, data=member_photo_data))
    join_date = parse_optional_date(start_date) or date.today()
    db.add(Membership(member_id=m.id, membership_type="Permanente", start_date=join_date, end_date=date(2099, 12, 31), amount=0))
    db.add(User(email=m.email, password_hash=hash_password(password), role="member", member_id=m.id, active=True))
    await upsert_family_slot(db, m, "Esposa", spouse_name, spouse_birth_date, spouse_photo)
    await upsert_family_slot(db, m, "Hijo 1", child1_name, child1_birth_date, child1_photo)
    await upsert_family_slot(db, m, "Hijo 2", child2_name, child2_birth_date, child2_photo)
    await upsert_family_slot(db, m, "Hijo 3", child3_name, child3_birth_date, child3_photo)
    await upsert_family_slot(db, m, "Hijo 4", child4_name, child4_birth_date, child4_photo)
    current_fee = member_monthly_fee(db, m.id)
    if current_fee > 0:
        today = date.today()
        db.add(MonthlyCharge(member_id=m.id, period=f"{today.year:04d}-{today.month:02d}", base_amount=current_fee, late_fee=0, paid_amount=0, due_date=monthly_due_date(today.year, today.month)))
    db.commit()
    return RedirectResponse(f"/admin/socios/{m.id}", 303)


@app.get("/admin/socios/{member_id}", response_class=HTMLResponse)
def member_detail(member_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    sync_finances(db)
    m = db.get(Member, member_id)
    if not m: raise HTTPException(404)
    family = db.query(FamilyMember).filter(FamilyMember.member_id == m.id, FamilyMember.active == True).order_by(FamilyMember.id).all()
    family_by_type = {f.relationship_type: f for f in family}
    dues_payment_id = request.query_params.get("cuota_pago")
    return templates.TemplateResponse("member_detail.html", {
        "request": request, "m": m, "status": member_status(m), "latest": latest_membership(m),
        "pool": pool_status(m), "family_slot": get_family_slot,
        "family": family, "family_by_type": family_by_type,
        "family_saved": request.query_params.get("familia") == "guardada",
        "dues_payment_id": int(dues_payment_id) if dues_payment_id and dues_payment_id.isdigit() else None,
        "debt_total": member_debt_total(db, m.id),
        "member_type": member_billing_type(db, m.id),
        "member_monthly_fee": member_monthly_fee(db, m.id),
        "cancelled_payment_ids": {
            payment_id for (payment_id,) in db.query(PaymentAudit.payment_id).filter(
                PaymentAudit.payment_kind == "member",
                PaymentAudit.action == "cancelled",
                PaymentAudit.payment_id.in_([p.id for p in m.payments] or [-1]),
            ).all()
        },
    })


@app.post("/admin/socios/{member_id}/datos")
async def update_member_data(member_id: int, request: Request,
    member_number: str = Form(...), first_name: str = Form(...), last_name: str = Form(...), email: str = Form(...), phone: str = Form(""), birth_date: str = Form(""),
    address: str = Form(""), emergency_contact: str = Form(""), notes: str = Form(""), member_type: str = Form("Regular"), member_photo: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db)):
    require_admin(request, db)
    m = db.get(Member, member_id)
    if not m: raise HTTPException(404)
    number = member_number.strip()
    duplicate = db.query(Member).filter(func.lower(Member.member_number)==number.lower(), Member.id != member_id).first()
    if duplicate:
        raise HTTPException(400, "Ese número interno de socio ya está en uso.")
    email_owner = db.query(Member).filter(func.lower(Member.email)==email.strip().lower(), Member.id != member_id).first()
    if email_owner:
        raise HTTPException(400, "Ese correo ya está en uso.")
    m.member_number = number; m.first_name = first_name.strip(); m.last_name = last_name.strip(); m.email = email.strip().lower(); m.phone = phone.strip(); m.birth_date = parse_optional_date(birth_date); m.address = address.strip(); m.emergency_contact = emergency_contact.strip(); m.notes = notes.strip()
    if m.user: m.user.email = m.email
    set_member_billing_type(db, m.id, member_type)
    photo_data = await image_to_data_url(member_photo)
    if photo_data:
        if m.photo: m.photo.data = photo_data
        else: db.add(MemberPhoto(member_id=m.id, data=photo_data))
    db.commit()
    sync_finances(db, update_current_amount=True)
    return RedirectResponse(f"/admin/socios/{m.id}", 303)


@app.post("/admin/socios/{member_id}/familia")
async def update_family(member_id: int, request: Request,
    spouse_name: str = Form(""), spouse_birth_date: str = Form(""),
    child1_name: str = Form(""), child1_birth_date: str = Form(""), child2_name: str = Form(""), child2_birth_date: str = Form(""),
    child3_name: str = Form(""), child3_birth_date: str = Form(""), child4_name: str = Form(""), child4_birth_date: str = Form(""),
    spouse_photo: Optional[UploadFile] = File(None), child1_photo: Optional[UploadFile] = File(None), child2_photo: Optional[UploadFile] = File(None), child3_photo: Optional[UploadFile] = File(None), child4_photo: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db)):
    require_admin(request, db)
    m = db.get(Member, member_id)
    if not m: raise HTTPException(404)
    await upsert_family_slot(db, m, "Esposa", spouse_name, spouse_birth_date, spouse_photo)
    await upsert_family_slot(db, m, "Hijo 1", child1_name, child1_birth_date, child1_photo)
    await upsert_family_slot(db, m, "Hijo 2", child2_name, child2_birth_date, child2_photo)
    await upsert_family_slot(db, m, "Hijo 3", child3_name, child3_birth_date, child3_photo)
    await upsert_family_slot(db, m, "Hijo 4", child4_name, child4_birth_date, child4_photo)
    db.commit()
    return RedirectResponse(f"/admin/socios/{m.id}?familia=guardada", 303)


@app.post("/admin/socios/{member_id}/membresia")
def add_membership(member_id: int, request: Request, membership_type: str = Form("Permanente"), start_date: str = Form(""), end_date: str = Form(""), amount: float = Form(0), db: Session = Depends(get_db)):
    require_admin(request, db)
    m = db.get(Member, member_id)
    if not m: raise HTTPException(404)
    join_date = parse_optional_date(start_date) or date.today()
    membership = latest_membership(m)
    if membership:
        membership.membership_type = "Permanente"
        membership.start_date = join_date
        membership.end_date = date(2099, 12, 31)
        membership.amount = 0
    else:
        db.add(Membership(member_id=m.id, membership_type="Permanente", start_date=join_date, end_date=date(2099, 12, 31), amount=0))
    db.commit()
    return RedirectResponse(f"/admin/socios/{m.id}", 303)


@app.post("/admin/socios/{member_id}/cuota/pagar")
def pay_member_dues(member_id: int, request: Request, plan: str = Form(...), start_period: str = Form(""), method: str = Form(...), reference: str = Form(""), special_monthly_fee: float = Form(0), coronacion_amount: float = Form(0), posada_amount: float = Form(0), db: Session = Depends(get_db)):
    require_admin(request, db)
    sync_finances(db)
    m = db.get(Member, member_id)
    if not m: raise HTTPException(404)
    plan_months = {"Mensual": 1, "Trimestral": 3, "Anual": 12}
    months_to_cover = plan_months.get((plan or "").strip())
    if not months_to_cover:
        raise HTTPException(400, "Selecciona un plan Mensual, Trimestral o Anual.")
    fee = member_monthly_fee(db, m.id)
    if fee <= 0:
        raise HTTPException(400, "Primero configura la cuota mensual para este tipo de socio.")
    if not math.isfinite(special_monthly_fee or 0):
        raise HTTPException(400, "El importe de cuota especial no es válido.")
    special_fee = round(float(special_monthly_fee or 0), 2)
    if special_fee < 0:
        raise HTTPException(400, "La cuota especial no puede ser negativa.")
    special_fee = special_fee if special_fee > 0 else None
    cursor = month_start_from_period(start_period)
    today = date.today()
    current_month = date(today.year, today.month, 1)
    targets = []
    attempts = 0
    while len(targets) < months_to_cover and attempts < 48:
        period = f"{cursor.year:04d}-{cursor.month:02d}"
        charge = db.query(MonthlyCharge).filter(MonthlyCharge.member_id == m.id, MonthlyCharge.period == period).first()
        if not charge:
            charge = MonthlyCharge(
                member_id=m.id,
                period=period,
                base_amount=special_fee if special_fee is not None else fee,
                late_fee=0,
                paid_amount=0,
                due_date=monthly_due_date(cursor.year, cursor.month),
            )
            db.add(charge); db.flush()
        else:
            # Los periodos ya liquidados se omiten y se continúa al siguiente pendiente.
            if monthly_charge_balance(charge) <= 0:
                cursor = shift_months(cursor, 1)
                attempts += 1
                continue
            if special_fee is not None:
                if round(charge.paid_amount or 0, 2) > 0:
                    raise HTTPException(
                        400,
                        f"No se puede aplicar cuota especial a {period_label(period)} porque ya tiene un abono parcial."
                    )
                charge.base_amount = special_fee
                charge.late_fee = 0
            elif (charge.paid_amount or 0) == 0 and cursor >= current_month:
                charge.base_amount = fee
                charge.late_fee = 0

        base_pending = round((charge.base_amount or 0) - (charge.paid_amount or 0), 2)
        if today > charge.due_date and base_pending > 0 and (charge.late_fee or 0) == 0:
            charge.late_fee = round((charge.base_amount or 0) * 0.10, 2)
        balance = monthly_charge_balance(charge)
        if balance > 0:
            targets.append((charge, balance))
        cursor = shift_months(cursor, 1)
        attempts += 1
    if len(targets) < months_to_cover:
        raise HTTPException(400, "No fue posible determinar todos los periodos de la cuota seleccionada.")
    dues_total = round(sum(balance for _, balance in targets), 2)
    coronacion_amount = round(max(0.0, coronacion_amount or 0), 2)
    posada_amount = round(max(0.0, posada_amount or 0), 2)
    total = round(dues_total + coronacion_amount + posada_amount, 2)
    first_period = targets[0][0].period
    last_period = targets[-1][0].period
    next_id = (db.query(func.max(Payment.id)).scalar() or 0) + 1
    note = f"{plan}: {period_label(first_period)} a {period_label(last_period)}"
    extra = (reference or "").strip()
    if extra:
        note += f" · {extra}"
    concept_parts = [f"Cuota de socio · {plan}"]
    if special_fee is not None:
        concept_parts.append("Cuota especial $" + f"{special_fee:.2f}")
    if coronacion_amount > 0:
        concept_parts.append(f"Coronación ${coronacion_amount:.2f}")
    if posada_amount > 0:
        concept_parts.append(f"Posada ${posada_amount:.2f}")
    payment = Payment(member_id=m.id, folio=f"PAG-{next_id:06d}", concept=" · ".join(concept_parts), amount=total, method=method, reference=note[:120])
    db.add(payment); db.flush()
    for charge, balance in targets:
        charge.paid_amount = round((charge.paid_amount or 0) + balance, 2)
        db.add(DebtPaymentAllocation(payment_id=payment.id, target_type="monthly", target_id=charge.id, amount=balance))

    # El acceso de alberca ya está incluido en la cuota del socio.
    # La vigencia sigue exactamente los meses cubiertos por este pago.
    access_start = month_start_from_period(first_period)
    access_last_month = month_start_from_period(last_period)
    access_end = date(
        access_last_month.year,
        access_last_month.month,
        calendar.monthrange(access_last_month.year, access_last_month.month)[1],
    )
    db.add(PoolPass(
        member_id=m.id,
        payment_id=payment.id,
        plan_type=plan,
        start_date=access_start,
        end_date=access_end,
        amount=0,
    ))
    db.commit()
    return RedirectResponse(f"/admin/socios/{m.id}?cuota_pago={payment.id}", 303)


@app.post("/admin/socios/{member_id}/pago")
def add_payment(member_id: int, request: Request, concept: str = Form(...), amount: float = Form(...), method: str = Form(...), reference: str = Form(""), pool_plan: str = Form(""), pool_start_date: str = Form(""), db: Session = Depends(get_db)):
    require_admin(request, db)
    m = db.get(Member, member_id)
    if not m: raise HTTPException(404)
    next_id = (db.query(func.max(Payment.id)).scalar() or 0) + 1
    payment = Payment(member_id=m.id, folio=f"PAG-{next_id:06d}", concept=concept, amount=amount, method=method, reference=reference)
    db.add(payment); db.flush()
    plan = pool_plan.strip()
    if plan in ("Mensual", "Anual"):
        start = parse_optional_date(pool_start_date) or date.today()
        end = next_month_end(start) if plan == "Mensual" else annual_end(start)
        db.add(PoolPass(member_id=m.id, payment_id=payment.id, plan_type=plan, start_date=start, end_date=end, amount=amount))
    db.commit()
    return RedirectResponse(f"/admin/socios/{m.id}", 303)


@app.post("/admin/socios/{member_id}/toggle")
def toggle_member(member_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    m = db.get(Member, member_id)
    if not m: raise HTTPException(404)
    m.active = not m.active
    if m.user: m.user.active = m.active
    db.commit()
    return RedirectResponse(f"/admin/socios/{m.id}", 303)


@app.get("/mi-cuenta", response_class=HTMLResponse)
def my_account(request: Request, db: Session = Depends(get_db)):
    u = require_member(request, db)
    sync_finances(db)
    m = u.member
    return templates.TemplateResponse("member_portal.html", {"request": request, "m": m, "status": member_status(m), "latest": latest_membership(m), "pool": pool_status(m), "debt_total": member_debt_total(db, m.id), "member_type": member_billing_type(db, m.id), "member_monthly_fee": member_monthly_fee(db, m.id)})


@app.get("/admin/finanzas", response_class=HTMLResponse)
def finance_dashboard(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    sync_finances(db)
    members = db.query(Member).order_by(Member.last_name, Member.first_name).all()
    debtors = []
    total_debt = 0.0
    for member in members:
        balance = member_debt_total(db, member.id)
        if balance > 0:
            debtors.append((member, balance)); total_debt += balance

    today = date.today()
    month_start = datetime(today.year, today.month, 1)
    next_month_date = shift_months(date(today.year, today.month, 1), 1)
    next_month = datetime(next_month_date.year, next_month_date.month, 1)
    income_month = income_breakdown(db, month_start, next_month)
    income_all = income_breakdown(db)
    held_guarantees, retained_guarantees = guarantee_balances(db)

    return templates.TemplateResponse("finance_dashboard.html", {
        "request": request,
        "monthly_fee": monthly_fee_amount(db, "Regular"),
        "monthly_fee_pensionado": monthly_fee_amount(db, "Pensionado"),
        "member_type_map": {m.id: member_billing_type(db, m.id) for m in members},
        "debtors": debtors,
        "total_debt": round(total_debt, 2),
        "period": f"{SPANISH_MONTHS[today.month]} {today.year}",
        "saved": request.query_params.get("guardado") == "1",
        "income_categories": INCOME_CATEGORIES,
        "income_month": income_month,
        "income_all": income_all,
        "income_month_total": round(sum(income_month.values()), 2),
        "income_all_total": round(sum(income_all.values()), 2),
        "held_guarantees": held_guarantees,
        "retained_guarantees": retained_guarantees,
    })


@app.post("/admin/finanzas/mensualidad")
def update_monthly_fee(request: Request, regular_amount: float = Form(...), pensionado_amount: float = Form(...), db: Session = Depends(get_db)):
    require_admin(request, db)
    regular_amount = round(max(0.0, regular_amount), 2); pensionado_amount = round(max(0.0, pensionado_amount), 2)
    set_monthly_fee_amount(db, regular_amount, "Regular"); set_monthly_fee_amount(db, pensionado_amount, "Pensionado")
    db.commit(); sync_finances(db, update_current_amount=True)
    return RedirectResponse("/admin/finanzas?guardado=1", 303)


@app.post("/admin/finanzas/generar")
def generate_monthly_fees(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db); sync_finances(db)
    return RedirectResponse("/admin/finanzas", 303)


@app.get("/admin/socios/{member_id}/adeudo", response_class=HTMLResponse)
def member_debt_page(member_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db); sync_finances(db)
    m = db.get(Member, member_id)
    if not m: raise HTTPException(404)
    monthly = db.query(MonthlyCharge).filter(MonthlyCharge.member_id == m.id).order_by(MonthlyCharge.period.desc()).all()
    manual = db.query(ManualDebt).filter(ManualDebt.member_id == m.id).order_by(ManualDebt.created_at.desc()).all()
    payment_id = request.query_params.get("pago")
    return templates.TemplateResponse("member_debt.html", {"request": request, "m": m, "monthly": monthly, "manual": manual, "balance": member_debt_total(db, m.id), "monthly_balance": monthly_charge_balance, "manual_balance": manual_debt_balance, "period_label": period_label, "payment_id": int(payment_id) if payment_id and payment_id.isdigit() else None, "member_type": member_billing_type(db, m.id), "member_monthly_fee": member_monthly_fee(db, m.id)})


@app.post("/admin/socios/{member_id}/adeudo/agregar")
def add_manual_debt(member_id: int, request: Request, concept: str = Form(...), amount: float = Form(...), db: Session = Depends(get_db)):
    require_admin(request, db)
    m = db.get(Member, member_id)
    if not m: raise HTTPException(404)
    if amount <= 0: raise HTTPException(400, "El adeudo debe ser mayor a cero.")
    db.add(ManualDebt(member_id=m.id, concept=concept.strip() or "Saldo pendiente", amount=round(amount, 2), paid_amount=0)); db.commit()
    return RedirectResponse(f"/admin/socios/{m.id}/adeudo", 303)


@app.post("/admin/socios/{member_id}/adeudo/pagar")
def pay_member_debt(member_id: int, request: Request, amount: float = Form(...), method: str = Form(...), reference: str = Form(""), db: Session = Depends(get_db)):
    require_admin(request, db); sync_finances(db)
    m = db.get(Member, member_id)
    if not m: raise HTTPException(404)
    balance = member_debt_total(db, m.id)
    if balance <= 0: return RedirectResponse(f"/admin/socios/{m.id}/adeudo", 303)
    amount_to_apply = round(min(max(amount, 0.0), balance), 2)
    if amount_to_apply <= 0: raise HTTPException(400, "El abono debe ser mayor a cero.")
    next_id = (db.query(func.max(Payment.id)).scalar() or 0) + 1
    payment = Payment(member_id=m.id, folio=f"PAG-{next_id:06d}", concept="Abono a mensualidades / adeudo", amount=amount_to_apply, method=method, reference=reference.strip())
    db.add(payment); db.flush(); remaining = amount_to_apply
    monthly = db.query(MonthlyCharge).filter(MonthlyCharge.member_id == m.id).order_by(MonthlyCharge.due_date.asc(), MonthlyCharge.id.asc()).all()
    for charge in monthly:
        due = monthly_charge_balance(charge)
        if due <= 0 or remaining <= 0: continue
        applied = round(min(due, remaining), 2); charge.paid_amount = round((charge.paid_amount or 0) + applied, 2)
        db.add(DebtPaymentAllocation(payment_id=payment.id, target_type="monthly", target_id=charge.id, amount=applied)); remaining = round(remaining - applied, 2)
    manual = db.query(ManualDebt).filter(ManualDebt.member_id == m.id).order_by(ManualDebt.created_at.asc(), ManualDebt.id.asc()).all()
    for debt in manual:
        due = manual_debt_balance(debt)
        if due <= 0 or remaining <= 0: continue
        applied = round(min(due, remaining), 2); debt.paid_amount = round((debt.paid_amount or 0) + applied, 2)
        db.add(DebtPaymentAllocation(payment_id=payment.id, target_type="manual", target_id=debt.id, amount=applied)); remaining = round(remaining - applied, 2)
    db.commit()
    return RedirectResponse(f"/admin/socios/{m.id}/adeudo?pago={payment.id}", 303)


@app.get("/mi-adeudo", response_class=HTMLResponse)
def my_debt(request: Request, db: Session = Depends(get_db)):
    u = require_member(request, db); sync_finances(db); m = u.member
    monthly = db.query(MonthlyCharge).filter(MonthlyCharge.member_id == m.id).order_by(MonthlyCharge.period.desc()).all()
    manual = db.query(ManualDebt).filter(ManualDebt.member_id == m.id).order_by(ManualDebt.created_at.desc()).all()
    return templates.TemplateResponse("my_debt.html", {"request": request, "m": m, "monthly": monthly, "manual": manual, "balance": member_debt_total(db, m.id), "monthly_balance": monthly_charge_balance, "manual_balance": manual_debt_balance, "period_label": period_label, "member_type": member_billing_type(db, m.id), "member_monthly_fee": member_monthly_fee(db, m.id)})


@app.get("/verificar/{token}", response_class=HTMLResponse)
def verify_credential(token: str, request: Request, db: Session = Depends(get_db)):
    m = db.query(Member).filter(Member.qr_token==token).first()
    if not m: return templates.TemplateResponse("verify.html", {"request": request, "valid": False}, status_code=404)
    return templates.TemplateResponse("verify.html", {"request": request, "valid": True, "m": m, "status": member_status(m), "latest": latest_membership(m)})


@app.get("/verificar-alberca/{kind}/{token}", response_class=HTMLResponse)
def verify_pool_credential(kind: str, token: str, request: Request, db: Session = Depends(get_db)):
    member = None; person_name = ""; relation = ""
    if kind == "t":
        member = db.query(Member).filter(Member.qr_token == token).first()
        if member: person_name = f"{member.first_name} {member.last_name}"; relation = "Titular"
    elif kind == "f":
        f = db.query(FamilyMember).filter(FamilyMember.qr_token == token, FamilyMember.active == True).first()
        if f: member = f.member; person_name = f.full_name; relation = f.relationship_type
    if not member: return templates.TemplateResponse("verify_pool.html", {"request": request, "valid": False}, status_code=404)
    status, css, p = pool_status(member); valid = status in ("Activo", "Por vencer")
    return templates.TemplateResponse("verify_pool.html", {"request": request, "valid": valid, "person_name": person_name, "relation": relation, "m": member, "pool_status": (status, css), "pool_pass": p})


@app.get("/qr/{token}.png")
def qr_png(token: str, request: Request, db: Session = Depends(get_db)):
    m = db.query(Member).filter(Member.qr_token==token).first()
    if not m: raise HTTPException(404)
    base = str(request.base_url).rstrip("/"); img = qrcode.make(f"{base}/verificar/{token}")
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.get("/credencial/{member_id}.pdf")
def credential_pdf(member_id: int, request: Request, db: Session = Depends(get_db)):
    u = auth_user(request, db); m = db.get(Member, member_id)
    if not m: raise HTTPException(404)
    if not u or (u.role != "admin" and u.member_id != member_id): raise HTTPException(403)
    latest = latest_membership(m); status, _ = member_status(m); base = str(request.base_url).rstrip("/")
    out = io.BytesIO(); width, height = 86*mm, 54*mm; c = canvas.Canvas(out, pagesize=(width, height))
    regular_credential_page(c, m, latest, status, base); c.showPage(); c.save(); out.seek(0)
    return StreamingResponse(out, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="credencial_{m.member_number}.pdf"'})


@app.get("/credencial-alberca/{member_id}.pdf")
def pool_credentials_pdf(member_id: int, request: Request, db: Session = Depends(get_db)):
    u = auth_user(request, db); m = db.get(Member, member_id)
    if not m: raise HTTPException(404)
    if not u or (u.role != "admin" and u.member_id != member_id): raise HTTPException(403)
    status, _, p = pool_status(m)
    if not p: raise HTTPException(404, "No hay pago de alberca registrado.")
    base = str(request.base_url).rstrip("/"); out = io.BytesIO(); width, height = 86*mm, 54*mm; c = canvas.Canvas(out, pagesize=(width, height))
    pool_credential_page(c, m, f"{m.first_name} {m.last_name}", "Titular", "t", m.qr_token, m.photo.data if m.photo else "", p, status, base); c.showPage()
    for f in m.family_members:
        if f.active: pool_credential_page(c, m, f.full_name, f.relationship_type, "f", f.qr_token, f.photo_data, p, status, base); c.showPage()
    c.save(); out.seek(0)
    return StreamingResponse(out, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="alberca_{m.member_number}_{p.plan_type.lower()}.pdf"'})


@app.get("/recibo/{payment_id}.pdf")
def payment_receipt(payment_id: int, request: Request, db: Session = Depends(get_db)):
    u = auth_user(request, db); p = db.get(Payment, payment_id)
    if not p: raise HTTPException(404)
    if not u or (u.role != "admin" and u.member_id != p.member_id): raise HTTPException(403)
    if db.query(PaymentAudit).filter(
        PaymentAudit.payment_kind == "member",
        PaymentAudit.payment_id == p.id,
        PaymentAudit.action == "cancelled",
    ).first():
        raise HTTPException(410, "Este pago fue anulado y ya no tiene un recibo válido.")
    m = p.member
    ticket_width = 80 * mm; margin = 5 * mm; max_chars = 39
    def wrap_ticket_text(value, limit=max_chars):
        text = str(value or "—").strip(); words = text.replace("·", " · ").split(); lines = []; current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if len(candidate) <= limit: current = candidate; continue
            if current: lines.append(current)
            while len(word) > limit: lines.append(word[:limit]); word = word[limit:]
            current = word
        if current: lines.append(current)
        return lines or ["—"]
    rows = [("Fecha", p.paid_at.strftime('%d/%m/%Y %H:%M')), ("Socio", f"{m.first_name} {m.last_name}"), ("Número de socio", m.member_number), ("Concepto", p.concept), ("Método", p.method), ("Referencia / nota", p.reference or "—")]
    wrapped_rows = [(label, wrap_ticket_text(value)) for label, value in rows]
    pool_lines = []
    if p.pool_pass:
        pp = p.pool_pass; pool_lines = wrap_ticket_text(f"{pp.plan_type} · {pp.start_date.strftime('%d/%m/%Y')} al {pp.end_date.strftime('%d/%m/%Y')}")
    allocations = []
    if p.debt_allocations:
        for alloc in p.debt_allocations:
            if alloc.target_type == "monthly":
                target = db.get(MonthlyCharge, alloc.target_id); desc = f"Mensualidad {period_label(target.period)}" if target else "Mensualidad"
            else:
                target = db.get(ManualDebt, alloc.target_id); desc = target.concept if target else "Adeudo anterior"
            allocations.append((desc, alloc.amount))
    row_height = sum(15 + (len(lines) * 10) for _, lines in wrapped_rows); pool_height = (18 + len(pool_lines) * 10) if pool_lines else 0; allocation_height = (20 + len(allocations) * 13) if allocations else 0
    ticket_height = max(115 * mm, 74 + row_height + 48 + pool_height + allocation_height + 70)
    out = io.BytesIO(); c = canvas.Canvas(out, pagesize=(ticket_width, ticket_height)); c.setTitle(f"Recibo {p.folio}"); w, h = ticket_width, ticket_height; y = h - 8 * mm
    c.setFillColorRGB(0, 0, 0); c.setFont("Helvetica-Bold", 12); c.drawCentredString(w / 2, y, "CLUB DE LEONES"); y -= 12
    c.setFont("Helvetica-Bold", 10); c.drawCentredString(w / 2, y, "DE SABINAS"); y -= 12
    c.setFont("Helvetica", 8); c.drawCentredString(w / 2, y, "RECIBO DE PAGO"); y -= 12
    c.setStrokeColorRGB(0, 0, 0); c.setLineWidth(0.7); c.line(margin, y, w - margin, y); y -= 14
    c.setFont("Helvetica-Bold", 9); c.drawCentredString(w / 2, y, f"Folio: {p.folio}"); y -= 15
    for label, lines in wrapped_rows:
        c.setFont("Helvetica-Bold", 7.5); c.drawString(margin, y, label.upper()); y -= 10; c.setFont("Helvetica", 8.5)
        for line in lines: c.drawString(margin, y, line); y -= 10
        y -= 5
    c.line(margin, y, w - margin, y); y -= 10; c.setFont("Helvetica-Bold", 9); c.drawString(margin, y, "TOTAL PAGADO"); c.setFont("Helvetica-Bold", 15); c.drawRightString(w - margin, y - 1, f"${p.amount:,.2f}"); y -= 18; c.line(margin, y, w - margin, y); y -= 14
    if pool_lines:
        c.setFont("Helvetica-Bold", 7.5); c.drawString(margin, y, "ACCESO DE ALBERCA"); y -= 10; c.setFont("Helvetica", 8)
        for line in pool_lines: c.drawString(margin, y, line); y -= 10
        y -= 5
    if allocations:
        c.setFont("Helvetica-Bold", 7.5); c.drawString(margin, y, "APLICACIÓN DEL ABONO"); y -= 11
        for desc, amount in allocations:
            desc_lines = wrap_ticket_text(desc, 28); c.setFont("Helvetica", 7.5); c.drawString(margin, y, desc_lines[0]); c.drawRightString(w - margin, y, f"${amount:,.2f}"); y -= 11
            for extra in desc_lines[1:]: c.drawString(margin, y, extra); y -= 10
        y -= 4
    y -= 8; half = w / 2; c.setLineWidth(0.6); c.line(margin, y, half - 5 * mm, y); c.line(half + 5 * mm, y, w - margin, y); y -= 10
    c.setFont("Helvetica", 6.5); c.drawCentredString((margin + half - 5 * mm) / 2, y, "Caja"); c.drawCentredString((half + 5 * mm + w - margin) / 2, y, "Socio"); y -= 18
    c.setFont("Helvetica", 6.2); c.drawCentredString(w / 2, y, "Comprobante generado por el sistema"); y -= 8; c.drawCentredString(w / 2, y, "Club de Leones de Sabinas"); y -= 8; c.setFont("Helvetica-Bold", 6); c.drawCentredString(w / 2, y, "Conserve este ticket como comprobante")
    c.showPage(); c.save(); out.seek(0)
    return StreamingResponse(out, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="recibo_{p.folio}.pdf"'})


@app.get("/api/verificar/{token}")
def api_verify(token: str, db: Session = Depends(get_db)):
    m = db.query(Member).filter(Member.qr_token==token).first()
    if not m: return JSONResponse({"valid": False}, status_code=404)
    s,_=member_status(m)
    return {"valid": True, "member_number": m.member_number, "name": f"{m.first_name} {m.last_name}", "status": s, "membership": "Permanente", "valid_until": None}


@app.get("/manifest.webmanifest")
def manifest():
    return {"name":"Club de Leones de Sabinas", "short_name":"Leones Sabinas", "start_url":"/", "display":"standalone", "background_color":"#0d262e", "theme_color":"#00338d", "icons":[{"src":"/static/icons/icon-192.png","sizes":"192x192","type":"image/png"},{"src":"/static/icons/icon-512.png","sizes":"512x512","type":"image/png"}]}


@app.get("/sw.js")
def sw():
    js='''const CACHE="leones-sabinas-v4";self.addEventListener("install",e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(["/","/static/style.css"]))));self.addEventListener("fetch",e=>e.respondWith(fetch(e.request).catch(()=>caches.match(e.request))));'''
    return StreamingResponse(io.BytesIO(js.encode()), media_type="application/javascript")
