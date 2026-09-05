"""Task 3: Alembic, migrasi awal, ekstensi Timescale/pgvector, seed metric_types.

Acceptance criteria under test:
- `alembic upgrade head` lalu `downgrade base` sukses di Postgres
- vitals_readings terdaftar sebagai hypertable
- metric_types ter-seed 3 baris ERD §2.6, idempoten
- Suite tetap jalan saat ekstensi tidak tersedia
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.models import Base, MetricType
from app.db.seed import SEED_METRIC_TYPES, seed_metric_types


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL tidak di-set; lewati test yang butuh Postgres",
)


def alembic(*args: str, url: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(REPO_ROOT / ".venv/bin/alembic"), *args],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )


# --- Tanpa Postgres --------------------------------------------------------


def test_alembic_is_configured() -> None:
    assert (REPO_ROOT / "alembic.ini").exists()
    assert (REPO_ROOT / "alembic" / "env.py").exists()
    versions = list((REPO_ROOT / "alembic" / "versions").glob("*.py"))
    assert versions, "belum ada file migrasi"


def test_seed_data_matches_erd() -> None:
    """ERD §2.6: tiga metrik awal v1."""
    assert {m["code"] for m in SEED_METRIC_TYPES} == {
        "heart_rate",
        "hrv_rmssd",
        "respiration_rate",
    }


def test_seed_is_idempotent() -> None:
    """Seed dijalankan tiap startup, jadi tidak boleh menduplikasi baris."""
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed_metric_types(db)
        seed_metric_types(db)
        db.commit()
        assert db.query(MetricType).count() == len(SEED_METRIC_TYPES)


def test_seed_does_not_overwrite_local_changes() -> None:
    """Menonaktifkan metrik lewat is_active tidak boleh di-reset seed."""
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed_metric_types(db)
        db.commit()
        db.get(MetricType, "hrv_rmssd").is_active = False
        db.commit()

        seed_metric_types(db)
        db.commit()
        assert db.get(MetricType, "hrv_rmssd").is_active is False


# --- Butuh Postgres --------------------------------------------------------


@pytest.fixture
def pg_engine():
    engine = create_engine(TEST_DATABASE_URL, future=True)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    yield engine
    engine.dispose()


@requires_postgres
def test_upgrade_then_downgrade(pg_engine) -> None:
    up = alembic("upgrade", "head", url=TEST_DATABASE_URL)
    assert up.returncode == 0, up.stderr

    with pg_engine.connect() as conn:
        tables = set(
            conn.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )
            ).scalars()
        )
    assert {"users", "vitals_readings", "metric_types", "health_facts"} <= tables

    down = alembic("downgrade", "base", url=TEST_DATABASE_URL)
    assert down.returncode == 0, down.stderr


@requires_postgres
def test_vitals_readings_is_hypertable(pg_engine) -> None:
    assert alembic("upgrade", "head", url=TEST_DATABASE_URL).returncode == 0
    with pg_engine.connect() as conn:
        found = conn.execute(
            text(
                "SELECT 1 FROM timescaledb_information.hypertables "
                "WHERE hypertable_name = 'vitals_readings'"
            )
        ).scalar()
    assert found == 1, "vitals_readings bukan hypertable"


@requires_postgres
def test_extensions_enabled(pg_engine) -> None:
    assert alembic("upgrade", "head", url=TEST_DATABASE_URL).returncode == 0
    with pg_engine.connect() as conn:
        installed = set(
            conn.execute(text("SELECT extname FROM pg_extension")).scalars()
        )
    assert {"timescaledb", "vector"} <= installed


@requires_postgres
def test_metric_types_seeded_by_migration(pg_engine) -> None:
    assert alembic("upgrade", "head", url=TEST_DATABASE_URL).returncode == 0
    with pg_engine.connect() as conn:
        codes = set(
            conn.execute(text("SELECT code FROM metric_types")).scalars()
        )
    assert codes == {"heart_rate", "hrv_rmssd", "respiration_rate"}


@requires_postgres
def test_embedding_column_is_native_vector(pg_engine) -> None:
    """ERD §2.13: embedding harus tipe vector, bukan text/array."""
    assert alembic("upgrade", "head", url=TEST_DATABASE_URL).returncode == 0
    with pg_engine.connect() as conn:
        udt = conn.execute(
            text(
                "SELECT udt_name FROM information_schema.columns "
                "WHERE table_name = 'health_facts' AND column_name = 'embedding'"
            )
        ).scalar()
    assert udt == "vector"
