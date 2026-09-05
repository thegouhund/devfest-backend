"""Endpoint profil user."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.schemas import UserResponse, UserUpdateRequest


router = APIRouter()


@router.get("/me", response_model=UserResponse)
def read_own_profile(current_user: User = Depends(get_current_user)) -> User:
    return current_user


@router.patch("/me", response_model=UserResponse)
def update_own_profile(
    payload: UserUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    # exclude_unset supaya field yang tidak dikirim tetap seperti semula,
    # bukan tertimpa None. Field di luar UserUpdateRequest (email, is_active,
    # is_dependent) tidak akan pernah sampai ke sini.
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(current_user, field, value)

    db.commit()
    db.refresh(current_user)
    return current_user
