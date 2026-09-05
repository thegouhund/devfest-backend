"""Endpoint anomali: daftar, detail, dan penandaan status."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import get_current_profile
from app.db.models import ActivityLog, Anomaly, FamilyMember
from app.db.session import get_db
from app.schemas import (
    AnomalyDetailResponse,
    AnomalyListResponse,
    AnomalyResponse,
    AnomalyUpdateRequest,
    RelatedActivityResponse,
)
from app.services.visibility import accessible_profile_ids, same_account


router = APIRouter()

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# Anomali diturunkan dari data vital, jadi ikut setelan privasi `vitals`.
# Kalau dipisah, angka yang disembunyikan bocor lewat alert-nya.
VISIBILITY_SCOPE = "vitals"


def _visible_scope(
    db: Session, viewer: FamilyMember, family_member_id: uuid.UUID | None
) -> set[uuid.UUID]:
    visible = accessible_profile_ids(db, viewer.id, VISIBILITY_SCOPE)

    if family_member_id is None:
        return visible

    if family_member_id not in visible:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Anda tidak punya akses ke data user ini",
        )
    return {family_member_id}


def _get_visible_anomaly(
    db: Session, anomaly_id: uuid.UUID, viewer: FamilyMember
) -> Anomaly:
    anomaly = db.get(Anomaly, anomaly_id)
    if anomaly is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Anomali tidak ditemukan"
        )

    if anomaly.family_member_id not in accessible_profile_ids(db, viewer.id, VISIBILITY_SCOPE):
        # 404, bukan 403: keberadaan anomali orang lain pun bukan informasi
        # publik.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Anomali tidak ditemukan"
        )
    return anomaly


@router.get("", response_model=AnomalyListResponse)
def list_anomalies(
    status_filter: str | None = Query(default=None, alias="status"),
    metric_type: str | None = Query(default=None),
    family_member_id: uuid.UUID | None = Query(default=None),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    current_profile: FamilyMember = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> AnomalyListResponse:
    scope = _visible_scope(db, current_profile, family_member_id)

    conditions = [Anomaly.family_member_id.in_(scope)]
    if status_filter:
        conditions.append(Anomaly.status == status_filter)
    if metric_type:
        conditions.append(Anomaly.metric_type == metric_type)
    if start:
        conditions.append(Anomaly.detected_at >= start)
    if end:
        conditions.append(Anomaly.detected_at <= end)

    total = db.execute(
        select(func.count()).select_from(Anomaly).where(*conditions)
    ).scalar_one()

    rows = (
        db.execute(
            select(Anomaly)
            .where(*conditions)
            .order_by(Anomaly.detected_at.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )

    return AnomalyListResponse(
        anomalies=[AnomalyResponse.model_validate(r) for r in rows], total=total
    )


@router.get("/{anomaly_id}", response_model=AnomalyDetailResponse)
def read_anomaly(
    anomaly_id: uuid.UUID,
    current_profile: FamilyMember = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> AnomalyDetailResponse:
    """Detail anomali beserta konteks penyebabnya (FR-3.3)."""
    anomaly = _get_visible_anomaly(db, anomaly_id, current_profile)

    activity = None
    if anomaly.related_activity_id is not None:
        row = db.get(ActivityLog, anomaly.related_activity_id)
        if row is not None:
            activity = RelatedActivityResponse.model_validate(row)

    return AnomalyDetailResponse(
        **AnomalyResponse.model_validate(anomaly).model_dump(),
        measurement_session_id=anomaly.measurement_session_id,
        related_activity=activity,
    )


@router.patch("/{anomaly_id}", response_model=AnomalyResponse)
def update_anomaly_status(
    anomaly_id: uuid.UUID,
    payload: AnomalyUpdateRequest,
    current_profile: FamilyMember = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> Anomaly:
    """Tandai anomali sudah dibaca atau diabaikan.

    Hanya subjeknya atau admin yang mengelolanya — boleh melihat tidak
    berarti boleh menandai sudah dibaca atas nama orang lain.
    """
    anomaly = _get_visible_anomaly(db, anomaly_id, current_profile)

    if not _may_change_status(db, anomaly, current_profile):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Hanya pemilik data atau pengelolanya yang bisa mengubah status",
        )

    anomaly.status = payload.status
    db.commit()
    db.refresh(anomaly)
    return anomaly


def _may_change_status(db: Session, anomaly: Anomaly, actor: FamilyMember) -> bool:
    """Menutup anomali adalah keputusan medis kecil, jadi dibatasi ke
    subjeknya sendiri atau admin akun — bukan setiap anggota yang kebetulan
    bisa melihatnya."""
    if anomaly.family_member_id == actor.id:
        return True
    return actor.role == "admin" and same_account(
        db, actor.id, anomaly.family_member_id
    )
