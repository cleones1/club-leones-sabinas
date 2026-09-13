# Club de Leones de Sabinas — Administración y portal de socios

Aplicación web instalable (PWA) para escritorio y Android. Usa una sola base de datos SQLite y separa los accesos de administrador y socio.

## Funciones incluidas

- Alta de socios y generación automática de número de socio.
- Usuario/contraseña para administrador y para cada socio.
- Alta y renovación de membresías con fecha de inicio y vencimiento.
- Estados automáticos: Activo, Por vencer, Vencido, Suspendido.
- Registro e historial de pagos.
- Dashboard con socios e ingresos del mes.
- Portal del socio con estatus, vigencia y pagos.
- Credencial PDF descargable con QR.
- QR dinámico que abre una página de validación en tiempo real.
- Diseño responsive e instalable como PWA en Android/escritorio.

## Arranque local

1. Instalar Python 3.11+.
2. Abrir terminal dentro de la carpeta del proyecto.
3. Crear entorno virtual (opcional) e instalar dependencias:

   pip install -r requirements.txt

4. Arrancar:

   uvicorn app.main:app --host 0.0.0.0 --port 8000

5. Abrir http://localhost:8000

## Acceso administrador inicial

- Correo: admin@club.local
- Contraseña: Admin123!

**Cambia esta contraseña y la SESSION_SECRET antes de usar en producción.**

## Android

Al publicar la aplicación bajo HTTPS, abre la URL desde Chrome y usa “Instalar aplicación” / “Agregar a pantalla principal”. La PWA se comportará como app de pantalla completa.

## Producción recomendada

Para uso real, migrar SQLite a PostgreSQL, configurar HTTPS, respaldos, dominio, recuperación de contraseña, registro de auditoría y almacenamiento privado de fotografías/documentos.

## Identidad visual

La interfaz se personalizó como **Club de Leones de Sabinas**, utilizando los colores azul y dorado de Lions y el emblema oficial de Lions Clubs International en las pantallas. La aplicación incluye un icono local de respaldo para la PWA y para casos sin conexión.

## Despliegue en Railway

Esta versión ya soporta PostgreSQL mediante la variable `DATABASE_URL` y conserva SQLite como respaldo para desarrollo local.

Configuración recomendada en Railway:

1. Crear un proyecto y desplegar esta carpeta como servicio web.
2. Agregar un servicio PostgreSQL en el mismo proyecto.
3. En el servicio web, referenciar la variable `DATABASE_URL` del PostgreSQL.
4. Definir `SESSION_SECRET`, `ADMIN_EMAIL` y `ADMIN_PASSWORD` como variables privadas del servicio.
5. Railway usará `railway.json` para iniciar: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
6. Generar un dominio público desde Networking.

La ruta `/health` sirve como comprobación de salud del despliegue.
