"""Endpoint sesi pengukuran rPPG."""

from __future__ import annotations

import uuid

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.db.models import MeasurementSession, User, VitalsReading
from app.db.session import get_db
from app.schemas import (
    MeasurementAcceptedResponse,
    MeasurementListResponse,
    MeasurementResultResponse,
    MeasurementSessionResponse,
    ReadingResponse,
)
from app.services.measurement import (
    DISCLAIMER,
    NotAuthorisedToMeasure,
    create_session,
    process_session,
    resolve_subject,
)
from app.services.video_storage import VideoValidationError
from app.services.visibility import accessible_user_ids


router = APIRouter()

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def _background_session(db: Session) -> Session | None:
    """Sesi database yang dipakai background task.

    Di produksi mengembalikan None, sehingga `process_session` membuka
    koneksinya sendiri — sesi request sudah ditutup saat task berjalan.
    Di test, `get_db` dioverride ke satu sesi in-memory yang harus dipakai
    ulang, karena tiap koneksi SQLite in-memory adalah database berbeda.
    """
    from app.db.session import get_db as production_get_db
    from app.main import app

    return db if production_get_db in app.dependency_overrides else None


def _start_measurement(
    db: Session,
    background_tasks: BackgroundTasks,
    actor: User,
    subject_id: uuid.UUID | None,
    capture_method: str,
    file: UploadFile,
) -> MeasurementAcceptedResponse:
    """Jalur bersama untuk upload dan live capture."""
    try:
        subject = resolve_subject(db, actor, subject_id)
    except NotAuthorisedToMeasure as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc

    content = file.file.read()

    try:
        session = create_session(
            db, subject, actor, capture_method, file.filename or "", content
        )
    except VideoValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    # Diproses setelah response terkirim: rPPG terlalu lama untuk siklus
    # request, dan frontend sudah dirancang melakukan polling.
    background_tasks.add_task(process_session, session.id, _background_session(db))

    return MeasurementAcceptedResponse(
        session_id=session.id, processing_status=session.processing_status
    )


@router.post(
    "/upload",
    response_model=MeasurementAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def upload_measurement(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user_id: uuid.UUID | None = Form(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MeasurementAcceptedResponse:
    """Unggah video wajah yang sudah direkam (FR-1.2)."""
    return _start_measurement(
        db, background_tasks, current_user, user_id, "upload", file
    )


@router.post(
    "/live",
    response_model=MeasurementAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def live_measurement(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user_id: uuid.UUID | None = Form(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MeasurementAcceptedResponse:
    """Kirim hasil rekaman live dari browser (FR-1.1).

    Bedanya dengan `/upload` hanya `capture_method`, supaya sumber rekaman
    tetap terlacak untuk analisis kualitas nanti.
    """
    return _start_measurement(db, background_tasks, current_user, user_id, "live", file)


def _get_visible_session(
    db: Session, session_id: uuid.UUID, viewer: User
) -> MeasurementSession:
    session = db.get(MeasurementSession, session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Sesi tidak ditemukan"
        )

    if session.user_id not in accessible_user_ids(db, viewer.id, "vitals"):
        # 404, bukan 403: keberadaan sesi orang lain pun bukan informasi publik.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Sesi tidak ditemukan"
        )
    return session


@router.get("", response_model=MeasurementListResponse)
def list_measurements(
    user_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MeasurementListResponse:
    visible = accessible_user_ids(db, current_user.id, "vitals")
    if user_id is not None:
        # Irisan, bukan pengganti: filter tidak boleh memperluas akses.
        visible &= {user_id}

    condition = MeasurementSession.user_id.in_(visible)
    total = db.execute(
        select(func.count()).select_from(MeasurementSession).where(condition)
    ).scalar_one()

    sessions = (
        db.execute(
            select(MeasurementSession)
            .where(condition)
            .order_by(MeasurementSession.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )

    return MeasurementListResponse(
        sessions=[MeasurementSessionResponse.model_validate(s) for s in sessions],
        total=total,
    )


@router.get("/{session_id}", response_model=MeasurementSessionResponse)
def read_measurement(
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MeasurementSession:
    return _get_visible_session(db, session_id, current_user)


@router.get("/{session_id}/results", response_model=MeasurementResultResponse)
def read_measurement_results(
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MeasurementResultResponse:
    session = _get_visible_session(db, session_id, current_user)

    if session.processing_status != "completed":
        # Bukan 200 dengan array kosong: itu terbaca seolah pengukurannya
        # normal tapi tidak menghasilkan apa-apa.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Hasil belum tersedia (status: {session.processing_status})",
        )

    readings = (
        db.execute(
            select(VitalsReading).where(
                VitalsReading.measurement_session_id == session.id
            )
        )
        .scalars()
        .all()
    )

    return MeasurementResultResponse(
        session_id=session.id,
        recorded_at=readings[0].recorded_at if readings else session.ended_at,
        signal_quality_score=session.signal_quality_score,
        signal_quality_flag=session.signal_quality_flag,
        readings=[
            ReadingResponse(
                metric_type=r.metric_type, value=float(r.value), unit=r.unit
            )
            for r in readings
        ],
        disclaimer=DISCLAIMER,
    )
