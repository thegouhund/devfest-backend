"""Endpoint pencatatan aktivitas harian."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.schemas import (
    ActivityCreateRequest,
    ActivityListResponse,
    ActivityResponse,
    ActivityUpdateRequest,
)
from app.services import activity as activity_service
from app.services.activity import NotAuthorisedToLog


router = APIRouter()

# Entri lewat REST berasal dari tombol quick-menu; chatbot memakai lapisan
# service yang sama dengan source `chat` (FR-4.3).
SOURCE_MENU = "menu"


@router.post("", response_model=ActivityResponse, status_code=status.HTTP_201_CREATED)
def create_activity(
    payload: ActivityCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ActivityResponse:
    try:
        activity = activity_service.create_activity(
            db,
            actor=current_user,
            subject_id=payload.user_id,
            category=payload.category,
            quantity=payload.quantity,
            unit=payload.unit,
            note=payload.note,
            occurred_at=payload.occurred_at,
            source=SOURCE_MENU,
        )
    except NotAuthorisedToLog as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc

    db.commit()
    db.refresh(activity)
    return ActivityResponse.model_validate(activity)


@router.get("", response_model=ActivityListResponse)
def list_activities(
    category: str | None = Query(default=None),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    user_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(
        default=activity_service.DEFAULT_PAGE_SIZE,
        ge=1,
        le=activity_service.MAX_PAGE_SIZE,
    ),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ActivityListResponse:
    try:
        rows, total = activity_service.list_activities(
            db,
            viewer_id=current_user.id,
            subject_id=user_id,
            category=category,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )
    except NotAuthorisedToLog as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc

    return ActivityListResponse(
        activities=[ActivityResponse.model_validate(r) for r in rows], total=total
    )


def _load_editable(
    db: Session, activity_id: uuid.UUID, current_user: User
):
    try:
        return activity_service.get_editable_activity(db, activity_id, current_user)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except NotAuthorisedToLog as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc


@router.patch("/{activity_id}", response_model=ActivityResponse)
def update_activity(
    activity_id: uuid.UUID,
    payload: ActivityUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ActivityResponse:
    activity = _load_editable(db, activity_id, current_user)

    # exclude_unset supaya field yang tidak dikirim tetap seperti semula.
    activity_service.update_activity(
        db, activity, payload.model_dump(exclude_unset=True)
    )
    db.commit()
    db.refresh(activity)
    return ActivityResponse.model_validate(activity)


@router.delete("/{activity_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_activity(
    activity_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    activity = _load_editable(db, activity_id, current_user)
    activity_service.delete_activity(db, activity)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
