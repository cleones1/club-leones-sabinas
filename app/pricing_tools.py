from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pathlib import Path

from .database import get_db
from .models import ClubSetting, User

BASE_DIR = Path(__file__).resolve().parent
router = APIRouter()
templates = Jinja2Templates(directory=BASE_DIR / "templates")

RENTAL_PRICES = [
    {"key":"price_rent_fundadores_total","label":"Salón Fundadores · Total","desc":"Salón Fundadores · Total","default":4000.00,"group":"Salones"},
    {"key":"price_rent_fundadores_anticipo","label":"Salón Fundadores · Anticipo","desc":"Salón Fundadores · Anticipo","default":2000.00,"group":"Salones"},
    {"key":"price_rent_fundadores_clima","label":"Clima extra Fundadores · por hora","desc":"Clima extra Fundadores","default":500.00,"group":"Salones"},
    {"key":"price_rent_damas_total","label":"Salón Damas · Total","desc":"Salón Damas · Total","default":1800.00,"group":"Salones"},
    {"key":"price_rent_damas_anticipo","label":"Salón Damas · Anticipo","desc":"Salón Damas · Anticipo","default":900.00,"group":"Salones"},
    {"key":"price_rent_damas_clima","label":"Clima extra Damas · por hora","desc":"Clima extra Damas","default":350.00,"group":"Salones"},
    {"key":"price_rent_presidentes_total","label":"Salón Presidentes · Total","desc":"Salón Presidentes · Total","default":900.00,"group":"Salones"},
    {"key":"price_rent_presidentes_anticipo","label":"Salón Presidentes · Anticipo","desc":"Salón Presidentes · Anticipo","default":450.00,"group":"Salones"},
    {"key":"price_rent_guarda_total","label":"Guarda · Total","desc":"Guarda · Total","default":750.00,"group":"Guarda y palapas"},
    {"key":"price_rent_guarda_anticipo","label":"Guarda · Anticipo","desc":"Guarda · Anticipo","default":375.00,"group":"Guarda y palapas"},
    {"key":"price_rent_clima_unidad","label":"Renta de clima · por unidad","desc":"Renta de clima","default":500.00,"group":"Guarda y palapas"},
    {"key":"price_rent_palapa_principal_total","label":"Palapa Principal · Total","desc":"Palapa Principal · Total","default":1100.00,"group":"Guarda y palapas"},
    {"key":"price_rent_palapa_principal_anticipo","label":"Palapa Principal · Anticipo","desc":"Palapa Principal · Anticipo","default":550.00,"group":"Guarda y palapas"},
    {"key":"price_rent_palapa_hacienda_total","label":"Palapa Hacienda · Total","desc":"Palapa Hacienda · Total","default":700.00,"group":"Guarda y palapas"},
    {"key":"price_rent_palapa_hacienda_anticipo","label":"Palapa Hacienda · Anticipo","desc":"Palapa Hacienda · Anticipo","default":350.00,"group":"Guarda y palapas"},
    {"key":"price_rent_palapa_palma_total","label":"Palapa Palma · Total","desc":"Palapa Palma · Total","default":700.00,"group":"Guarda y palapas"},
    {"key":"price_rent_palapa_palma_anticipo","label":"Palapa Palma · Anticipo","desc":"Palapa Palma · Anticipo","default":350.00,"group":"Guarda y palapas"},
    {"key":"price_rent_palapa_zacatito_total","label":"Palapa Zacatito · Total","desc":"Palapa Zacatito · Total","default":500.00,"group":"Guarda y palapas"},
    {"key":"price_rent_palapa_zacatito_anticipo","label":"Palapa Zacatito · Anticipo","desc":"Palapa Zacatito · Anticipo","default":250.00,"group":"Guarda y palapas"},
    {"key":"price_rent_evento_alberca_derecho","label":"Derecho de alberca para evento","desc":"Derecho de alberca para evento","default":500.00,"group":"Alberca para eventos"},
    {"key":"price_rent_evento_salvavidas","label":"Salvavidas para evento","desc":"Salvavidas para evento","default":400.00,"group":"Alberca para eventos"},
    {"key":"price_rent_evento_entrada_alberca","label":"Entrada a la alberca para evento","desc":"Entrada a la alberca para evento","default":200.00,"group":"Alberca para eventos"},
]

