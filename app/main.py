from . import public_models  # Registra tablas de rentas públicas antes de create_all.
from .core import app
from .test_cleanup import run_test_cleanup_once

# Limpieza única solicitada antes de iniciar operación real.
run_test_cleanup_once()

from .member_importer import router as member_importer_router
from .daily_close import router as daily_close_router
from .payment_corrections import router as payment_corrections_router
from .admin_tools import router as admin_tools_router
from .dues_tools import router as dues_tools_router
from .pricing_tools import router as pricing_tools_router
from .public_rentals import router as public_rentals_router

app.include_router(member_importer_router)
app.include_router(daily_close_router)
app.include_router(payment_corrections_router)
app.include_router(admin_tools_router)
app.include_router(dues_tools_router)
app.include_router(pricing_tools_router)
app.include_router(public_rentals_router)
