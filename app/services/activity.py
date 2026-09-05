"""Pencatatan aktivitas harian (PRD FR-7.1, FR-7.3).

Ada dua pintu masuk ke data yang sama: tombol quick-menu lewat REST, dan
chatbot yang mem-parse kalimat seperti "baru ngopi 2 cangkir" (FR-4.3).
Keduanya memanggil fungsi di modul ini, jadi aturan izin dan validasi
berlaku identik — kalau aturannya diletakkan di endpoint, jalur chatbot
akan melewatinya begitu saja.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import ActivityLog, User
from app.services.visibility import accessible_user_ids


class NotAuthorisedToLog(PermissionError):
    """Pemanggil tidak berhak mencatat atau mengubah data user tersebut."""


DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def resolve_subject(db: Session, actor: User, subject_id: uuid.UUID | None) -> User:
    """Tentukan pemilik data aktivitas.

    Tanpa `subject_id`, subjeknya pemanggil sendiri. Dengan `subject_id`,
    hanya boleh dependent yang dikelola pemanggil.
    """
    if subject_id is None or subject_id == actor.id:
        return actor

    subject = db.get(User, subject_id)
    if subject is None or subject.managed_by_user_id != actor.id:
        raise NotAuthorisedToLog(
            "Hanya bisa mencatat untuk diri sendiri atau anggota yang Anda kelola"
        )
    return subject


def create_activity(
    db: Session,
    *,
    actor: User,
    subject_id: uuid.UUID | None,
    category: str,
    quantity: float | None,
    unit: str | None,
    note: str | None,
    occurred_at: datetime | None,
    source: str,
) -> ActivityLog:
    """Catat satu aktivitas. Pemanggil yang melakukan `commit`.

    `source` membedakan asalnya: `menu` dari tombol, `chat` dari chatbot.
    """
    subject = resolve_subject(db, actor, subject_id)

    activity = ActivityLog(
        user_id=subject.id,
        logged_by_user_id=actor.id,
        category=category,
        quantity=quantity,
        unit=unit,
        note=note,
        # Default sekarang, tapi user sering mencatat setelah kejadian —
        # waktunya bisa dikoreksi (FR-7.1).
        occurred_at=occurred_at or datetime.now(UTC),
        source=source,
    )
    db.add(activity)
    db.flush()
    return activity


def list_activities(
    db: Session,
    *,
    viewer_id: uuid.UUID,
    subject_id: uuid.UUID | None = None,
    category: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> tuple[list[ActivityLog], int]:
    """Daftar aktivitas yang boleh dilihat `viewer_id`, beserta totalnya.

    Melempar `NotAuthorisedToLog` kalau `subject_id` di luar jangkauan —
    daftar kosong akan terbaca seolah orangnya belum pernah mencatat apa pun.
    """
    visible = accessible_user_ids(db, viewer_id, "activities")

    if subject_id is not None:
        if subject_id not in visible:
            raise NotAuthorisedToLog("Anda tidak punya akses ke data user ini")
        visible = {subject_id}

    conditions = [ActivityLog.user_id.in_(visible)]
    if category:
        conditions.append(ActivityLog.category == category)
    if start:
        conditions.append(ActivityLog.occurred_at >= start)
    if end:
        conditions.append(ActivityLog.occurred_at <= end)

    total = db.execute(
        select(func.count()).select_from(ActivityLog).where(*conditions)
    ).scalar_one()

    rows = (
        db.execute(
            select(ActivityLog)
            .where(*conditions)
            .order_by(ActivityLog.occurred_at.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return rows, total


def get_editable_activity(
    db: Session, activity_id: uuid.UUID, actor: User
) -> ActivityLog:
    """Ambil aktivitas yang boleh diubah `actor`.

    Boleh melihat tidak berarti boleh mengubah: hanya subjeknya sendiri
    atau admin yang mengelolanya.
    """
    activity = db.get(ActivityLog, activity_id)
    if activity is None:
        raise LookupError("Aktivitas tidak ditemukan")

    if activity.user_id == actor.id:
        return activity

    subject = db.get(User, activity.user_id)
    if subject is not None and subject.managed_by_user_id == actor.id:
        return activity

    raise NotAuthorisedToLog("Anda tidak berhak mengubah aktivitas ini")


def update_activity(db: Session, activity: ActivityLog, changes: dict) -> ActivityLog:
    """Terapkan perubahan. Pemanggil yang melakukan `commit`."""
    for field, value in changes.items():
        setattr(activity, field, value)
    db.flush()
    return activity


def delete_activity(db: Session, activity: ActivityLog) -> None:
    """Hapus permanen.

    Berbeda dari keanggotaan family yang ditandai `removed`: aktivitas
    salah catat memang perlu benar-benar hilang, karena ikut terhitung
    sebagai konteks anomali.
    """
    db.delete(activity)
    db.flush()
