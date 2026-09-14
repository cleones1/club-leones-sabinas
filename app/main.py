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
import io, os, secrets, base64, calendar
import qrcode
from PIL import Image, ImageOps
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader

from .database import Base, engine, get_db, SessionLocal
from .models import Member, MemberPhoto, FamilyMember, Membership, Payment, PoolPass, User
from .auth import hash_password, verify_password

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Club de Leones de Sabinas")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "CHANGE-ME-IN-PRODUCTION-7a4ccf3e"), same_site="lax")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Crea tablas nuevas sin eliminar ni alterar los datos existentes.
Base.metadata.create_all(bind=engine)


def seed_admin():
    db = SessionLocal()
    try:
        if not db.query(User).filter(User.role == "admin").first():
            admin_email = os.getenv("ADMIN_EMAIL", "admin@club.local").strip().lower()
            admin_password = os.getenv("ADMIN_PASSWORD", "Admin123!")
            db.add(User(email=admin_email, password_hash=hash_password(admin_password), role="admin", active=True))
            db.commit()
    finally:
        db.close()


seed_admin()


def member_status(member: Member):
    if not member.active:
        return "Suspendido", "danger"
    if not member.memberships:
        return "Sin membresía", "muted"
    latest = max(member.memberships, key=lambda m: m.end_date)
    today = date.today()
    if latest.end_date < today:
        return "Vencido", "danger"
    if latest.end_date <= today + timedelta(days=30):
        return "Por vencer", "warning"
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
    vig = latest.end_date.strftime('%d/%m/%Y') if latest else "Sin vigencia"
    c.drawString(31*mm,24*mm,f"Vigencia: {vig}")
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
    members = db.query(Member).order_by(Member.created_at.desc()).all()
    active = expiring = expired = 0
    for m in members:
        s, _ = member_status(m)
        if s == "Activo": active += 1
        elif s == "Por vencer": expiring += 1
        elif s == "Vencido": expired += 1
    month_start = date.today().replace(day=1)
    income = db.query(func.coalesce(func.sum(Payment.amount),0)).filter(Payment.paid_at >= datetime.combine(month_start, datetime.min.time())).scalar()
    return templates.TemplateResponse("admin_dashboard.html", {
        "request": request, "members": members, "status_fn": member_status, "latest_fn": latest_membership,
        "total": len(members), "active": active, "expiring": expiring, "expired": expired, "income": income
    })


