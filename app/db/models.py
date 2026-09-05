"""Model SQLAlchemy untuk seluruh tabel di ERD.md §2.

Konvensi:
- `user_id` = subjek data, `*_by_user_id` = pelaku input (ERD note 7)
- `metric_type` = FK ke `metric_types.code`, bukan enum Python (ERD note 2)
- Nilai enum dibatasi lewat CHECK constraint (ERD §0)
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
    column,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


def pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=new_uuid)


def created_at_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


def one_of(column: str, *values: str) -> CheckConstraint:
    """CHECK constraint untuk kolom enum (ERD §3)."""
    allowed = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=f"ck_{column}_valid")


# --- Akun & keluarga -------------------------------------------------------


class User(Base):
    """ERD §2.1. Dependent = profil tanpa login, dikelola admin lewat
    self-FK `managed_by_user_id`, jadi tidak perlu tabel terpisah."""

    __tablename__ = "users"
    __table_args__ = (
        # Keunikan email ditegakkan tanpa memandang huruf besar-kecil, di
        # level database. Normalisasi di schema saja tidak cukup: jalur lain
        # (seed, import, pembuatan profil dependent) tidak melewatinya.
        Index(
            "uq_users_email_lower",
            func.lower(column("email")),
            unique=True,
            sqlite_where=column("email").isnot(None),
            postgresql_where=column("email").isnot(None),
        ),
    )

    id: Mapped[uuid.UUID] = pk()
    # Nullable: dependent tidak punya kredensial sendiri.
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    gender: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_dependent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    managed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    # Konteks fisik untuk analisis chatbot, mis. estimasi BMI (PRD FR-4.1).
    height_cm: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    weight: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    dependents: Mapped[list[User]] = relationship(remote_side=[managed_by_user_id])


class Family(Base):
    """ERD §2.2."""

    __tablename__ = "families"

    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    invite_code: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = created_at_column()


class FamilyMember(Base):
    """ERD §2.3. Many-to-many user ↔ family, sudah multi-family-ready."""

    __tablename__ = "family_members"
    __table_args__ = (
        UniqueConstraint("family_id", "user_id", name="uq_family_member"),
        one_of("role", "admin", "member"),
        one_of("status", "active", "removed"),
    )

    id: Mapped[uuid.UUID] = pk()
    family_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("families.id"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, default="active", nullable=False)
    joined_at: Mapped[datetime] = created_at_column()


# --- Pengukuran ------------------------------------------------------------


class MeasurementSession(Base):
    """ERD §2.4. Satu sesi pengukuran rPPG."""

    __tablename__ = "measurement_sessions"
    __table_args__ = (
        one_of("capture_method", "live", "upload"),
        one_of("signal_quality_flag", "good", "fair", "poor", "rejected"),
        one_of("processing_status", "pending", "processing", "completed", "failed"),
    )

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    initiated_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    capture_method: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signal_quality_score: Mapped[float | None] = mapped_column(
        Numeric(4, 3), nullable=True
    )
    signal_quality_flag: Mapped[str | None] = mapped_column(Text, nullable=True)
    processing_status: Mapped[str] = mapped_column(
        Text, default="pending", nullable=False
    )
    created_at: Mapped[datetime] = created_at_column()


class VideoStorageRef(Base):
    """ERD §2.5. Metadata video; file fisik ada di filesystem VPS.

    Terpisah dari `measurement_sessions` supaya kebijakan retensi/lokasi
    penyimpanan bisa berubah tanpa menyentuh tabel sesi.
    """

    __tablename__ = "video_storage_refs"
    __table_args__ = (one_of("storage_provider", "vps_local", "s3", "minio"),)

    id: Mapped[uuid.UUID] = pk()
    measurement_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("measurement_sessions.id"), unique=True, nullable=False
    )
    storage_provider: Mapped[str] = mapped_column(
        Text, default="vps_local", nullable=False
    )
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Referensi ke key management, bukan key mentah.
    encryption_key_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    retention_policy: Mapped[str] = mapped_column(
        Text, default="indefinite", nullable=False
    )
    created_at: Mapped[datetime] = created_at_column()
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MetricType(Base):
    """ERD §2.6. Tabel lookup pengganti enum hardcode — menambah metrik
    baru cukup satu INSERT, tanpa migrasi skema."""

    __tablename__ = "metric_types"
    __table_args__ = (one_of("category", "vital", "derived", "experimental", "other"),)

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    default_unit: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, default="vital", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = created_at_column()


class VitalsReading(Base):
    """ERD §2.7. Long-format: satu baris per metrik per waktu, jadi metrik
    baru tidak perlu kolom baru. Hypertable Timescale di `recorded_at`.

    PK-nya composite `(id, recorded_at)` — bukan `id` saja seperti tabel lain —
    karena TimescaleDB mewajibkan kolom partisi ikut dalam primary key.
    `id` tetap unik lewat UUID, jadi secara praktis tetap berperilaku
    sebagai identifier tunggal.
    """

    __tablename__ = "vitals_readings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_uuid)
    measurement_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("measurement_sessions.id"), nullable=True, index=True
    )
    # Didenormalisasi dari session supaya query dashboard tidak perlu join.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, primary_key=True, nullable=False
    )
    metric_type: Mapped[str] = mapped_column(
        ForeignKey("metric_types.code"), nullable=False, index=True
    )
    value: Mapped[float] = mapped_column(Numeric, nullable=False)
    unit: Mapped[str | None] = mapped_column(Text, nullable=True)


# --- Baseline & anomali ----------------------------------------------------


class Baseline(Base):
    """ERD §2.8. Baseline statistik personal untuk deteksi anomali."""

    __tablename__ = "baselines"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "metric_type", "window_end", name="uq_baseline_window"
        ),
    )

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    metric_type: Mapped[str] = mapped_column(
        ForeignKey("metric_types.code"), nullable=False
    )
    mean_value: Mapped[float] = mapped_column(Numeric, nullable=False)
    stddev_value: Mapped[float] = mapped_column(Numeric, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # True hanya setelah cold-start terpenuhi (PRD A3).
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    computed_at: Mapped[datetime] = created_at_column()


class Anomaly(Base):
    """ERD §2.9."""

    __tablename__ = "anomalies"
    __table_args__ = (
        one_of("severity", "low", "medium", "high"),
        one_of("status", "new", "acknowledged", "dismissed"),
    )

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    measurement_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("measurement_sessions.id"), nullable=True
    )
    # Aktivitas terdekat sebagai konteks penyebab (FR-3.3).
    related_activity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("activities_log.id"), nullable=True
    )
    metric_type: Mapped[str] = mapped_column(
        ForeignKey("metric_types.code"), nullable=False
    )
    observed_value: Mapped[float] = mapped_column(Numeric, nullable=False)
    baseline_mean: Mapped[float] = mapped_column(Numeric, nullable=False)
    baseline_stddev: Mapped[float] = mapped_column(Numeric, nullable=False)
    deviation_score: Mapped[float] = mapped_column(Numeric, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, default="new", nullable=False)
    detected_at: Mapped[datetime] = created_at_column()


class ActivityLog(Base):
    """ERD §2.10. Satu model untuk dua entry point: quick-menu & chat."""

    __tablename__ = "activities_log"
    __table_args__ = (
        one_of(
            "category",
            "coffee",
            "exercise",
            "smoking",
            "alcohol",
            "sleep",
            "meal",
            "other",
        ),
        one_of("source", "menu", "chat"),
    )

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    logged_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    category: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    unit: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    # Bisa diedit manual user, jadi tidak selalu sama dengan created_at.
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    created_at: Mapped[datetime] = created_at_column()


# --- Chatbot ---------------------------------------------------------------


class ConversationLog(Base):
    """ERD §2.11."""

    __tablename__ = "conversation_log"

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = created_at_column()
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConversationMessage(Base):
    """ERD §2.12. Audit trail; sumber memory sebenarnya ada di health_facts."""

    __tablename__ = "conversation_messages"
    __table_args__ = (one_of("role", "user", "assistant", "system", "tool"),)

    id: Mapped[uuid.UUID] = pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation_log.id"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at_column()


class HealthFact(Base):
    """ERD §2.13. Fakta hasil ekstraksi chat untuk RAG.

    Subjek fakta (`user_id`) bisa beda dari yang menceritakan
    (`reported_by_user_id`) — mis. orang tua bercerita soal anaknya.
    """

    __tablename__ = "health_facts"
    __table_args__ = (
        one_of(
            "fact_category",
            "medical_history",
            "allergy",
            "symptom",
            "family_history",
            "medication",
            "other",
        ),
    )

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    reported_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversation_log.id"), nullable=True
    )
    fact_text: Mapped[str] = mapped_column(Text, nullable=False)
    fact_category: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(
        Numeric(3, 2), nullable=True
    )
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


# --- Notifikasi & privasi --------------------------------------------------


class TelegramLink(Base):
    """ERD §2.14."""

    __tablename__ = "telegram_links"

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), unique=True, nullable=False
    )
    # Nullable sampai kode ditukarkan: saat kode diterbitkan, chat id-nya
    # memang belum diketahui — baru terisi ketika user mengirim kode ke bot.
    telegram_chat_id: Mapped[str | None] = mapped_column(
        Text, unique=True, nullable=True
    )
    # Kode sementara saat linking, jadi NULL setelah terhubung.
    link_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Kode linking kedaluwarsa: kode yang bocor lewat screenshot atau chat
    # tidak boleh berlaku selamanya, karena siapa pun yang memakainya akan
    # menerima notifikasi kesehatan keluarga ini.
    link_code_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    linked_at: Mapped[datetime] = created_at_column()


class Notification(Base):
    """ERD §2.15. Log pengiriman untuk audit & debugging delivery."""

    __tablename__ = "notifications"
    __table_args__ = (
        one_of("channel", "telegram", "in_app"),
        one_of("status", "sent", "failed", "pending"),
    )

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    anomaly_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("anomalies.id"), nullable=True
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, default="pending", nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at_column()


class DataVisibilitySetting(Base):
    """ERD §2.16. Kontrol privasi per user per jenis data (FR-6.2)."""

    __tablename__ = "data_visibility_settings"
    __table_args__ = (
        UniqueConstraint("user_id", "data_type", name="uq_visibility_per_type"),
        one_of("data_type", "vitals", "activities", "all"),
        one_of("visibility", "family", "private"),
    )

    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    data_type: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(Text, default="family", nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
