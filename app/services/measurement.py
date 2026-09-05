"""Alur satu sesi pengukuran: video masuk, angka keluar.

Pemrosesan berjalan di luar siklus request karena rPPG butuh waktu (~1 detik
setelah model panas, ~20 detik pada pemanggilan pertama). Status di
`measurement_sessions.processing_status` yang jadi mesin keadaannya:

    pending -> processing -> completed | failed

Kontrak yang dijaga ketat: sesi **tidak pernah** berhenti di `processing`.
Frontend melakukan polling; sesi yang menggantung berarti spinner selamanya.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import MeasurementSession, User, VitalsReading
from app.db.session import SessionLocal
from app.services.rppg import RppgError, SignalQualityError, extract_vitals
from app.services.video_storage import save_video


# Ditampilkan di setiap layar hasil (PRD FR-1.6).
DISCLAIMER = (
    "Hasil ini bersifat informasional dan bukan diagnosis medis. "
    "Untuk keluhan kesehatan, konsultasikan ke tenaga medis profesional."
)


class NotAuthorisedToMeasure(PermissionError):
    """Pemanggil tidak berhak mengukur user tersebut."""


def resolve_subject(db: Session, actor: User, subject_id: uuid.UUID | None) -> User:
    """Tentukan siapa yang diukur.

    Tanpa `subject_id`, subjeknya adalah pemanggil sendiri. Dengan
    `subject_id`, hanya boleh dependent yang dikelola pemanggil — kalau
    tidak, siapa pun bisa menuliskan data kesehatan atas nama orang lain.
    """
    if subject_id is None or subject_id == actor.id:
        return actor

    subject = db.get(User, subject_id)
    if subject is None or subject.managed_by_user_id != actor.id:
        raise NotAuthorisedToMeasure(
            "Hanya bisa mengukur diri sendiri atau anggota yang Anda kelola"
        )
    return subject


def create_session(
    db: Session,
    subject: User,
    actor: User,
    capture_method: str,
    filename: str,
    content: bytes,
) -> MeasurementSession:
    """Buat sesi dan simpan videonya. Belum diproses.

    `user_id` adalah subjek, `initiated_by_user_id` adalah pelaku — pola
    subjek vs aktor dari ERD note 7.
    """
    session = MeasurementSession(
        user_id=subject.id,
        initiated_by_user_id=actor.id,
        capture_method=capture_method,
        started_at=datetime.now(UTC),
        processing_status="pending",
    )
    db.add(session)
    db.flush()

    save_video(db, session, filename, content)
    db.commit()
    db.refresh(session)
    return session


def process_session(session_id: uuid.UUID, db: Session | None = None) -> None:
    """Ekstrak vital sign lalu simpan hasilnya.

    Dijalankan sebagai background task, jadi secara default membuka sesi
    database sendiri: sesi milik request sudah ditutup saat fungsi ini
    berjalan. Parameter `db` ada supaya test bisa menyuntikkan sesinya.

    Semua exception ditangkap dan diterjemahkan jadi status `failed`.
    Membiarkan satu saja lolos berarti sesi tertinggal di `processing` dan
    frontend polling tanpa akhir.
    """
    if db is not None:
        _process_with_session(db, session_id)
        return

    with SessionLocal() as own_session:
        _process_with_session(own_session, session_id)


def _process_with_session(db: Session, session_id: uuid.UUID) -> None:
    session = db.get(MeasurementSession, session_id)
    if session is None:
        return

    video_path = _video_path_of(db, session)
    if video_path is None:
        _mark_failed(db, session, "Berkas video tidak ditemukan")
        return

    session.processing_status = "processing"
    db.commit()

    try:
        result = extract_vitals(video_path)
    except SignalQualityError as exc:
        # Sinyal tidak layak: user perlu mengulang, bukan kesalahan sistem.
        _mark_failed(db, session, str(exc), quality_flag="rejected")
        return
    except Exception as exc:
        # Sengaja menangkap semua: exception apa pun yang lolos akan
        # meninggalkan sesi menggantung di `processing`.
        _mark_failed(db, session, f"Pemrosesan gagal: {exc}")
        return

    _store_result(db, session, result)


def _video_path_of(db: Session, session: MeasurementSession) -> str | None:
    from app.db.models import VideoStorageRef

    ref = db.execute(
        select(VideoStorageRef).where(
            VideoStorageRef.measurement_session_id == session.id
        )
    ).scalar_one_or_none()
    return ref.storage_path if ref else None


def _store_result(db: Session, session: MeasurementSession, result) -> None:
    recorded_at = datetime.now(UTC)

    for reading in result.as_readings():
        db.add(
            VitalsReading(
                measurement_session_id=session.id,
                # Didenormalisasi dari sesi: subjek, bukan yang mengukur.
                # Kalau tertukar, data anak masuk ke grafik orang tuanya.
                user_id=session.user_id,
                recorded_at=recorded_at,
                metric_type=reading["metric_type"],
                value=reading["value"],
                unit=reading["unit"],
            )
        )

    session.signal_quality_score = result.quality_score
    session.signal_quality_flag = result.quality_flag
    session.processing_status = "completed"
    session.ended_at = recorded_at
    db.commit()


def _mark_failed(
    db: Session,
    session: MeasurementSession,
    reason: str,
    quality_flag: str | None = None,
) -> None:
    session.processing_status = "failed"
    session.ended_at = datetime.now(UTC)
    if quality_flag:
        session.signal_quality_flag = quality_flag
    db.commit()
