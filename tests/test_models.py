"""Model SQLAlchemy untuk setiap tabel ERD.

Yang diuji:
- Tabel ERD §2 lengkap dan skemanya bisa dibuat
- metric_type adalah FK ke metric_types.code, bukan enum Python
- Pemisahan subjek (family_member_id) vs pelaku (*_by_family_member_id)
- Unique constraint dan CHECK constraint benar-benar ditegakkan database
- health_facts.embedding Vector(1536)
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    Account,
    Base,
    DataVisibilitySetting,
    FamilyMember,
    MetricType,
    VitalsReading,
)


ERD_TABLES = {
    "accounts",
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


@pytest.fixture
def account(engine):
    """Akun beserta profil admin-nya — titik awal hampir semua data."""
    with Session(engine) as db:
        acc = Account(email="budi@example.com", password_hash="x")
        db.add(acc)
        db.flush()
        admin = FamilyMember(account_id=acc.id, full_name="Budi", role="admin")
        db.add(admin)
        db.commit()
        yield acc.id, admin.id


def test_every_erd_table_is_modelled() -> None:
    assert ERD_TABLES <= set(Base.metadata.tables)


def test_schema_creates_cleanly(engine) -> None:
    assert ERD_TABLES <= set(inspect(engine).get_table_names())


def test_no_legacy_tables_remain() -> None:
    """Model satu-akun-per-keluarga: `users` dan `families` tidak boleh
    tersisa, karena keduanya sudah dilebur ke accounts + family_members."""
    assert "users" not in Base.metadata.tables
    assert "families" not in Base.metadata.tables


class TestMetricTypeLookup:
    """ERD note 2: metric_type divalidasi lewat FK, bukan enum hardcode —
    menambah metrik baru harus cukup satu INSERT."""

    @pytest.mark.parametrize("table", ["vitals_readings", "baselines", "anomalies"])
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
    """ERD note 7: family_member_id (subjek data) selalu terpisah dari
    *_by_family_member_id (pelaku input)."""

    @pytest.mark.parametrize(
        ("table", "actor_column"),
        [
            ("measurement_sessions", "initiated_by_family_member_id"),
            ("activities_log", "logged_by_family_member_id"),
            ("health_facts", "reported_by_family_member_id"),
        ],
    )
    def test_subject_and_actor_columns_both_exist(
        self, table: str, actor_column: str
    ) -> None:
        columns = Base.metadata.tables[table].columns
        assert "family_member_id" in columns
        assert actor_column in columns


def test_profiles_belong_to_account() -> None:
    """Batas keluarga adalah akun: setiap profil menunjuk satu akun."""
    fks = {
        (fk.parent.name, fk.column.table.name)
        for fk in Base.metadata.tables["family_members"].foreign_keys
    }
    assert ("account_id", "accounts") in fks


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("baselines", {"family_member_id", "metric_type", "window_end"}),
        ("data_visibility_settings", {"family_member_id", "data_type"}),
        ("video_storage_refs", {"measurement_session_id"}),
        ("telegram_links", {"account_id"}),
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


def test_unique_constraint_is_enforced(engine, account) -> None:
    """Constraint harus benar-benar ditegakkan database, bukan sekadar
    dideklarasikan."""
    _, profile_id = account
    with Session(engine) as db:
        db.add(
            DataVisibilitySetting(
                family_member_id=profile_id, data_type="vitals", visibility="private"
            )
        )
        db.commit()

        db.add(
            DataVisibilitySetting(
                family_member_id=profile_id, data_type="vitals", visibility="family"
            )
        )
        with pytest.raises(IntegrityError, match="UNIQUE"):
            db.commit()


def test_embedding_column_is_vector_1536() -> None:
    embedding = Base.metadata.tables["health_facts"].columns["embedding"]
    assert getattr(embedding.type, "dim", None) == 1536


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("family_members", "role"),
        ("family_members", "ui_mode"),
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


def test_enum_check_is_enforced(engine, account) -> None:
    account_id, _ = account
    with Session(engine) as db:
        db.add(
            FamilyMember(
                account_id=account_id, full_name="Salah Role", role="superadmin"
            )
        )
        with pytest.raises(IntegrityError, match="CHECK"):
            db.commit()


def test_vitals_reading_requires_known_metric(engine, account) -> None:
    """metric_type tak dikenal harus ditolak FK, bukan lolos sebagai text bebas."""
    _, profile_id = account
    with Session(engine) as db:
        db.add(
            VitalsReading(
                measurement_session_id=None,
                family_member_id=profile_id,
                metric_type="not_a_real_metric",
                value=60,
            )
        )
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            db.commit()


def test_account_email_unique_ignoring_case(engine) -> None:
    """Keunikan email ditegakkan database, bukan hanya schema Pydantic —
    seed dan import tidak lewat validasi schema."""
    with Session(engine) as db:
        db.add(Account(email="budi@example.com", password_hash="x"))
        db.commit()
        db.add(Account(email="BUDI@example.com", password_hash="y"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_profiles_need_no_credentials(engine, account) -> None:
    """Anggota keluarga tidak punya email/password sendiri: satu akun
    dipakai bersama, profil hanya membedakan siapa datanya."""
    account_id, _ = account
    with Session(engine) as db:
        anak = FamilyMember(account_id=account_id, full_name="Anak")
        db.add(anak)
        db.commit()

        assert not hasattr(anak, "password_hash")
        assert anak.pin_hash is None
        assert anak.role == "member"


def test_many_profiles_share_one_account(engine, account) -> None:
    account_id, _ = account
    with Session(engine) as db:
        db.add_all(
            [
                FamilyMember(account_id=account_id, full_name="Anak 1"),
                FamilyMember(account_id=account_id, full_name="Anak 2"),
            ]
        )
        db.commit()
        assert (
            db.query(FamilyMember).filter_by(account_id=account_id).count() == 3
        )