SALE_PRICES = [
    {"key":"price_sale_refresco_25","label":"Refresco 2.5 L","desc":"Refresco 2.5 L","default":57.00,"group":"Bebidas"},
    {"key":"price_sale_refresco_355","label":"Refresco 355 ml vidrio","desc":"Refresco 355 ml vidrio","default":13.00,"group":"Bebidas"},
    {"key":"price_sale_topo_chico_15","label":"Topo Chico 1.5 L","desc":"Topo Chico 1.5 L","default":31.00,"group":"Bebidas"},
    {"key":"price_sale_refresco_450","label":"Refresco no retornable 450 ml plástico","desc":"Refresco no retornable 450 ml plástico","default":18.00,"group":"Bebidas"},
    {"key":"price_sale_hielo_25kg","label":"Bolsa de hielo 25 kg","desc":"Bolsa de hielo 25 kg","default":140.00,"group":"Bebidas"},
    {"key":"price_sale_tktl_carton","label":"TKT-L 20/2 · cartón c/20 botellas","desc":"TKT-L 20/2 · cartón c/20 botellas vidrio","default":315.00,"group":"Bebidas"},
    {"key":"price_sale_indio_carton","label":"Indio 20/2 · cartón c/20 botellas","desc":"Indio 20/2 · cartón c/20 botellas","default":315.00,"group":"Bebidas"},
    {"key":"price_sale_mantel_redondo","label":"Mantel redondo","desc":"Mantel redondo","default":40.00,"group":"Servicios y artículos"},
    {"key":"price_sale_mantel_rectangular","label":"Mantel rectangular","desc":"Mantel rectangular","default":40.00,"group":"Servicios y artículos"},
    {"key":"price_sale_uso_loza","label":"Uso de loza","desc":"Uso de loza","default":0.00,"group":"Servicios y artículos"},
]

PUBLIC_RENTAL_PRICES = [
    {"key":"price_public_fundadores","label":"Salón Fundadores · precio al público","desc":"Salón Fundadores","default":0.00,"group":"Precio de renta"},
    {"key":"deposit_public_fundadores","label":"Salón Fundadores · depósito en garantía","desc":"Salón Fundadores","default":0.00,"group":"Depósito en garantía"},
    {"key":"price_public_damas","label":"Salón Damas · precio al público","desc":"Salón Damas","default":0.00,"group":"Precio de renta"},
    {"key":"deposit_public_damas","label":"Salón Damas · depósito en garantía","desc":"Salón Damas","default":0.00,"group":"Depósito en garantía"},
    {"key":"price_public_presidentes","label":"Salón Presidentes · precio al público","desc":"Salón Presidentes","default":0.00,"group":"Precio de renta"},
    {"key":"deposit_public_presidentes","label":"Salón Presidentes · depósito en garantía","desc":"Salón Presidentes","default":0.00,"group":"Depósito en garantía"},
]

ALL_PRICES = RENTAL_PRICES + SALE_PRICES + PUBLIC_RENTAL_PRICES


def require_admin(request: Request, db: Session):
    uid = request.session.get("user_id")
    user = db.get(User, uid) if uid else None
    if not user or user.role != "admin" or not user.active:
        raise HTTPException(403)
    return user


def _setting(db: Session, key: str):
    return db.query(ClubSetting).filter(ClubSetting.key == key).first()


def _value(db: Session, item):
    row = _setting(db, item["key"])
    try:
        return round(float(row.value), 2) if row else round(float(item["default"]), 2)
    except (TypeError, ValueError):
        return round(float(item["default"]), 2)


def ensure_price_settings(db: Session):
    changed = False
    for item in ALL_PRICES:
        if not _setting(db, item["key"]):
            db.add(ClubSetting(key=item["key"], value=f'{item["default"]:.2f}'))
            changed = True
    if changed:
        db.commit()


def _items_with_values(db: Session, definitions):
    return [{**item, "value": _value(db, item)} for item in definitions]


@router.get("/admin/precios", response_class=HTMLResponse)
def prices_page(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    ensure_price_settings(db)
    return templates.TemplateResponse("admin_prices.html", {
        "request": request,
        "rentals": _items_with_values(db, RENTAL_PRICES),
        "sales": _items_with_values(db, SALE_PRICES),
        "public_rentals": _items_with_values(db, PUBLIC_RENTAL_PRICES),
        "saved": request.query_params.get("guardado") == "1",
    })


@router.post("/admin/precios")
async def save_prices(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    ensure_price_settings(db)
    form = await request.form()
    for item in ALL_PRICES:
        raw = str(form.get(item["key"], "")).strip()
        try:
            value = round(float(raw), 2)
        except ValueError:
            raise HTTPException(400, f'Precio inválido para {item["label"]}.')
        if value < 0:
            raise HTTPException(400, f'El precio de {item["label"]} no puede ser negativo.')
        row = _setting(db, item["key"])
        if row:
            row.value = f"{value:.2f}"
        else:
            db.add(ClubSetting(key=item["key"], value=f"{value:.2f}"))
    db.commit()
    return RedirectResponse("/admin/precios?guardado=1", 303)


@router.get("/admin/precios/data")
def prices_data(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    ensure_price_settings(db)
    return {
        "rentals": {item["desc"]: _value(db, item) for item in RENTAL_PRICES},
        "sales": {item["desc"]: _value(db, item) for item in SALE_PRICES},
        "public_rentals": {item["key"]: _value(db, item) for item in PUBLIC_RENTAL_PRICES},
    }
