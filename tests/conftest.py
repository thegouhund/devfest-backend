"""Fixture bersama untuk test yang butuh API client + database."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base
from app.db.seed import seed_metric_types
from app.db.session import get_db
from app.main import app


@pytest.fixture
def db_session():
    """Database in-memory per test, dengan FK aktif.

    SQLite mematikan FK secara default; tanpa PRAGMA ini, test yang
    seharusnya gagal karena FK malah lolos.
    """
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        # SQLite in-memory memberi database terpisah per koneksi; tanpa
        # StaticPool, tabel yang dibuat di sini tidak terlihat oleh request.
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, class_=Session, autoflush=False)
    with TestingSession() as session:
        # Di produksi `metric_types` diisi migrasi Alembic; di sini skema
        # dibuat lewat create_all, jadi seed-nya harus dipanggil manual —
        # tanpa ini penulisan vitals_readings gagal karena foreign key.
        seed_metric_types(session)
        session.commit()
        yield session
    engine.dispose()


@pytest.fixture
def client(db_session, monkeypatch: pytest.MonkeyPatch):
    from app.core.config import get_settings

    monkeypatch.setenv("JWT_SECRET", "secret-khusus-test")
    # Tanpa ini tiap TestClient memuat JAX (~20 detik) saat startup.
    monkeypatch.setenv("WARM_UP_RPPG_ON_START", "false")
    get_settings.cache_clear()

    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def registered_user(client):
    """User terdaftar beserta token-nya, untuk test yang butuh login."""
    payload = {
        "email": "budi@example.com",
        "password": "rahasia-yang-kuat-123",
        "full_name": "Budi Santoso",
    }
    response = client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201, response.text
    return {**payload, "token": response.json()["access_token"]}


@pytest.fixture
def auth_headers(registered_user):
    return {"Authorization": f"Bearer {registered_user['token']}"}
