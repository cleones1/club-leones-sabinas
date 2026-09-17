from datetime import date, datetime, timedelta
from pathlib import Path
import io
import re
import secrets
import zipfile
import xml.etree.ElementTree as ET

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from .database import get_db
from .models import FamilyMember, Member, MemberBillingProfile, Membership, User

BASE_DIR = Path(__file__).resolve().parent
router = APIRouter()
templates = Jinja2Templates(directory=BASE_DIR / "templates")

SS_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def require_admin(request: Request, db: Session):
    uid = request.session.get("user_id")
    user = db.get(User, uid) if uid else None
    if not user or user.role != "admin" or not user.active:
        raise HTTPException(403)
    return user


def _col_index(cell_ref: str):
    letters = re.match(r"([A-Z]+)", cell_ref or "")
    if not letters:
        return 0
    value = 0
    for ch in letters.group(1):
        value = value * 26 + (ord(ch) - 64)
    return value - 1


def _shared_strings(zf: zipfile.ZipFile):
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    out = []
    for si in root.findall(f"{{{SS_NS}}}si"):
        parts = [node.text or "" for node in si.iter(f"{{{SS_NS}}}t")]
        out.append("".join(parts))
    return out


def _cell_text(cell, shared):
    kind = cell.attrib.get("t", "")
    if kind == "inlineStr":
        return "".join((n.text or "") for n in cell.iter(f"{{{SS_NS}}}t")).strip()
    value = cell.find(f"{{{SS_NS}}}v")
    raw = value.text if value is not None and value.text is not None else ""
    if kind == "s" and raw:
        try:
            return shared[int(raw)].strip()
        except (ValueError, IndexError):
            return ""
    return raw.strip()


def _xlsx_rows(raw: bytes):
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("El archivo no es un XLSX válido.")

    with zf:
        shared = _shared_strings(zf)
        sheet_files = sorted(
            name for name in zf.namelist()
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        )
        if not sheet_files:
            raise ValueError("El archivo no contiene hojas de cálculo.")
        root = ET.fromstring(zf.read(sheet_files[0]))
        rows = []
        for row in root.iter(f"{{{SS_NS}}}row"):
            values = {}
            max_col = -1
            for cell in row.findall(f"{{{SS_NS}}}c"):
                idx = _col_index(cell.attrib.get("r", ""))
                values[idx] = _cell_text(cell, shared)
                max_col = max(max_col, idx)
            if max_col >= 0:
                rows.append([values.get(i, "") for i in range(max_col + 1)])
        return rows


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "").replace("\r", " ").replace("\n", " ")).strip()


def _parse_source_date(value):
    value = _clean(value)
    if not value:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    try:
        serial = float(value)
        if serial > 1000:
            return date(1899, 12, 30) + timedelta(days=int(serial))
    except ValueError:
        pass
    return None


def _header_lookup(headers):
    return {_clean(name): idx for idx, name in enumerate(headers) if _clean(name)}


def _get(row, header_map, name):
    idx = header_map.get(name)
    return _clean(row[idx]) if idx is not None and idx < len(row) else ""


def _preferred_email(row, h):
    options = [
        _get(row, h, "Contacto: Correo electrónico particular"),
        _get(row, h, "Contacto: Correo electrónico laboral"),
        _get(row, h, "Contacto: Correo electrónico alternativo"),
    ]
    return next((x.lower() for x in options if x), "")


def _phones(row, h):
    values = [
        ("Móvil", _get(row, h, "Contacto: Móvil")),
        ("Particular", _get(row, h, "Contacto: Teléfono particular")),
        ("Otro", _get(row, h, "Contacto: Otro teléfono")),
    ]
    out = []
    for label, value in values:
        if value and value not in [v for _, v in out]:
            out.append((label, value))
    return out


def _address(row, h):
    names = [
        "Contacto: Línea de dirección de correo 1",
        "Contacto: Línea de dirección de correo 2",
        "Contacto: Línea de dirección de correo 3",
        "Contacto: Ciudad de correo",
        "Contacto: Estado o provincia de correo",
        "Contacto: Código postal de correo",
        "Contacto: País de correo",
    ]
    return ", ".join(x for x in (_get(row, h, name) for name in names) if x)


def _technical_email(member_number):
    return f"socio-{member_number}@sin-correo.local"


def _find_header(rows):
    required = "Contacto: Número de identificación del socio"
    for idx, row in enumerate(rows):
        if required in [_clean(v) for v in row]:
            return idx
    raise ValueError("No encontré los encabezados esperados del reporte de socios de Lions.")


