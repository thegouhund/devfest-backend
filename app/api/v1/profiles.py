"""Endpoint profil anggota keluarga.

Satu keluarga satu akun (ERD §0): admin membuat profil untuk tiap anggota,
dan tidak ada mekanisme undangan atau bergabung — batas keluarga adalah
batas akun. Yang dulu ditangani `families` sekarang implisit di `account_id`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import (
    get_current_account,
    get_current_profile,
    hash_pin,
    require_admin_profile,
)
from app.db.models import Account, Anomaly, FamilyMember
from app.db.session import get_db
from app.schemas import (
    AccountDashboardResponse,
    DashboardMemberResponse,
    ProfileCreateRequest,
    ProfileListResponse,
    ProfileResponse,
    ProfileUpdateRequest,
    ReadingResponse,
)
from app.services import statistics
from app.services.visibility import accessible_profile_ids


router = APIRouter()


def to_response(profile: FamilyMember) -> ProfileResponse:
    """Bentuk response satu profil.

    `has_pin` diturunkan dari ada-tidaknya hash; hash-nya sendiri tidak
    pernah keluar dari server.
    """
    return ProfileResponse(
        id=profile.id,
        full_name=profile.full_name,
        date_of_birth=profile.date_of_birth,
        gender=profile.gender,
        relationship_label=profile.relationship_label,
        height_cm=float(profile.height_cm) if profile.height_cm is not None else None,
        weight=float(profile.weight) if profile.weight is not None else None,
        role=profile.role,
        ui_mode=profile.ui_mode,
        is_active=profile.is_active,
        has_pin=profile.pin_hash is not None,
        created_at=profile.created_at,
    )


def get_owned_profile(
    db: Session, account_id: uuid.UUID, profile_id: uuid.UUID
) -> FamilyMember:
    """Ambil profil milik akun ini, atau 404.

    Profil milik akun lain dijawab 404 dan bukan 403: 403 memberi tahu
    bahwa id itu ada, dan itu sudah bocor informasi.
    """
    profile = db.get(FamilyMember, profile_id)
    if profile is None or profile.account_id != account_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Profil tidak ditemukan"
        )
    return profile


@router.post("", response_model=ProfileResponse, status_code=status.HTTP_201_CREATED)
def create_profile(
    payload: ProfileCreateRequest,
    admin: FamilyMember = Depends(require_admin_profile),
    db: Session = Depends(get_db),
) -> ProfileResponse:
    """Tambah profil anggota keluarga. Hanya admin (FR-6.4)."""
    profile = FamilyMember(
        account_id=admin.account_id,
        full_name=payload.full_name,
        date_of_birth=payload.date_of_birth,
        gender=payload.gender,
        relationship_label=payload.relationship_label,
        height_cm=payload.height_cm,
        weight=payload.weight,
        ui_mode=payload.ui_mode,
        role="member",
        pin_hash=hash_pin(payload.pin) if payload.pin else None,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return to_response(profile)


@router.get("", response_model=ProfileListResponse)
def list_profiles(
    account: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
) -> ProfileListResponse:
    """Semua profil dalam akun.

    Dipakai layar pilih profil, jadi sengaja memakai token tingkat akun —
    saat memilih, belum ada profil aktif. Yang dikembalikan hanya identitas
    dan penanda terkunci, tanpa data kesehatan apa pun.
    """
    profiles = (
        db.execute(
            select(FamilyMember)
            .where(
                FamilyMember.account_id == account.id,
                FamilyMember.is_active.is_(True),
            )
            .order_by(FamilyMember.created_at)
        )
        .scalars()
        .all()
    )
    return ProfileListResponse(profiles=[to_response(p) for p in profiles])


@router.get("/me", response_model=ProfileResponse)
def read_active_profile(
    profile: FamilyMember = Depends(get_current_profile),
) -> ProfileResponse:
    return to_response(profile)


@router.patch("/{profile_id}", response_model=ProfileResponse)
def update_profile(
    profile_id: uuid.UUID,
    payload: ProfileUpdateRequest,
    actor: FamilyMember = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> ProfileResponse:
    """Ubah data profil.

    Boleh: profilnya sendiri, atau admin atas profil mana pun di akunnya.
    Sesama anggota biasa tidak — satu akun bukan berarti boleh saling
    mengubah tinggi badan dan tanggal lahir.
    """
    profile = get_owned_profile(db, actor.account_id, profile_id)

    if profile.id != actor.id and actor.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Hanya profil sendiri atau admin yang bisa mengubah ini",
        )

    # exclude_unset supaya field yang tidak dikirim tetap seperti semula,
    # bukan tertimpa None.
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)

    db.commit()
    db.refresh(profile)
    return to_response(profile)


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def deactivate_profile(
    profile_id: uuid.UUID,
    admin: FamilyMember = Depends(require_admin_profile),
    db: Session = Depends(get_db),
) -> Response:
    """Nonaktifkan profil. Hanya admin.

    Ditandai tidak aktif, bukan dihapus: menghapusnya ikut menghapus
    seluruh riwayat kesehatan orang itu lewat cascade.
    """
    profile = get_owned_profile(db, admin.account_id, profile_id)

    # Akun tanpa admin tidak bisa lagi mengelola profil apa pun — tidak ada
    # jalan keluarnya karena tidak ada login terpisah per profil.
    if profile.role == "admin" and _count_active_admins(db, admin.account_id) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Akun harus punya minimal satu admin aktif",
        )

    profile.is_active = False
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/dashboard/family", response_model=AccountDashboardResponse)
def read_family_dashboard(
    profile: FamilyMember = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> AccountDashboardResponse:
    """Ringkasan status seluruh anggota keluarga (FR-2.4).

    Hanya memuat profil yang datanya boleh dilihat pemanggil. Profil yang
    menyetel privat tidak muncul sama sekali — bukan muncul dengan data
    kosong, karena itu justru membocorkan bahwa dia menyembunyikan sesuatu.
    """
    visible = accessible_profile_ids(db, profile.id, "vitals")

    rows = (
        db.execute(
            select(FamilyMember).where(
                FamilyMember.account_id == profile.account_id,
                FamilyMember.is_active.is_(True),
                FamilyMember.id.in_(visible),
            )
        )
        .scalars()
        .all()
    )

    members = [
        DashboardMemberResponse(
            family_member_id=member.id,
            full_name=member.full_name,
            last_measurement_at=statistics.last_measurement_at(db, member.id),
            latest=[
                ReadingResponse(
                    metric_type=r.metric_type,
                    value=float(r.value),
                    unit=r.unit,
                )
                for r in statistics.latest_readings(db, member.id)
            ],
            open_anomalies=_count_open_anomalies(db, member.id),
        )
        for member in rows
    ]

    return AccountDashboardResponse(members=members)


def _count_open_anomalies(db: Session, family_member_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count())
        .select_from(Anomaly)
        .where(
            Anomaly.family_member_id == family_member_id,
            Anomaly.status == "new",
        )
    ).scalar_one()


def _count_active_admins(db: Session, account_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count())
        .select_from(FamilyMember)
        .where(
            FamilyMember.account_id == account_id,
            FamilyMember.role == "admin",
            FamilyMember.is_active.is_(True),
        )
    ).scalar_one()
