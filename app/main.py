import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.activities import router as activities_router
from app.api.v1.anomalies import router as anomalies_router
from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.api.v1.account import router as account_router
from app.api.v1.health import router as health_router
from app.api.v1.measurements import router as measurements_router
from app.api.v1.settings import router as settings_router
from app.api.v1.telegram import router as telegram_router
from app.api.v1.profiles import router as profiles_router
from app.api.v1.vitals import router as vitals_router
from app.core.config import get_settings
from app.services.rppg import warm_up as warm_up_rppg


# Batas tunggu pemanasan saat shutdown; kompilasi JAX ~20 detik.
WARM_UP_SHUTDOWN_TIMEOUT = 60


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema creation is Alembic's job (`alembic upgrade head`), not the app's.

    # Panaskan model rPPG supaya pengukuran pertama tidak menanggung ~20
    # detik kompilasi JAX (terukur: 24 detik jadi 2 detik).
    #
    # Dijalankan lewat executor bawaan, bukan threading.Thread daemon:
    # thread daemon yang masih memuat JAX saat interpreter dimatikan
    # membuat proses crash ("terminate called", core dump) — berbahaya
    # saat server di-restart.
    if get_settings().warm_up_rppg_on_start:
        loop = asyncio.get_running_loop()
        warm_up_task = loop.run_in_executor(None, warm_up_rppg)
    else:
        warm_up_task = None

    try:
        yield
    finally:
        # Tunggu pemanasan selesai sebelum proses berakhir, supaya JAX
        # tidak dimatikan di tengah kompilasi.
        if warm_up_task is not None:
            await asyncio.wait_for(warm_up_task, timeout=WARM_UP_SHUTDOWN_TIMEOUT)


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
app.include_router(account_router, prefix="/api/v1/account", tags=["account"])
app.include_router(profiles_router, prefix="/api/v1/profiles", tags=["profiles"])
app.include_router(settings_router, prefix="/api/v1/settings", tags=["settings"])
app.include_router(
    measurements_router, prefix="/api/v1/measurements", tags=["measurements"]
)
app.include_router(vitals_router, prefix="/api/v1/vitals", tags=["vitals"])
app.include_router(
    activities_router, prefix="/api/v1/activities", tags=["activities"]
)
app.include_router(telegram_router, prefix="/api/v1/telegram", tags=["telegram"])
app.include_router(
    anomalies_router, prefix="/api/v1/anomalies", tags=["anomalies"]
)
app.include_router(chat_router, prefix="/api/v1/chat", tags=["chat"])
