"""Task 2: SQLAlchemy models for every ERD table.

Acceptance criteria under test:
- Kolom sesuai ERD §2 (nama, tipe, nullability, default)
- metric_type adalah FK ke metric_types.code, bukan enum Python
- users.managed_by_user_id self-FK; health_facts punya user_id + reported_by_user_id
- Unique constraint lengkap
- health_facts.embedding Vector(1536); kolom enum pakai CHECK constraint
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    Base,
    Family,
    FamilyMember,
    MetricType,
    User,
    VitalsReading,
)


ERD_TABLES = {
    "users",
    "families",
    "family_members",
    "measurement_sessions",
    "video_storage_refs",
    "metric_types",
    "vitals_readings",
    "baselines",
    "anomalies",
    "activities_log",
    "conversation_log",
    "conversation_messages",
    "health_facts",
    "telegram_links",
    "notifications",
    "data_visibility_settings",
}


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", future=True)

    # SQLite mematikan FK secara default; tanpa ini test FK lolos palsu.
    @event.listens_for(eng, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)
    return eng


def test_every_erd_table_is_modelled() -> None:
    assert ERD_TABLES <= set(Base.metadata.tables)


def test_schema_creates_cleanly(engine) -> None:
    assert ERD_TABLES <= set(inspect(engine).get_table_names())


class TestMetricTypeLookup:
    """ERD note 2: metric_type divalidasi lewat FK, bukan enum hardcode —
    menambah metrik baru harus cukup satu INSERT."""

    @pytest.mark.parametrize(
        "table", ["vitals_readings", "baselines", "anomalies"]
    )
    def test_metric_type_is_foreign_key(self, table: str) -> None:
        fks = {
            (fk.parent.name, fk.column.table.name, fk.column.name)
            for fk in Base.metadata.tables[table].foreign_keys
        }
        assert ("metric_type", "metric_types", "code") in fks

    def test_new_metric_needs_no_schema_change(self, engine) -> None:
        with Session(engine) as db:
            db.add(MetricType(code="spo2", display_name="SpO2", default_unit="%"))
            db.commit()
            assert db.get(MetricType, "spo2") is not None


class TestSubjectVersusActor:
    """ERD note 7: user_id (subjek data) selalu terpisah dari *_by_user_id (pelaku input)."""

    @pytest.mark.parametrize(
        ("table", "actor_column"),
        [
            ("measurement_sessions", "initiated_by_user_id"),
            ("activities_log", "logged_by_user_id"),
            ("health_facts", "reported_by_user_id"),
        ],
    )
    def test_subject_and_actor_columns_both_exist(
        self, table: str, actor_column: str
    ) -> None:
        columns = Base.metadata.tables[table].columns
        assert "user_id" in columns
        assert actor_column in columns


def test_users_self_reference_for_dependents() -> None:
    """ERD note 1: dependent ditangani lewat self-FK, tanpa tabel terpisah."""
    fks = {
        (fk.parent.name, fk.column.table.name)
        for fk in Base.metadata.tables["users"].foreign_keys
    }
    assert ("managed_by_user_id", "users") in fks


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("family_members", {"family_id", "user_id"}),
        ("baselines", {"user_id", "metric_type", "window_end"}),
        ("data_visibility_settings", {"user_id", "data_type"}),
        ("video_storage_refs", {"measurement_session_id"}),
        ("telegram_links", {"user_id"}),
    ],
)
def test_unique_constraints_present(table: str, columns: set[str]) -> None:
    tbl = Base.metadata.tables[table]
    unique_sets = [
        {c.name for c in con.columns}
        for con in tbl.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    ]
    unique_sets += [{c.name for c in idx.columns} for idx in tbl.indexes if idx.unique]
    unique_sets += [{c.name} for c in tbl.columns if c.unique]
    assert columns in unique_sets


def test_unique_constraint_is_enforced(engine) -> None:
    """Constraint harus benar-benar ditegakkan database, bukan sekadar dideklarasikan."""
    with Session(engine) as db:
        user = User(full_name="A", email="a@example.com")
        db.add(user)
        db.flush()
        family = Family(name="F", invite_code="CODE1", created_by=user.id)
        db.add(family)
        db.flush()
        db.add(FamilyMember(family_id=family.id, user_id=user.id, role="admin"))
        db.commit()

        db.add(FamilyMember(family_id=family.id, user_id=user.id, role="member"))
        with pytest.raises(IntegrityError, match="UNIQUE"):
            db.commit()


def test_embedding_column_is_vector_1536() -> None:
    embedding = Base.metadata.tables["health_facts"].columns["embedding"]
    assert getattr(embedding.type, "dim", None) == 1536


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("family_members", "role"),
        ("measurement_sessions", "capture_method"),
        ("measurement_sessions", "processing_status"),
        ("activities_log", "category"),
        ("activities_log", "source"),
        ("anomalies", "severity"),
        ("anomalies", "status"),
        ("conversation_messages", "role"),
        ("health_facts", "fact_category"),
        ("notifications", "channel"),
        ("data_visibility_settings", "visibility"),
    ],
)
def test_enum_columns_have_check_constraint(table: str, column: str) -> None:
    """ERD §0: nilai enum dibatasi lewat CHECK constraint."""
    checks = [
        con
        for con in Base.metadata.tables[table].constraints
        if con.__class__.__name__ == "CheckConstraint"
    ]
    assert any(column in str(con.sqltext) for con in checks), (
        f"{table}.{column} tidak punya CHECK constraint"
    )


def test_enum_check_is_enforced(engine) -> None:
    with Session(engine) as db:
        user = User(full_name="B", email="b@example.com")
        db.add(user)
        db.flush()
        family = Family(name="F2", invite_code="CODE2", created_by=user.id)
        db.add(family)
        db.flush()
        db.add(
            FamilyMember(family_id=family.id, user_id=user.id, role="superadmin")
        )
        with pytest.raises(IntegrityError, match="CHECK"):
            db.commit()


def test_vitals_reading_requires_known_metric(engine) -> None:
    """metric_type tak dikenal harus ditolak FK, bukan lolos sebagai text bebas."""
    with Session(engine) as db:
        user = User(full_name="C", email="c@example.com")
        db.add(user)
        db.flush()
        db.add(
            VitalsReading(
                measurement_session_id=None,
                user_id=user.id,
                metric_type="not_a_real_metric",
                value=60,
            )
        )
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            db.commit()


def test_email_unique_ignoring_case(engine) -> None:
    """Keunikan email harus ditegakkan database, bukan hanya schema Pydantic —
    seed, import, dan pembuatan profil dependent tidak lewat validasi schema."""
    with Session(engine) as db:
        db.add(User(full_name="A", email="budi@example.com"))
        db.commit()
        db.add(User(full_name="B", email="BUDI@example.com"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_multiple_users_without_email_allowed(engine) -> None:
    """Dependent tidak punya email; banyak NULL harus tetap boleh berdampingan."""
    with Session(engine) as db:
        admin = User(full_name="Admin", email="admin@example.com")
        db.add(admin)
        db.flush()
        db.add_all(
            [
                User(full_name="Anak 1", is_dependent=True, managed_by_user_id=admin.id),
                User(full_name="Anak 2", is_dependent=True, managed_by_user_id=admin.id),
            ]
        )
        db.commit()
        assert db.query(User).count() == 3


def test_dependent_user_has_no_credentials(engine) -> None:
    """ERD §2.1: email & password_hash nullable supaya dependent bisa tanpa login."""
    with Session(engine) as db:
        admin = User(full_name="Admin", email="admin@example.com")
        db.add(admin)
        db.flush()
        child = User(full_name="Anak", is_dependent=True, managed_by_user_id=admin.id)
        db.add(child)
        db.commit()
        assert child.email is None
        assert child.password_hash is None
        assert child.managed_by_user_id == admin.id
