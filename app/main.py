from .core import app
from .admin_tools import router as admin_tools_router

app.include_router(admin_tools_router)
