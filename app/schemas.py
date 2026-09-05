"""Skema request/response.

Model response dibuat eksplisit per field, tidak pernah men-serialisasi
model database langsung — supaya kolom sensitif seperti `password_hash`
tidak ikut terbawa hanya karena ada di tabel.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_serializer,
    field_validator,
)


# Batas wajar tubuh manusia. Nilai di luar ini hampir pasti salah input, dan
# akan meracuni analisis chatbot (mis. estimasi BMI) kalau dibiarkan masuk.
MIN_HEIGHT_CM, MAX_HEIGHT_CM = 30, 300
MIN_WEIGHT_KG, MAX_WEIGHT_KG = 1, 500

MIN_PASSWORD_LENGTH = 8


def as_utc(value: datetime | None) -> datetime | None:
    """Pastikan timestamp membawa penanda zona waktu.

    Postgres mengembalikan datetime beserta zonanya, SQLite tidak. Tanpa
    penyeragaman ini, response dari SQLite kehilangan akhiran `Z` dan
    frontend menafsirkannya sebagai waktu lokal — jam yang ditampilkan
    jadi meleset sesuai zona pengguna.
    """
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class HealthResponse(BaseModel):
    status: str


# --- Auth ------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=128)
    full_name: str = Field(min_length=1, max_length=160)
    date_of_birth: date | None = None
    gender: str | None = None

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        """Email disimpan lowercase supaya "Budi@x.com" dan "budi@x.com"
        tidak bisa jadi dua akun berbeda."""
        return value.lower()

    @field_validator("full_name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("full_name tidak boleh kosong")
        return stripped


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# --- User ------------------------------------------------------------------


class UserResponse(BaseModel):
    """Bentuk publik profil user. `password_hash` sengaja tidak ada di sini."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str | None
    phone: str | None
    full_name: str
    date_of_birth: date | None
    gender: str | None
    height_cm: float | None
    weight: float | None
    is_dependent: bool
    created_at: datetime

    @field_serializer('created_at')
    def _utc(self, value: datetime | None) -> datetime | None:
        return as_utc(value)


class FamilyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name tidak boleh kosong")
        return stripped


class FamilyJoinRequest(BaseModel):
    invite_code: str = Field(min_length=1, max_length=64)


class FamilyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    invite_code: str
    created_at: datetime

    @field_serializer('created_at')
    def _utc(self, value: datetime | None) -> datetime | None:
        return as_utc(value)


class FamilyMemberResponse(BaseModel):
    """Anggota family beserta identitasnya, digabung dari `users`."""

    user_id: uuid.UUID
    full_name: str
    role: str
    status: str
    is_dependent: bool
    joined_at: datetime

    @field_serializer('joined_at')
    def _utc(self, value: datetime | None) -> datetime | None:
        return as_utc(value)


class FamilyMemberListResponse(BaseModel):
    members: list[FamilyMemberResponse]


class FamilyMemberUpdateRequest(BaseModel):
    role: Literal["admin", "member"]


class MeasurementAcceptedResponse(BaseModel):
    """Balasan 202: pemrosesan berjalan di background, bukan inline."""

    session_id: uuid.UUID
    processing_status: str


class MeasurementSessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    capture_method: str
    processing_status: str
    signal_quality_flag: str | None
    signal_quality_score: float | None
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: int | None

    @field_serializer('started_at', 'ended_at')
    def _utc(self, value: datetime | None) -> datetime | None:
        return as_utc(value)


class MeasurementListResponse(BaseModel):
    sessions: list[MeasurementSessionResponse]
    total: int


class ReadingResponse(BaseModel):
    metric_type: str
    value: float
    unit: str | None


class MeasurementResultResponse(BaseModel):
    session_id: uuid.UUID
    recorded_at: datetime | None
    signal_quality_score: float | None
    signal_quality_flag: str | None
    readings: list[ReadingResponse]
    # Wajib ditampilkan di layar hasil (PRD FR-1.6).
    disclaimer: str

    @field_serializer('recorded_at')
    def _utc(self, value: datetime | None) -> datetime | None:
        return as_utc(value)


class TelegramLinkResponse(BaseModel):
    link_code: str
    bot_username: str | None
    expires_at: datetime

    @field_serializer("expires_at")
    def _utc(self, value: datetime | None) -> datetime | None:
        return as_utc(value)


