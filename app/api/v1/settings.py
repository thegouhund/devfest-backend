"""Endpoint pengaturan privasi per jenis data (PRD FR-6.2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.db.models import DataVisibilitySetting, User
from app.db.session import get_db
from app.schemas import (
    VisibilityListResponse,
    VisibilitySettingResponse,
    VisibilityUpdateRequest,
)
from app.services.visibility import DATA_TYPES, resolve_visibility


router = APIRouter()


@router.get("/visibility", response_model=VisibilityListResponse)
def read_visibility_settings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VisibilityListResponse:
    """Setelan efektif untuk tiap jenis data.

    Selalu mengembalikan baris lengkap — jenis yang belum pernah diatur
    dikembalikan dengan nilai default, supaya frontend tidak perlu tahu
    apa defaultnya.
    """
    return VisibilityListResponse(
        settings=[
            VisibilitySettingResponse(
                data_type=data_type,
                visibility=resolve_visibility(db, current_user.id, data_type),
            )
            for data_type in DATA_TYPES
        ]
    )


@router.put("/visibility", response_model=VisibilityListResponse)
def update_visibility_setting(
    payload: VisibilityUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VisibilityListResponse:
    """Ubah setelan privasi milik sendiri.

    `user_id` diambil dari token, tidak pernah dari request body — kalau
    tidak, siapa pun bisa mengubah setelan privasi orang lain.
    """
    existing = db.execute(
        select(DataVisibilitySetting).where(
            DataVisibilitySetting.user_id == current_user.id,
            DataVisibilitySetting.data_type == payload.data_type,
        )
    ).scalar_one_or_none()

    if existing is None:
        db.add(
            DataVisibilitySetting(
                user_id=current_user.id,
                data_type=payload.data_type,
                visibility=payload.visibility,
            )
        )
    else:
        # Upsert: UNIQUE(user_id, data_type) melarang baris kedua.
        existing.visibility = payload.visibility

    db.commit()
    return read_visibility_settings(current_user=current_user, db=db)
