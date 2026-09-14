from fastapi import FastAPI, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func
from pathlib import Path
from datetime import date, datetime, timedelta
import io, os, secrets
import qrcode
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import landscape
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader

from .database import Base, engine, get_db, SessionLocal
from .models import Member, Membership, Payment, User
from .auth import hash_password, verify_password

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Club de Leones de Sabinas")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "CHANGE-ME-IN-PRODUCTION-7a4ccf3e"), same_site="lax")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

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
def create_member(request: Request,
    first_name: str = Form(...), last_name: str = Form(...), email: str = Form(...), phone: str = Form(""),
    address: str = Form(""), emergency_contact: str = Form(""), notes: str = Form(""),
    membership_type: str = Form("Anual"), start_date: str = Form(...), end_date: str = Form(...),
    amount: float = Form(0), password: str = Form("Socio123!"), db: Session = Depends(get_db)):
    require_admin(request, db)
    if db.query(Member).filter(func.lower(Member.email)==email.strip().lower()).first():
        return templates.TemplateResponse("member_form.html", {"request": request, "error": "Ese correo ya está registrado."}, status_code=400)
    next_id = (db.query(func.max(Member.id)).scalar() or 0) + 1
    m = Member(member_number=f"SOC-{next_id:05d}", first_name=first_name.strip(), last_name=last_name.strip(), email=email.strip().lower(), phone=phone.strip(), address=address.strip(), emergency_contact=emergency_contact.strip(), notes=notes.strip(), qr_token=secrets.token_urlsafe(24))
    db.add(m); db.flush()
    ms = Membership(member_id=m.id, membership_type=membership_type, start_date=date.fromisoformat(start_date), end_date=date.fromisoformat(end_date), amount=amount)
    db.add(ms)
    db.add(User(email=m.email, password_hash=hash_password(password), role="member", member_id=m.id, active=True))
    db.commit()
    return RedirectResponse(f"/admin/socios/{m.id}", 303)

