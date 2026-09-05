"""Endpoint akun (bukan profil).

Data pribadi anggota keluarga ada di `/profiles`; di sini hanya hal yang
melekat pada login: email dan nomor telepon.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.security import get_current_account
from app.db.models import Account
from app.db.session import get_db
from app.schemas import AccountResponse, AccountUpdateRequest


router = APIRouter()


@router.get("/me", response_model=AccountResponse)
def read_own_account(account: Account = Depends(get_current_account)) -> Account:
    return account


@router.patch("/me", response_model=AccountResponse)
def update_own_account(
    payload: AccountUpdateRequest,
    account: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
) -> Account:
    # exclude_unset supaya field yang tidak dikirim tetap seperti semula.
    # Field di luar AccountUpdateRequest (email, password_hash, is_active)
    # tidak akan pernah sampai ke sini.
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(account, field, value)

    db.commit()
    db.refresh(account)
    return account
