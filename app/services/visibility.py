"""Otoritas tunggal soal profil mana yang datanya boleh dilihat (PRD FR-6.2).

Setiap endpoint yang mengembalikan vitals, aktivitas, atau anomali WAJIB
memfilter lewat `accessible_profile_ids`. Aturannya sengaja hanya ada di
satu tempat: pengecekan yang tersebar di tiap endpoint pasti terlewat di
salah satunya, dan yang terlewat itu berarti data medis bocor.

Model akun & profil: batas terluar adalah akun. Profil dari akun lain tidak
pernah terlihat, apa pun setelan privasinya.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DataVisibilitySetting, FamilyMember


# Nilai default saat profil belum pernah diatur (PRD FR-6.2).
DEFAULT_VISIBILITY = "family"

DATA_TYPES = ("vitals", "activities")


def accessible_profile_ids(
    db: Session, viewer_profile_id: uuid.UUID, data_type: str
) -> set[uuid.UUID]:
    """Kumpulan profil yang datanya boleh dilihat `viewer_profile_id`.

    Isinya:
    - dirinya sendiri — selalu, apa pun setelan privasinya
    - profil lain dalam akun yang sama yang setelan `data_type`-nya `family`
    - seluruh profil dalam akun kalau pemanggilnya admin — admin memang
      pengelola akun (FR-6.4)

    Mengembalikan himpunan kosong kalau profilnya tidak valid.
    """
    viewer = db.get(FamilyMember, viewer_profile_id)
    if viewer is None:
        return set()

    visible = {viewer_profile_id}

    sibling_ids = set(
        db.execute(
            select(FamilyMember.id).where(
                FamilyMember.account_id == viewer.account_id,
                FamilyMember.is_active.is_(True),
            )
        )
        .scalars()
        .all()
    )
    candidates = sibling_ids - visible
    if not candidates:
        return visible

    if viewer.role == "admin":
        # Admin mengelola seluruh akun, termasuk profil yang ditandai privat
        # dari dashboard gabungan.
        visible.update(candidates)
        return visible

    visible.update(
        candidate
        for candidate in candidates
        if resolve_visibility(db, candidate, data_type) == "family"
    )
    return visible


def resolve_visibility(db: Session, profile_id: uuid.UUID, data_type: str) -> str:
    """Setelan efektif satu profil untuk satu jenis data.

    Setelan spesifik (`vitals`/`activities`) mengalahkan `all` yang lebih
    umum; kalau keduanya tidak ada, jatuh ke default.
    """
    rows = (
        db.execute(
            select(DataVisibilitySetting).where(
                DataVisibilitySetting.family_member_id == profile_id,
                DataVisibilitySetting.data_type.in_((data_type, "all")),
            )
        )
        .scalars()
        .all()
    )
    by_type = {row.data_type: row.visibility for row in rows}
    return by_type.get(data_type) or by_type.get("all") or DEFAULT_VISIBILITY


def can_view(
    db: Session, viewer_profile_id: uuid.UUID, subject_id: uuid.UUID, data_type: str
) -> bool:
    """Versi satu-subjek dari `accessible_profile_ids`, untuk endpoint detail."""
    return subject_id in accessible_profile_ids(db, viewer_profile_id, data_type)


def same_account(db: Session, profile_id: uuid.UUID, other_id: uuid.UUID) -> bool:
    """True kalau kedua profil berada di akun yang sama.

    Dipakai untuk aksi tulis (mis. mencatat aktivitas atas nama profil
    lain), yang batasnya akun — bukan setelan visibility yang hanya
    mengatur tampilan.
    """
    profile = db.get(FamilyMember, profile_id)
    other = db.get(FamilyMember, other_id)
    return (
        profile is not None
        and other is not None
        and profile.account_id == other.account_id
    )
