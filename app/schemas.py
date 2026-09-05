"""Skema request/response.

Model response dibuat eksplisit per field, tidak pernah men-serialisasi
model database langsung — supaya kolom sensitif seperti `password_hash`
tidak ikut terbawa hanya karena ada di tabel.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


# Batas wajar tubuh manusia. Nilai di luar ini hampir pasti salah input, dan
# akan meracuni analisis chatbot (mis. estimasi BMI) kalau dibiarkan masuk.
MIN_HEIGHT_CM, MAX_HEIGHT_CM = 30, 300
MIN_WEIGHT_KG, MAX_WEIGHT_KG = 1, 500

MIN_PASSWORD_LENGTH = 8


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


class FamilyMemberResponse(BaseModel):
    """Anggota family beserta identitasnya, digabung dari `users`."""

    user_id: uuid.UUID
    full_name: str
    role: str
    status: str
    is_dependent: bool
    joined_at: datetime


class FamilyMemberListResponse(BaseModel):
    members: list[FamilyMemberResponse]


class FamilyMemberUpdateRequest(BaseModel):
    role: Literal["admin", "member"]


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
