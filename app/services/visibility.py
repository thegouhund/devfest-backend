"""Otoritas tunggal soal siapa boleh melihat data kesehatan siapa (PRD FR-6.2).

Setiap endpoint yang mengembalikan vitals, aktivitas, atau anomali WAJIB
memfilter lewat `accessible_user_ids`. Aturannya sengaja hanya ada di satu
tempat: pengecekan yang tersebar di tiap endpoint pasti terlewat di salah
satunya, dan yang terlewat itu berarti data medis bocor antar anggota keluarga.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DataVisibilitySetting, FamilyMember, User


# Nilai default saat user belum pernah mengatur apa pun (PRD FR-6.2).
DEFAULT_VISIBILITY = "family"

DATA_TYPES = ("vitals", "activities")


def accessible_user_ids(
    db: Session, viewer_id: uuid.UUID, data_type: str
) -> set[uuid.UUID]:
    """Kumpulan user id yang datanya boleh dilihat `viewer_id`.

    Isinya:
    - diri sendiri — selalu, apa pun setelan privasinya
    - dependent yang dikelola viewer — admin bertanggung jawab atas kesehatan
      mereka, jadi setelan privat tidak menyembunyikan dari pengelolanya
    - sesama anggota family aktif yang setelan `data_type`-nya `family`

    Mengembalikan himpunan kosong kalau `viewer_id` bukan user yang valid.

    Anggota yang akunnya dinonaktifkan (`is_active=false`) tetap terlihat:
    nonaktif berarti tidak bisa login, bukan riwayat kesehatannya dihapus
    dari dashboard keluarga. Keanggotaan yang `removed` beda hal — itu
    memang mencabut akses.
    """
    viewer = db.get(User, viewer_id)
    if viewer is None:
        return set()

    visible = {viewer_id}

    # Dependent yang dikelola viewer, terlepas dari setelan privasinya.
    managed = (
        db.execute(select(User.id).where(User.managed_by_user_id == viewer_id))
        .scalars()
        .all()
    )
    visible.update(managed)

    family_ids = (
        db.execute(
            select(FamilyMember.family_id).where(
                FamilyMember.user_id == viewer_id,
                FamilyMember.status == "active",
            )
        )
        .scalars()
        .all()
    )
    if not family_ids:
        return visible

    sibling_ids = set(
        db.execute(
            select(FamilyMember.user_id).where(
                FamilyMember.family_id.in_(family_ids),
                FamilyMember.status == "active",
            )
        )
        .scalars()
        .all()
    )
    candidates = sibling_ids - visible
    if not candidates:
        return visible

    visible.update(
        candidate
        for candidate in candidates
        if resolve_visibility(db, candidate, data_type) == "family"
    )
    return visible


def resolve_visibility(db: Session, user_id: uuid.UUID, data_type: str) -> str:
    """Setelan efektif seorang user untuk satu jenis data.

    Setelan spesifik (`vitals`/`activities`) mengalahkan `all` yang lebih
    umum; kalau keduanya tidak ada, jatuh ke default.
    """
    rows = (
        db.execute(
            select(DataVisibilitySetting).where(
                DataVisibilitySetting.user_id == user_id,
                DataVisibilitySetting.data_type.in_((data_type, "all")),
            )
        )
        .scalars()
        .all()
    )
    by_type = {row.data_type: row.visibility for row in rows}
    return by_type.get(data_type) or by_type.get("all") or DEFAULT_VISIBILITY


def can_view(db: Session, viewer_id: uuid.UUID, subject_id: uuid.UUID, data_type: str) -> bool:
    """Versi satu-subjek dari `accessible_user_ids`, untuk endpoint detail."""
    return subject_id in accessible_user_ids(db, viewer_id, data_type)


def manages_user(db: Session, manager_id: uuid.UUID, subject_id: uuid.UUID) -> bool:
    """True kalau `subject_id` adalah dependent yang dikelola `manager_id`."""
    subject = db.get(User, subject_id)
    return subject is not None and subject.managed_by_user_id == manager_id