class TelegramStatusResponse(BaseModel):
    is_linked: bool
    linked_at: datetime | None

    @field_serializer("linked_at")
    def _utc(self, value: datetime | None) -> datetime | None:
        return as_utc(value)


ACTIVITY_CATEGORIES = Literal[
    "coffee", "exercise", "smoking", "alcohol", "sleep", "meal", "other"
]


class ActivityCreateRequest(BaseModel):
    category: ACTIVITY_CATEGORIES
    quantity: float | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=1000)
    occurred_at: datetime | None = None
    # Diisi kalau mencatat untuk dependent yang dikelola.
    user_id: uuid.UUID | None = None


class ActivityUpdateRequest(BaseModel):
    category: ACTIVITY_CATEGORIES | None = None
    quantity: float | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=1000)
    occurred_at: datetime | None = None


class ActivityResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    logged_by_user_id: uuid.UUID
    category: str
    quantity: float | None
    unit: str | None
    note: str | None
    source: str
    occurred_at: datetime

    @field_serializer('occurred_at')
    def _utc(self, value: datetime | None) -> datetime | None:
        return as_utc(value)


class ActivityListResponse(BaseModel):
    activities: list[ActivityResponse]
    total: int


class TrendBucket(BaseModel):
    bucket: datetime | str
    avg: float
    min: float
    max: float
    count: int


class TrendResponse(BaseModel):
    metric_type: str
    unit: str | None
    buckets: list[TrendBucket]


class BaselineSummary(BaseModel):
    mean: float
    stddev: float
    is_active: bool


class PeriodComparison(BaseModel):
    avg: float
    change_percent: float | None


class MetricSummary(BaseModel):
    metric_type: str
    unit: str | None
    avg: float
    min: float
    max: float
    count: int
    baseline: BaselineSummary | None
    previous_period: PeriodComparison | None


class PeriodRange(BaseModel):
    start: datetime
    end: datetime


class SummaryResponse(BaseModel):
    period: PeriodRange
    metrics: list[MetricSummary]


class DashboardMemberResponse(BaseModel):
    user_id: uuid.UUID
    full_name: str
    last_measurement_at: datetime | None
    latest: list[ReadingResponse]
    open_anomalies: int

    @field_serializer('last_measurement_at')
    def _utc(self, value: datetime | None) -> datetime | None:
        return as_utc(value)


class FamilyDashboardResponse(BaseModel):
    members: list[DashboardMemberResponse]


class DependentCreateRequest(BaseModel):
    """Profil anggota keluarga yang tidak punya akun sendiri (anak/lansia).

    Tidak ada email maupun password — dependent tidak pernah login,
    profilnya dikelola admin family (ERD §2.1).
    """

    full_name: str = Field(min_length=1, max_length=160)
    date_of_birth: date | None = None
    gender: str | None = Field(default=None, max_length=32)
    height_cm: float | None = Field(default=None, gt=MIN_HEIGHT_CM, lt=MAX_HEIGHT_CM)
    weight: float | None = Field(default=None, gt=MIN_WEIGHT_KG, lt=MAX_WEIGHT_KG)

    @field_validator("full_name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("full_name tidak boleh kosong")
        return stripped


class VisibilitySettingResponse(BaseModel):
    data_type: str
    visibility: str


class VisibilityListResponse(BaseModel):
    settings: list[VisibilitySettingResponse]


class VisibilityUpdateRequest(BaseModel):
    data_type: Literal["vitals", "activities", "all"]
    visibility: Literal["family", "private"]


class UserUpdateRequest(BaseModel):
    """Field yang boleh diubah user sendiri.

    `email`, `is_active`, `is_dependent`, dan `managed_by_user_id` sengaja
    tidak ada — mengubahnya lewat endpoint profil berarti user bisa
    mengambil alih identitas atau status akun orang lain.
    """

    full_name: str | None = Field(default=None, min_length=1, max_length=160)
    phone: str | None = Field(default=None, max_length=32)
    date_of_birth: date | None = None
    gender: str | None = Field(default=None, max_length=32)
    height_cm: float | None = Field(
        default=None, gt=MIN_HEIGHT_CM, lt=MAX_HEIGHT_CM
    )
    weight: float | None = Field(default=None, gt=MIN_WEIGHT_KG, lt=MAX_WEIGHT_KG)

    @field_validator("full_name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("full_name tidak boleh kosong")
        return stripped
