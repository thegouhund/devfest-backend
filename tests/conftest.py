"""Fixture bersama untuk test yang butuh API client + database."""

from __future__ import annotations

import uuid

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

    monkeypatch.setenv("JWT_SECRET", "secret-khusus-test-yang-cukup-panjang-32b")
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
    """Akun terdaftar beserta token profil admin-nya.

    Registrasi membuat akun sekaligus profil admin, dan token yang
    dikembalikan sudah menunjuk profil itu — jadi fixture ini langsung
    siap dipakai endpoint data kesehatan.
    """
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


@pytest.fixture
def admin_profile_id(client, auth_headers):
    """Id profil admin dari akun `registered_user`."""
    response = client.get("/api/v1/profiles/me", headers=auth_headers)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def make_account(db, email: str = "budi@example.com"):
    """Akun langsung di database, tanpa lewat HTTP.

    Untuk test lapisan service yang tidak butuh API client.
    """
    from app.db.models import Account

    account = Account(email=email, password_hash="hash-palsu-untuk-test")
    db.add(account)
    db.flush()
    return account


def make_profile_row(db, account=None, full_name: str = "Budi", **extra):
    """Profil langsung di database, membuat akunnya sekalian kalau perlu.

    Profil tidak bisa berdiri sendiri lagi — `account_id` wajib — jadi
    helper ini menggantikan `FamilyMember(full_name=...)` yang dulu cukup.
    """
    from app.db.models import FamilyMember

    if account is None:
        account = make_account(db, email=f"{full_name.lower()}@example.com")

    profile = FamilyMember(account_id=account.id, full_name=full_name, **extra)
    db.add(profile)
    db.flush()
    return profile


def register_account(client, email: str, name: str) -> dict:
    """Akun baru beserta token profil admin-nya.

    Dipakai banyak modul test yang perlu membuktikan batas antar-akun, jadi
    diletakkan di conftest supaya bentuknya seragam.
    """
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "rahasia-kuat-123", "full_name": name},
    )
    assert response.status_code == 201, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    profile_id = client.get("/api/v1/profiles/me", headers=headers).json()["id"]
    return {"headers": headers, "id": uuid.UUID(profile_id)}


def add_profile(client, headers, full_name: str, **extra) -> dict:
    """Profil anggota dalam akun yang sedang login, beserta token-nya."""
    created = client.post(
        "/api/v1/profiles", json={"full_name": full_name, **extra}, headers=headers
    )
    assert created.status_code == 201, created.text
    profile_id = created.json()["id"]

    token = client.post(
        "/api/v1/auth/select-profile",
        json={"profile_id": profile_id, **({"pin": extra["pin"]} if "pin" in extra else {})},
        headers=headers,
    )
    assert token.status_code == 200, token.text
    return {
        "id": uuid.UUID(profile_id),
        "headers": {"Authorization": f"Bearer {token.json()['access_token']}"},
    }


@pytest.fixture
def keluarga(client):
    """Satu akun keluarga lengkap, plus akun lain yang tidak berhubungan.

    Ayah admin; ibu dan anak anggota biasa. `luar` ada di akun terpisah —
    dipakai untuk membuktikan tidak ada data yang menyeberang akun.
    """
    ayah = register_account(client, "ayah@x.com", "Ayah")
    ibu = add_profile(client, ayah["headers"], "Ibu")
    anak = add_profile(client, ayah["headers"], "Anak")
    luar = register_account(client, "luar@x.com", "Orang Luar")
    return {"ayah": ayah, "ibu": ibu, "anak": anak, "luar": luar}


@pytest.fixture
def make_profile(client, auth_headers):
    """Buat profil anggota tambahan dalam akun yang sama."""

    def _make(full_name: str, **extra) -> dict:
        response = client.post(
            "/api/v1/profiles",
            json={"full_name": full_name, **extra},
            headers=auth_headers,
        )
        assert response.status_code == 201, response.text
        return response.json()

    return _make