def _source_records(raw: bytes):
    rows = _xlsx_rows(raw)
    header_idx = _find_header(rows)
    headers = rows[header_idx]
    h = _header_lookup(headers)

    required_headers = [
        "Contacto: Número de identificación del socio",
        "Contacto: Nombre",
        "Contacto: Apellidos",
    ]
    missing = [name for name in required_headers if name not in h]
    if missing:
        raise ValueError("Faltan columnas necesarias: " + ", ".join(missing))

    records = []
    for row in rows[header_idx + 1:]:
        number = _get(row, h, "Contacto: Número de identificación del socio")
        first = _get(row, h, "Contacto: Nombre")
        middle = _get(row, h, "Contacto: Segundo nombre")
        last = _get(row, h, "Contacto: Apellidos")
        if not number or not first or not last:
            continue

        category = _get(row, h, "Categoría de afiliación del socio")
        if category and category.lower() != "activo":
            continue

        program = _get(row, h, "Programa").lower()
        member_type = "Pensionado" if ("pension" in program or "jubil" in program) else "Regular"

        phone_list = _phones(row, h)
        spouse = _get(row, h, "Contacto: Nombre del cónyuge")
        if spouse.lower() in ("se desconoce", "desconocido"):
            spouse = ""

        notes = []
        sponsor = _get(row, h, "Nombre del patrocinador de la afiliación")
        occupation = _get(row, h, "Contacto: Ocupación")
        gender = _get(row, h, "Contacto: Género")
        if sponsor:
            notes.append(f"Patrocinador: {sponsor}.")
        if occupation:
            notes.append(f"Ocupación: {occupation}.")
        if gender:
            notes.append(f"Género: {gender}.")
        if len(phone_list) > 1:
            notes.append("Teléfonos adicionales: " + "; ".join(f"{k}: {v}" for k, v in phone_list[1:]) + ".")

        records.append({
            "member_number": number,
            "first_name": " ".join(x for x in (first, middle) if x),
            "last_name": last,
            "email": _preferred_email(row, h),
            "phone": phone_list[0][1] if phone_list else "",
            "birth_date": _parse_source_date(_get(row, h, "Contacto: Fecha de nacimiento")),
            "address": _address(row, h),
            "join_date": _parse_source_date(_get(row, h, "Fecha de ingreso del León")),
            "spouse_name": spouse,
            "member_type": member_type,
            "notes": " ".join(notes),
        })
    return records


def _render(request, db, error="", result=None):
    return templates.TemplateResponse("member_import.html", {
        "request": request,
        "error": error,
        "result": result,
        "current_members": db.query(Member).count(),
    })


@router.get("/admin/importar-socios", response_class=HTMLResponse)
def import_members_page(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    return _render(request, db)


@router.post("/admin/importar-socios", response_class=HTMLResponse)
async def import_members(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    require_admin(request, db)
    filename = (file.filename or "").lower()
    if not filename.endswith(".xlsx"):
        return _render(request, db, "Selecciona el archivo XLSX del reporte de Lions.")

    raw = await file.read()
    if not raw:
        return _render(request, db, "El archivo está vacío.")
    if len(raw) > 12 * 1024 * 1024:
        return _render(request, db, "El archivo supera el límite de 12 MB.")

    try:
        source = _source_records(raw)
    except ValueError as exc:
        return _render(request, db, str(exc))

    if not source:
        return _render(request, db, "No encontré socios activos para importar.")

    imported = 0
    skipped = 0
    technical_emails = 0
    missing_phones = 0
    spouses = 0

    try:
        for row in source:
            number = row["member_number"]
            if db.query(Member).filter(Member.member_number == number).first():
                skipped += 1
                continue

            real_email = row["email"]
            email = real_email or _technical_email(number)
            email_conflict = (
                db.query(Member).filter(func.lower(Member.email) == email.lower()).first()
                or db.query(User).filter(func.lower(User.email) == email.lower()).first()
            )
            if email_conflict:
                email = _technical_email(number)

            if not real_email or email != real_email:
                technical_emails += 1

            notes = row["notes"]
            if not real_email:
                notes = ("Correo no disponible en archivo de origen. " + notes).strip()
            elif email != real_email:
                notes = (f"Correo original: {real_email}. Se usó correo técnico por conflicto. " + notes).strip()
            if not row["phone"]:
                missing_phones += 1
                notes = ("Teléfono no disponible en archivo de origen. " + notes).strip()

            member = Member(
                member_number=number,
                first_name=row["first_name"],
                last_name=row["last_name"],
                email=email,
                phone=row["phone"],
                birth_date=row["birth_date"],
                address=row["address"],
                emergency_contact="",
                notes=notes,
                active=True,
                qr_token=secrets.token_urlsafe(24),
            )
            db.add(member)
            db.flush()

            db.add(MemberBillingProfile(member_id=member.id, member_type=row["member_type"]))
            db.add(Membership(
                member_id=member.id,
                membership_type="Permanente",
                start_date=row["join_date"] or date.today(),
                end_date=date(2099, 12, 31),
                amount=0,
            ))

            if row["spouse_name"]:
                db.add(FamilyMember(
                    member_id=member.id,
                    relationship_type="Esposa",
                    full_name=row["spouse_name"],
                    birth_date=None,
                    photo_data="",
                    qr_token=secrets.token_urlsafe(24),
                    active=True,
                ))
                spouses += 1

            imported += 1

        db.commit()
    except Exception:
        db.rollback()
        raise

    result = {
        "detectados": len(source),
        "importados": imported,
        "omitidos": skipped,
        "correos_tecnicos": technical_emails,
        "sin_telefono": missing_phones,
        "conyuges": spouses,
    }
    return _render(request, db, result=result)
