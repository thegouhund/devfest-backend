"""Task 1: configuration and application skeleton.

Acceptance criteria under test:
- Settings covers every documented key
- No secret has a usable default
- .env.example documents every setting
- GET /health returns {"status": "ok"}
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app


REPO_ROOT = Path(__file__).resolve().parents[1]

# Every setting the plan requires Settings to carry (plan.md Task 1).
REQUIRED_SETTINGS = {
    "database_url",
    "jwt_secret",
    "jwt_expire_minutes",
    "video_storage_path",
    "deepseek_api_key",
    "telegram_bot_token",
    "cors_origins",
    "anomaly_zscore_threshold",
    "baseline_cold_start_days",
}

# Secrets must never carry a usable default — an unset secret has to fail loudly.
SECRET_SETTINGS = {"jwt_secret", "deepseek_api_key", "telegram_bot_token"}

# Settings whose env var name differs from the field name. `cors_origins` is a
# derived property; the raw value is read from BACKEND_CORS_ORIGINS.
ENV_VAR_OVERRIDES = {"cors_origins": "BACKEND_CORS_ORIGINS"}


def test_settings_expose_every_required_key() -> None:
    fields = set(Settings.model_fields) | {
        name
        for name in dir(Settings)
        if isinstance(getattr(Settings, name, None), property)
    }
    assert REQUIRED_SETTINGS <= fields


def test_cors_origins_splits_and_strips() -> None:
    settings = Settings(
        app_name="test",
        environment="test",
        database_url="sqlite://",
        jwt_secret="x",
        jwt_expire_minutes=30,
        video_storage_path="/tmp/v",
        deepseek_api_key="",
        telegram_bot_token="",
        backend_cors_origins="http://a ,  http://b ,",
        warm_up_rppg_on_start=False,
        anomaly_zscore_threshold=2.0,
        baseline_cold_start_days=14,
    )
    assert settings.cors_origins == ["http://a", "http://b"]


@pytest.mark.parametrize("secret", sorted(SECRET_SETTINGS))
def test_secrets_have_no_usable_default(secret: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset secret must be empty, never a real-looking fallback value."""
    monkeypatch.delenv(secret.upper(), raising=False)
    get_settings.cache_clear()
    assert getattr(get_settings(), secret) == ""
    get_settings.cache_clear()


def test_env_example_documents_every_setting() -> None:
    env_example = REPO_ROOT / ".env.example"
    assert env_example.exists(), ".env.example is required"
    documented = set(re.findall(r"^([A-Z_]+)=", env_example.read_text(), re.MULTILINE))
    expected = {
        ENV_VAR_OVERRIDES.get(name, name.upper()) for name in REQUIRED_SETTINGS
    }
    missing = expected - documented
    assert not missing, f"undocumented settings: {sorted(missing)}"


def test_health_endpoint_returns_ok() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
