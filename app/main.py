from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.auth import router as auth_router
from app.api.v1.families import router as families_router
from app.api.v1.health import router as health_router
from app.api.v1.users import router as users_router
from app.core.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema creation is Alembic's job (`alembic upgrade head`), not the app's.
    yield


settings = get_settings()
app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router, prefix="/health", tags=["health"])
app.include_router(auth_router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(users_router, prefix="/api/v1/users", tags=["users"])
app.include_router(families_router, prefix="/api/v1/families", tags=["families"])
