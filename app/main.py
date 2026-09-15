from .core import app
from .admin_tools import router as admin_tools_router
from .dues_tools import router as dues_tools_router

app.include_router(admin_tools_router)
app.include_router(dues_tools_router)
