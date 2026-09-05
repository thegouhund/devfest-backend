"""Hashing password, JWT, dan otorisasi.

Data kesehatan bersifat sensitif (PRD §6.1), jadi setiap kegagalan di sini
harus menutup akses, bukan membiarkannya lewat.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import FamilyMember, User
from app.db.session import get_db


ALGORITHM = "HS256"

# bcrypt hanya membaca 72 byte pertama; sisanya diabaikan diam-diam.
BCRYPT_MAX_BYTES = 72

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

credentials_error = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Kredensial tidak valid",
    headers={"WWW-Authenticate": "Bearer"},
)


def _jwt_secret() -> str:
    """Secret kosong berarti token bisa dipalsukan siapa saja, jadi lebih
    baik gagal keras saat dipakai daripada menandatangani dengan string kosong."""
    secret = get_settings().jwt_secret
    if not secret:
        raise RuntimeError(
            "JWT_SECRET belum di-set. Generate dengan: "
            'python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    return secret


# --- Password --------------------------------------------------------------


def hash_password(password: str) -> str:
    truncated = password.encode()[:BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(truncated, bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    truncated = password.encode()[:BCRYPT_MAX_BYTES]
    try:
        return bcrypt.checkpw(truncated, password_hash.encode())
    except ValueError:
        # Hash rusak/berformat lain: perlakukan sebagai gagal, jangan sampai
        # jadi 500 di endpoint login.
        return False


# --- Token -----------------------------------------------------------------


def create_access_token(
    user_id: uuid.UUID, expires_delta: timedelta | None = None
) -> str:
    settings = get_settings()
    expire_in = expires_delta or timedelta(minutes=settings.jwt_expire_minutes)
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(UTC) + expire_in,
        "iat": datetime.now(UTC),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            _jwt_secret(),
            algorithms=[ALGORITHM],
            # Tanpa require, token tanpa `exp` diterima dan berlaku selamanya.
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError:
        # Kedaluwarsa, tanda tangan salah, atau formatnya rusak — semuanya
        # dijawab sama supaya tidak membocorkan alasan penolakan.
        raise credentials_error from None


# --- Autentikasi -----------------------------------------------------------

# Hash pembanding untuk email yang tidak terdaftar, supaya biaya verifikasi
# tetap sama. Dihitung sekali saat import, bukan per-request.
_DUMMY_HASH = hash_password("dummy-password-untuk-menyamakan-waktu-verifikasi")


def authenticate_user(db: Session, email: str, password: str) -> User | None:
    """Kembalikan user bila kredensial cocok, selain itu None.

    Dependent (`password_hash` NULL) tidak pernah bisa login — profilnya
    dikelola admin, bukan diakses sendiri (ERD §2.1).

    Verifikasi hash tetap dijalankan walau user tidak ada, supaya waktu
    respons tidak membocorkan email mana yang terdaftar. Tanpa ini,
    selisihnya ~1000x dan siapa pun bisa memetakan anggota keluarga.
    """
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()

    if user is None or user.password_hash is None or not user.is_active:
        verify_password(password, _DUMMY_HASH)
        return None

    if not verify_password(password, user.password_hash):
        return None
    return user


def resolve_current_user(db: Session, token: str) -> User:
    """Terjemahkan token jadi user aktif. Dipisah dari dependency FastAPI
    supaya bisa dipakai ulang di Chainlit (Task 21) yang bukan HTTP request."""
    payload = decode_access_token(token)
    subject = payload.get("sub")
    if not subject:
        raise credentials_error

    try:
        user_id = uuid.UUID(subject)
    except ValueError:
        raise credentials_error from None

    user = db.get(User, user_id)
    # User terhapus atau dinonaktifkan setelah token terbit harus langsung
    # kehilangan akses, tanpa menunggu token kedaluwarsa.
    if user is None or not user.is_active:
        raise credentials_error
    return user


def get_current_user(
    db: Session = Depends(get_db), token: str = Depends(oauth2_scheme)
) -> User:
    return resolve_current_user(db, token)


# --- Otorisasi -------------------------------------------------------------


def require_family_admin(db: Session, user: User, family_id: uuid.UUID) -> FamilyMember:
    """Pastikan `user` admin aktif di family tersebut, kalau tidak 403."""
    membership = db.execute(
        select(FamilyMember).where(
            FamilyMember.family_id == family_id,
            FamilyMember.user_id == user.id,
            FamilyMember.role == "admin",
            FamilyMember.status == "active",
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Butuh hak admin di family ini",
        )
    return membership
