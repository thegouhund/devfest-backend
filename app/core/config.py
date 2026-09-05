from functools import lru_cache
from os import getenv

from pydantic import BaseModel


class Settings(BaseModel):
    app_name: str
    environment: str
    database_url: str

    # Auth
    jwt_secret: str
    jwt_expire_minutes: int

    # Media storage (PRD A1: raw video lives on the VPS filesystem, not object storage)
    video_storage_path: str

    # External services
    deepseek_api_key: str
    telegram_bot_token: str
    # Ditampilkan ke user sebagai tujuan pengiriman kode linking.
    telegram_bot_username: str

    backend_cors_origins: str

    # Pemanasan model rPPG saat startup. Dimatikan di test supaya suite
    # tidak memuat JAX ratusan kali.
    warm_up_rppg_on_start: bool

    # Anomaly detection tunables (PRD FR-3.2 / A3) — thresholds are still open items
    # in PRD §13, so they stay configurable rather than baked into the detector.
    anomaly_zscore_threshold: float
    baseline_cold_start_days: int

    @property
    def cors_origins(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.backend_cors_origins.split(",")
            if origin.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings(
        app_name=getenv("APP_NAME", "Family Health Monitor"),
        environment=getenv("ENVIRONMENT", "local"),
        database_url=getenv("DATABASE_URL", "sqlite:///./devfest_backend.db"),
        # Secrets default to empty, never to a usable value: an unset secret must
        # fail loudly at the point of use rather than silently signing tokens with
        # a known-to-everyone fallback.
        jwt_secret=getenv("JWT_SECRET", ""),
        jwt_expire_minutes=int(getenv("JWT_EXPIRE_MINUTES", "1440")),
        video_storage_path=getenv("VIDEO_STORAGE_PATH", "./data/videos"),
        deepseek_api_key=getenv("DEEPSEEK_API_KEY", ""),
        telegram_bot_token=getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_bot_username=getenv("TELEGRAM_BOT_USERNAME", ""),
        backend_cors_origins=getenv(
            "BACKEND_CORS_ORIGINS",
            "http://localhost:5173,http://localhost:3000",
        ),
        warm_up_rppg_on_start=getenv("WARM_UP_RPPG_ON_START", "true").lower()
        not in ("false", "0", "no"),
        anomaly_zscore_threshold=float(getenv("ANOMALY_ZSCORE_THRESHOLD", "2.0")),
        baseline_cold_start_days=int(getenv("BASELINE_COLD_START_DAYS", "14")),
    )