@app.get("/admin/socios/nuevo", response_class=HTMLResponse)
def new_member_form(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    return templates.TemplateResponse("member_form.html", {"request": request})


@app.post("/admin/socios/nuevo")
async def create_member(
    request: Request,
    member_number: str = Form(""), first_name: str = Form(...), last_name: str = Form(...), email: str = Form(...), phone: str = Form(""),
    address: str = Form(""), emergency_contact: str = Form(""), notes: str = Form(""),
    membership_type: str = Form("Anual"), start_date: str = Form(...), end_date: str = Form(...),
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
    m = Member(member_number=number, first_name=first_name.strip(), last_name=last_name.strip(), email=email.strip().lower(), phone=phone.strip(), address=address.strip(), emergency_contact=emergency_contact.strip(), notes=notes.strip(), qr_token=secrets.token_urlsafe(24))
    db.add(m); db.flush()
    member_photo_data = await image_to_data_url(member_photo)
    if member_photo_data:
        db.add(MemberPhoto(member_id=m.id, data=member_photo_data))
    db.add(Membership(member_id=m.id, membership_type=membership_type, start_date=date.fromisoformat(start_date), end_date=date.fromisoformat(end_date), amount=amount))
    db.add(User(email=m.email, password_hash=hash_password(password), role="member", member_id=m.id, active=True))
    await upsert_family_slot(db, m, "Esposa", spouse_name, spouse_birth_date, spouse_photo)
    await upsert_family_slot(db, m, "Hijo 1", child1_name, child1_birth_date, child1_photo)
    await upsert_family_slot(db, m, "Hijo 2", child2_name, child2_birth_date, child2_photo)
    await upsert_family_slot(db, m, "Hijo 3", child3_name, child3_birth_date, child3_photo)
    await upsert_family_slot(db, m, "Hijo 4", child4_name, child4_birth_date, child4_photo)
    db.commit()
    return RedirectResponse(f"/admin/socios/{m.id}", 303)


@app.get("/admin/socios/{member_id}", response_class=HTMLResponse)
def member_detail(member_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    m = db.get(Member, member_id)
    if not m: raise HTTPException(404)
    family = db.query(FamilyMember).filter(
        FamilyMember.member_id == m.id, FamilyMember.active == True
    ).order_by(FamilyMember.id).all()
    family_by_type = {f.relationship_type: f for f in family}
    return templates.TemplateResponse("member_detail.html", {
        "request": request, "m": m, "status": member_status(m), "latest": latest_membership(m),
        "pool": pool_status(m), "family_slot": get_family_slot,
        "family": family, "family_by_type": family_by_type,
        "family_saved": request.query_params.get("familia") == "guardada",
    })


@app.post("/admin/socios/{member_id}/datos")
async def update_member_data(member_id: int, request: Request,
    member_number: str = Form(...), first_name: str = Form(...), last_name: str = Form(...), email: str = Form(...), phone: str = Form(""),
    address: str = Form(""), emergency_contact: str = Form(""), notes: str = Form(""), member_photo: Optional[UploadFile] = File(None),
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
    m.member_number = number; m.first_name = first_name.strip(); m.last_name = last_name.strip(); m.email = email.strip().lower(); m.phone = phone.strip(); m.address = address.strip(); m.emergency_contact = emergency_contact.strip(); m.notes = notes.strip()
    if m.user: m.user.email = m.email
    photo_data = await image_to_data_url(member_photo)
    if photo_data:
        if m.photo: m.photo.data = photo_data
        else: db.add(MemberPhoto(member_id=m.id, data=photo_data))
    db.commit()
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
def add_membership(member_id: int, request: Request, membership_type: str = Form(...), start_date: str = Form(...), end_date: str = Form(...), amount: float = Form(0), db: Session = Depends(get_db)):
    require_admin(request, db)
    m = db.get(Member, member_id)
    if not m: raise HTTPException(404)
    db.add(Membership(member_id=m.id, membership_type=membership_type, start_date=date.fromisoformat(start_date), end_date=date.fromisoformat(end_date), amount=amount))
    db.commit()
    return RedirectResponse(f"/admin/socios/{m.id}", 303)


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
    m = u.member
    return templates.TemplateResponse("member_portal.html", {"request": request, "m": m, "status": member_status(m), "latest": latest_membership(m), "pool": pool_status(m)})


@app.get("/verificar/{token}", response_class=HTMLResponse)
def verify_credential(token: str, request: Request, db: Session = Depends(get_db)):
    m = db.query(Member).filter(Member.qr_token==token).first()
    if not m:
        return templates.TemplateResponse("verify.html", {"request": request, "valid": False}, status_code=404)
    return templates.TemplateResponse("verify.html", {"request": request, "valid": True, "m": m, "status": member_status(m), "latest": latest_membership(m)})


@app.get("/verificar-alberca/{kind}/{token}", response_class=HTMLResponse)
def verify_pool_credential(kind: str, token: str, request: Request, db: Session = Depends(get_db)):
    member = None; person_name = ""; relation = ""
    if kind == "t":
        member = db.query(Member).filter(Member.qr_token == token).first()
        if member:
            person_name = f"{member.first_name} {member.last_name}"; relation = "Titular"
    elif kind == "f":
        f = db.query(FamilyMember).filter(FamilyMember.qr_token == token, FamilyMember.active == True).first()
        if f:
            member = f.member; person_name = f.full_name; relation = f.relationship_type
    if not member:
        return templates.TemplateResponse("verify_pool.html", {"request": request, "valid": False}, status_code=404)
    status, css, p = pool_status(member)
    valid = status in ("Activo", "Por vencer")
    return templates.TemplateResponse("verify_pool.html", {"request": request, "valid": valid, "person_name": person_name, "relation": relation, "m": member, "pool_status": (status, css), "pool_pass": p})


@app.get("/qr/{token}.png")
def qr_png(token: str, request: Request, db: Session = Depends(get_db)):
    m = db.query(Member).filter(Member.qr_token==token).first()
    if not m: raise HTTPException(404)
    base = str(request.base_url).rstrip("/")
    img = qrcode.make(f"{base}/verificar/{token}")
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.get("/credencial/{member_id}.pdf")
def credential_pdf(member_id: int, request: Request, db: Session = Depends(get_db)):
    u = auth_user(request, db); m = db.get(Member, member_id)
    if not m: raise HTTPException(404)
    if not u or (u.role != "admin" and u.member_id != member_id): raise HTTPException(403)
    latest = latest_membership(m); status, _ = member_status(m); base = str(request.base_url).rstrip("/")
    out = io.BytesIO(); width, height = 86*mm, 54*mm; c = canvas.Canvas(out, pagesize=(width, height))
    regular_credential_page(c, m, latest, status, base)
    c.showPage(); c.save(); out.seek(0)
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
        if f.active:
            pool_credential_page(c, m, f.full_name, f.relationship_type, "f", f.qr_token, f.photo_data, p, status, base); c.showPage()
    c.save(); out.seek(0)
    return StreamingResponse(out, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="alberca_{m.member_number}_{p.plan_type.lower()}.pdf"'})


@app.get("/recibo/{payment_id}.pdf")
def payment_receipt(payment_id: int, request: Request, db: Session = Depends(get_db)):
    u = auth_user(request, db); p = db.get(Payment, payment_id)
    if not p: raise HTTPException(404)
    if not u or (u.role != "admin" and u.member_id != p.member_id): raise HTTPException(403)
    m = p.member; out = io.BytesIO(); c = canvas.Canvas(out, pagesize=letter); w, h = letter
    c.setFillColorRGB(0.00,0.20,0.55); c.rect(0,h-82,w,82,fill=1,stroke=0)
    c.setFillColorRGB(1,1,1); c.setFont("Helvetica-Bold",20); c.drawString(45,h-50,"CLUB DE LEONES DE SABINAS")
    c.setFont("Helvetica",10); c.drawString(45,h-68,"RECIBO DE PAGO")
    c.setFillColorRGB(.08,.14,.28); c.setFont("Helvetica-Bold",13); c.drawString(45,h-125,f"Folio: {p.folio}")
    c.setFont("Helvetica",11)
    rows = [
        ("Fecha", p.paid_at.strftime('%d/%m/%Y %H:%M')),
        ("Socio", f"{m.first_name} {m.last_name}"),
        ("Número de socio", m.member_number),
        ("Concepto", p.concept),
        ("Método", p.method),
        ("Referencia", p.reference or "—"),
    ]
    y = h-165
    for label, value in rows:
        c.setFont("Helvetica-Bold",10); c.drawString(45,y,label+":")
        c.setFont("Helvetica",10); c.drawString(160,y,str(value)[:70]); y -= 28
    c.setFillColorRGB(0.00,0.20,0.55); c.roundRect(45,y-15,w-90,55,8,fill=1,stroke=0)
    c.setFillColorRGB(1,1,1); c.setFont("Helvetica-Bold",15); c.drawString(65,y+7,"TOTAL PAGADO")
    c.setFont("Helvetica-Bold",19); c.drawRightString(w-65,y+6,f"${p.amount:,.2f}")
    y -= 75
    if p.pool_pass:
        pp = p.pool_pass
        c.setFillColorRGB(.08,.14,.28); c.setFont("Helvetica-Bold",10); c.drawString(45,y,"Acceso de alberca:")
        c.setFont("Helvetica",10); c.drawString(160,y,f"{pp.plan_type} · {pp.start_date.strftime('%d/%m/%Y')} al {pp.end_date.strftime('%d/%m/%Y')}")
    c.setStrokeColorRGB(.55,.58,.65); c.line(70,125,270,125); c.line(w-270,125,w-70,125)
    c.setFillColorRGB(.25,.28,.35); c.setFont("Helvetica",8); c.drawCentredString(170,110,"Recibí / Caja"); c.drawCentredString(w-170,110,"Socio")
    c.setFont("Helvetica",7.5); c.drawCentredString(w/2,55,"Comprobante generado por el sistema de Club de Leones de Sabinas")
    c.showPage(); c.save(); out.seek(0)
    return StreamingResponse(out, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="recibo_{p.folio}.pdf"'})


@app.get("/api/verificar/{token}")
def api_verify(token: str, db: Session = Depends(get_db)):
    m = db.query(Member).filter(Member.qr_token==token).first()
    if not m: return JSONResponse({"valid": False}, status_code=404)
    s,_=member_status(m); latest=latest_membership(m)
    return {"valid": True, "member_number": m.member_number, "name": f"{m.first_name} {m.last_name}", "status": s, "valid_until": latest.end_date.isoformat() if latest else None}


@app.get("/manifest.webmanifest")
def manifest():
    return {
      "name":"Club de Leones de Sabinas", "short_name":"Leones Sabinas", "start_url":"/", "display":"standalone",
      "background_color":"#0d262e", "theme_color":"#00338d",
      "icons":[{"src":"/static/icons/icon-192.png","sizes":"192x192","type":"image/png"},{"src":"/static/icons/icon-512.png","sizes":"512x512","type":"image/png"}]
    }


@app.get("/sw.js")
def sw():
    js='''const CACHE="leones-sabinas-v3";self.addEventListener("install",e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(["/","/static/style.css"]))));self.addEventListener("fetch",e=>e.respondWith(fetch(e.request).catch(()=>caches.match(e.request))));'''
    return StreamingResponse(io.BytesIO(js.encode()), media_type="application/javascript")
