"""Endpoint family group: buat, gabung, kelola anggota."""

from __future__ import annotations

import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import get_current_user, require_family_admin
from app.db.models import Family, FamilyMember, User
from app.db.session import get_db
from app.services import statistics
from app.services.visibility import accessible_user_ids
from app.schemas import (
    DashboardMemberResponse,
    DependentCreateRequest,
    FamilyDashboardResponse,
    FamilyCreateRequest,
    FamilyJoinRequest,
    FamilyMemberListResponse,
    FamilyMemberResponse,
    FamilyMemberUpdateRequest,
    FamilyResponse,
    ReadingResponse,
    UserResponse,
)


router = APIRouter()

# Alfabet tanpa karakter yang mudah tertukar saat dibacakan atau disalin
# (0/O, 1/I/L) — kode ini sering dikirim lewat chat atau dibaca lisan.
INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
INVITE_LENGTH = 6
INVITE_PREFIX = "FAM-"


def generate_invite_code() -> str:
    """Kode acak kriptografis — kode yang bisa ditebak berarti orang asing
    bisa masuk ke data kesehatan sebuah keluarga.

    30 bit entropi (~887 juta kombinasi).
    """
    # ponytail: tabrakan kode tidak di-retry; peluangnya ~1:887juta dan
    # UNIQUE di DB tetap mencegah duplikat (muncul sebagai 500). Tambahkan
    # retry loop kalau jumlah family sudah sampai puluhan ribu.
    suffix = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(INVITE_LENGTH))
    return f"{INVITE_PREFIX}{suffix}"


def get_active_membership(
    db: Session, family_id: uuid.UUID, user_id: uuid.UUID
) -> FamilyMember | None:
    return db.execute(
        select(FamilyMember).where(
            FamilyMember.family_id == family_id,
            FamilyMember.user_id == user_id,
            FamilyMember.status == "active",
        )
    ).scalar_one_or_none()


def require_family_access(
    db: Session, family_id: uuid.UUID, user: User
) -> FamilyMember:
    """Anggota aktif mana pun boleh melihat; selain itu 403.

    Family yang tidak ada juga dijawab 403, bukan 404 — kalau dibedakan,
    orang luar bisa menebak-nebak family mana yang eksis.
    """
    membership = get_active_membership(db, family_id, user.id)
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Anda bukan anggota family ini",
        )
    return membership


def count_active_admins(db: Session, family_id: uuid.UUID) -> int:
    return len(
        db.execute(
            select(FamilyMember).where(
                FamilyMember.family_id == family_id,
                FamilyMember.role == "admin",
                FamilyMember.status == "active",
            )
        )
        .scalars()
        .all()
    )


def guard_last_admin(db: Session, membership: FamilyMember) -> None:
    """Cegah family kehilangan admin terakhirnya.

    Tanpa admin, tidak ada yang bisa mengundang, mengubah role, atau
    mengeluarkan anggota — family jadi terkunci permanen.
    """
    if membership.role != "admin":
        return
    if count_active_admins(db, membership.family_id) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Family harus punya minimal satu admin. Angkat admin lain dulu.",
        )


@router.post("", response_model=FamilyResponse, status_code=status.HTTP_201_CREATED)
def create_family(
    payload: FamilyCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Family:
    family = Family(
        name=payload.name,
        invite_code=generate_invite_code(),
        created_by=current_user.id,
    )
    db.add(family)
    db.flush()

    db.add(
        FamilyMember(
            family_id=family.id,
            user_id=current_user.id,
            role="admin",
            status="active",
        )
    )
    db.commit()
    db.refresh(family)
    return family


@router.post("/join", response_model=FamilyResponse)
def join_family(
    payload: FamilyJoinRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Family:
    family = db.execute(
        select(Family).where(Family.invite_code == payload.invite_code)
    ).scalar_one_or_none()
    if family is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Kode undangan tidak valid"
        )

    existing = db.execute(
        select(FamilyMember).where(
            FamilyMember.family_id == family.id,
            FamilyMember.user_id == current_user.id,
        )
    ).scalar_one_or_none()

    if existing is not None:
        if existing.status == "active":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Anda sudah menjadi anggota family ini",
            )
        # Pernah dikeluarkan lalu diundang lagi: aktifkan baris lama, karena
        # UNIQUE(family_id, user_id) melarang baris keanggotaan kedua.
        existing.status = "active"
        existing.role = "member"
        db.commit()
        return family

    db.add(
        FamilyMember(
            family_id=family.id,
            user_id=current_user.id,
            role="member",
            status="active",
        )
    )
    db.commit()
    return family


