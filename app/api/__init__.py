from app.api.actions import router as actions_router
from app.api.chat import router as chat_router
from app.api.decisions import router as decisions_router
from app.api.intake import router as intake_router
from app.api.learning import router as learning_router
from app.api.profiles import router as profiles_router
from app.api.tools import router as tools_router
from app.api.trips import router as trips_router

__all__ = [
    "actions_router",
    "chat_router",
    "decisions_router",
    "intake_router",
    "learning_router",
    "profiles_router",
    "tools_router",
    "trips_router",
]