@app.get("/admin/socios/{member_id}", response_class=HTMLResponse)
def member_detail(member_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    m = db.get(Member, member_id)
    if not m: raise HTTPException(404)
    return templates.TemplateResponse("member_detail.html", {"request": request, "m": m, "status": member_status(m), "latest": latest_membership(m)})

@app.post("/admin/socios/{member_id}/membresia")
def add_membership(member_id: int, request: Request, membership_type: str = Form(...), start_date: str = Form(...), end_date: str = Form(...), amount: float = Form(0), db: Session = Depends(get_db)):
    require_admin(request, db)
    m = db.get(Member, member_id)
    if not m: raise HTTPException(404)
    db.add(Membership(member_id=m.id, membership_type=membership_type, start_date=date.fromisoformat(start_date), end_date=date.fromisoformat(end_date), amount=amount))
    db.commit()
    return RedirectResponse(f"/admin/socios/{m.id}", 303)

@app.post("/admin/socios/{member_id}/pago")
def add_payment(member_id: int, request: Request, concept: str = Form(...), amount: float = Form(...), method: str = Form(...), reference: str = Form(""), db: Session = Depends(get_db)):
    require_admin(request, db)
    m = db.get(Member, member_id)
    if not m: raise HTTPException(404)
    next_id = (db.query(func.max(Payment.id)).scalar() or 0) + 1
    db.add(Payment(member_id=m.id, folio=f"PAG-{next_id:06d}", concept=concept, amount=amount, method=method, reference=reference))
    db.commit()
    return RedirectResponse(f"/admin/socios/{m.id}", 303)

@app.post("/admin/socios/{member_id}/toggle")
def toggle_member(member_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    m = db.get(Member, member_id)
    if not m: raise HTTPException(404)
    m.active = not m.active
    db.commit()
    return RedirectResponse(f"/admin/socios/{m.id}", 303)

@app.get("/mi-cuenta", response_class=HTMLResponse)
def my_account(request: Request, db: Session = Depends(get_db)):
    u = require_member(request, db)
    m = u.member
    return templates.TemplateResponse("member_portal.html", {"request": request, "m": m, "status": member_status(m), "latest": latest_membership(m)})

@app.get("/verificar/{token}", response_class=HTMLResponse)
def verify_credential(token: str, request: Request, db: Session = Depends(get_db)):
    m = db.query(Member).filter(Member.qr_token==token).first()
    if not m:
        return templates.TemplateResponse("verify.html", {"request": request, "valid": False}, status_code=404)
    return templates.TemplateResponse("verify.html", {"request": request, "valid": True, "m": m, "status": member_status(m), "latest": latest_membership(m)})

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
    u = auth_user(request, db)
    m = db.get(Member, member_id)
    if not m: raise HTTPException(404)
    if not u or (u.role != "admin" and u.member_id != member_id): raise HTTPException(403)
    latest = latest_membership(m)
    status, _ = member_status(m)
    base = str(request.base_url).rstrip("/")
    qr = qrcode.make(f"{base}/verificar/{m.qr_token}")
    qbuf = io.BytesIO(); qr.save(qbuf, format="PNG"); qbuf.seek(0)
    out = io.BytesIO()
    width, height = 86*mm, 54*mm
    c = canvas.Canvas(out, pagesize=(width, height))
    # Identidad visual Lions: azul y dorado.
    c.setFillColorRGB(0.00,0.20,0.55); c.rect(0,0,width,height,fill=1,stroke=0)
    c.setFillColorRGB(1.00,0.80,0.00); c.rect(0,43*mm,width,11*mm,fill=1,stroke=0)
    # Emblema local simplificado para que la credencial funcione aun sin conexión.
    c.setFillColorRGB(0.00,0.20,0.55); c.circle(9*mm,48.5*mm,4.1*mm,fill=1,stroke=0)
    c.setFillColorRGB(1,1,1); c.setFont("Helvetica-Bold", 9); c.drawCentredString(9*mm,46.5*mm,"L")
    c.setFillColorRGB(0.00,0.20,0.55); c.setFont("Helvetica-Bold", 8.8); c.drawString(15*mm, 47*mm, "CLUB DE LEONES DE SABINAS")
    c.setFillColorRGB(1,1,1); c.setFont("Helvetica-Bold", 10); c.drawString(7*mm, 35*mm, f"{m.first_name} {m.last_name}"[:38])
    c.setFont("Helvetica", 8); c.drawString(7*mm, 29*mm, f"Socio: {m.member_number}")
    vig = latest.end_date.strftime('%d/%m/%Y') if latest else "Sin vigencia"
    c.drawString(7*mm, 24*mm, f"Vigencia: {vig}")
    c.drawString(7*mm, 19*mm, f"Estatus: {status}")
    c.drawImage(ImageReader(qbuf), 58*mm, 8*mm, 21*mm, 21*mm, preserveAspectRatio=True, mask='auto')
    c.setFillColorRGB(1.00,0.80,0.00); c.setFont("Helvetica-Bold", 5.5); c.drawString(7*mm, 6*mm, "Escanee el QR para validar la membresía en tiempo real.")
    c.showPage(); c.save(); out.seek(0)
    headers={"Content-Disposition": f'attachment; filename="credencial_{m.member_number}.pdf"'}
    return StreamingResponse(out, media_type="application/pdf", headers=headers)

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
      "background_color":"#0d262e", "theme_color":"#0d262e",
      "icons":[{"src":"/static/icons/icon-192.png","sizes":"192x192","type":"image/png"},{"src":"/static/icons/icon-512.png","sizes":"512x512","type":"image/png"}]
    }

@app.get("/sw.js")
def sw():
    js='''const CACHE="leones-sabinas-v2";self.addEventListener("install",e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(["/","/static/style.css"]))));self.addEventListener("fetch",e=>e.respondWith(fetch(e.request).catch(()=>caches.match(e.request))));'''
    return StreamingResponse(io.BytesIO(js.encode()), media_type="application/javascript")