@router.get("/{family_id}/members", response_model=FamilyMemberListResponse)
def list_members(
    family_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FamilyMemberListResponse:
    require_family_access(db, family_id, current_user)

    rows = db.execute(
        select(FamilyMember, User)
        .join(User, User.id == FamilyMember.user_id)
        .where(
            FamilyMember.family_id == family_id,
            FamilyMember.status == "active",
        )
    ).all()

    return FamilyMemberListResponse(
        members=[
            FamilyMemberResponse(
                user_id=user.id,
                full_name=user.full_name,
                role=membership.role,
                status=membership.status,
                is_dependent=user.is_dependent,
                joined_at=membership.joined_at,
            )
            for membership, user in rows
        ]
    )


@router.post(
    "/{family_id}/dependents",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_dependent(
    family_id: uuid.UUID,
    payload: DependentCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """Buat profil untuk anggota tanpa akun sendiri (anak/lansia), FR-6.3.

    Dibuat tanpa email dan password sehingga tidak bisa login; datanya
    dikelola sepenuhnya oleh admin pembuatnya.
    """
    require_family_admin(db, current_user, family_id)

    dependent = User(
        full_name=payload.full_name,
        date_of_birth=payload.date_of_birth,
        gender=payload.gender,
        height_cm=payload.height_cm,
        weight=payload.weight,
        is_dependent=True,
        managed_by_user_id=current_user.id,
    )
    db.add(dependent)
    db.flush()

    db.add(
        FamilyMember(
            family_id=family_id,
            user_id=dependent.id,
            role="member",
            status="active",
        )
    )
    db.commit()
    db.refresh(dependent)
    return dependent


@router.get("/{family_id}/dashboard", response_model=FamilyDashboardResponse)
def read_family_dashboard(
    family_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FamilyDashboardResponse:
    """Ringkasan status seluruh anggota keluarga (FR-2.4).

    Hanya memuat anggota yang datanya boleh dilihat pemanggil. Anggota
    yang menyetel privat tidak muncul sama sekali — bukan muncul dengan
    data kosong, karena itu justru membocorkan bahwa dia menyembunyikan
    sesuatu.
    """
    require_family_access(db, family_id, current_user)
    visible = accessible_user_ids(db, current_user.id, "vitals")

    rows = db.execute(
        select(FamilyMember, User)
        .join(User, User.id == FamilyMember.user_id)
        .where(
            FamilyMember.family_id == family_id,
            FamilyMember.status == "active",
            FamilyMember.user_id.in_(visible),
        )
    ).all()

    members = []
    for _, user in rows:
        readings = statistics.latest_readings(db, user.id)
        members.append(
            DashboardMemberResponse(
                user_id=user.id,
                full_name=user.full_name,
                last_measurement_at=statistics.last_measurement_at(db, user.id),
                latest=[
                    ReadingResponse(
                        metric_type=r.metric_type,
                        value=float(r.value),
                        unit=r.unit,
                    )
                    for r in readings
                ],
                open_anomalies=_count_open_anomalies(db, user.id),
            )
        )

    return FamilyDashboardResponse(members=members)


def _count_open_anomalies(db: Session, user_id: uuid.UUID) -> int:
    """Anomali yang belum ditanggapi user. Selalu 0 sampai deteksi anomali
    dibangun (Task 13)."""
    from app.db.models import Anomaly

    return db.execute(
        select(func.count())
        .select_from(Anomaly)
        .where(Anomaly.user_id == user_id, Anomaly.status == "new")
    ).scalar_one()


@router.patch("/{family_id}/members/{user_id}", response_model=FamilyMemberResponse)
def update_member_role(
    family_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: FamilyMemberUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FamilyMemberResponse:
    require_family_admin(db, current_user, family_id)

    membership = get_active_membership(db, family_id, user_id)
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Anggota tidak ditemukan"
        )

    if payload.role != "admin":
        guard_last_admin(db, membership)

    membership.role = payload.role
    db.commit()

    user = db.get(User, user_id)
    return FamilyMemberResponse(
        user_id=user.id,
        full_name=user.full_name,
        role=membership.role,
        status=membership.status,
        is_dependent=user.is_dependent,
        joined_at=membership.joined_at,
    )


@router.delete("/{family_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    family_id: uuid.UUID,
    user_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    require_family_admin(db, current_user, family_id)

    membership = get_active_membership(db, family_id, user_id)
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Anggota tidak ditemukan"
        )

    guard_last_admin(db, membership)

    # Ditandai removed, bukan dihapus: baris ini jejak bahwa orang tersebut
    # pernah punya akses ke data keluarga.
    membership.status = "removed"
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
