ACTUALIZACIÓN CLUB DE LEONES DE SABINAS

Archivos a reemplazar/agregar en el repositorio:
- app/main.py
- app/models.py
- app/templates/base.html
- app/templates/member_form.html
- app/templates/member_detail.html
- app/templates/member_portal.html
- app/templates/verify_pool.html (nuevo)
- app/static/style-v2.css (nuevo)
- requirements.txt

No se debe borrar la base PostgreSQL. Al reiniciar, SQLAlchemy crea únicamente las tablas nuevas:
member_photos, family_members y pool_passes.

Funciones agregadas:
- Número interno de socio editable.
- Foto del titular guardada en PostgreSQL.
- Esposa y hasta 4 hijos con nombre, fecha de nacimiento y foto.
- Pago de alberca mensual o anual.
- PDF de credenciales de alberca para titular y familia.
- QR individual para validar acceso a alberca.
- Recibo PDF imprimible para cada pago.
