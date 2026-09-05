"""Hashing password, JWT, dan otorisasi.

Model akun & profil (ERD §0): login terjadi di level `accounts`, sementara
data kesehatan melekat pada `family_members` (profil). Token membawa
`sub` (akun) dan opsional `profile` (profil aktif) — profil ber-PIN baru
masuk token setelah PIN diverifikasi.

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
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Account, FamilyMember
from app.db.session import get_db


ALGORITHM = "HS256"

# bcrypt hanya membaca 72 byte pertama; sisanya diabaikan diam-diam.
BCRYPT_MAX_BYTES = 72

# Panjang minimum kunci HMAC-SHA256 menurut RFC 7518 §3.2.
MIN_JWT_SECRET_BYTES = 32

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

credentials_error = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Kredensial tidak valid",
    headers={"WWW-Authenticate": "Bearer"},
)


def _jwt_secret() -> str:
    """Secret kosong atau terlalu pendek berarti token bisa dipalsukan,
    jadi lebih baik gagal keras saat dipakai.

    Panjang minimum mengikuti RFC 7518 §3.2 untuk HMAC-SHA256. PyJWT hanya
    memberi peringatan untuk kunci pendek, dan peringatan tenggelam di log
    produksi — padahal kunci lemah bisa dipecahkan brute force, dan siapa
    pun yang berhasil bisa memalsukan token untuk membaca data kesehatan
    seluruh keluarga.
    """
    secret = get_settings().jwt_secret
    perintah = 'python -c "import secrets; print(secrets.token_urlsafe(32))"'

    if not secret:
        raise RuntimeError(f"JWT_SECRET belum di-set. Generate dengan: {perintah}")

    if len(secret.encode()) < MIN_JWT_SECRET_BYTES:
        raise RuntimeError(
            f"JWT_SECRET terlalu pendek ({len(secret.encode())} byte, minimal "
            f"{MIN_JWT_SECRET_BYTES}). Generate dengan: {perintah}"
        )
    return secret


# --- Password & PIN --------------------------------------------------------


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


# PIN profil memakai hashing yang sama dengan password. PIN jauh lebih
# pendek dan mudah ditebak, tapi ia lapisan tambahan di dalam akun yang
# sudah login — bukan pengganti password.
hash_pin = hash_password
verify_pin = verify_password


# --- Token -----------------------------------------------------------------


def create_access_token(
    account_id: uuid.UUID,
    profile_id: uuid.UUID | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    """Terbitkan token untuk satu akun, opsional dengan profil aktif.

    `profile_id` disematkan hanya setelah profil dipilih (dan PIN-nya
    diverifikasi kalau ada), sehingga token tidak bisa dipakai membaca data
    profil yang belum dibuka.
    """
    settings = get_settings()
    expire_in = expires_delta or timedelta(minutes=settings.jwt_expire_minutes)
    payload: dict = {
        "sub": str(account_id),
        "exp": datetime.now(UTC) + expire_in,
        "iat": datetime.now(UTC),
    }
    if profile_id is not None:
        payload["profile"] = str(profile_id)

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


def authenticate_account(db: Session, email: str, password: str) -> Account | None:
    """Kembalikan akun bila kredensial cocok, selain itu None.

    Verifikasi hash tetap dijalankan walau akun tidak ada, supaya waktu
    respons tidak membocorkan email mana yang terdaftar. Tanpa ini,
    selisihnya ~1000x dan siapa pun bisa memetakan pengguna.
    """
    account = db.execute(
        select(Account).where(func.lower(Account.email) == email.lower())
    ).scalar_one_or_none()

    if account is None or not account.is_active:
        verify_password(password, _DUMMY_HASH)
        return None

    if not verify_password(password, account.password_hash):
        return None
    return account


def resolve_current_account(db: Session, token: str) -> Account:
    """Terjemahkan token jadi akun aktif.

    Dipisah dari dependency FastAPI supaya bisa dipakai ulang di luar
    konteks HTTP request.
    """
    payload = decode_access_token(token)
    account = _account_from_payload(db, payload)
    return account


def _account_from_payload(db: Session, payload: dict) -> Account:
    subject = payload.get("sub")
    if not subject:
        raise credentials_error

    try:
        account_id = uuid.UUID(subject)
    except ValueError:
        raise credentials_error from None

    account = db.get(Account, account_id)
    # Akun terhapus atau dinonaktifkan setelah token terbit harus langsung
    # kehilangan akses, tanpa menunggu token kedaluwarsa.
    if account is None or not account.is_active:
        raise credentials_error
    return account


def get_current_account(
    db: Session = Depends(get_db), token: str = Depends(oauth2_scheme)
) -> Account:
    return resolve_current_account(db, token)


def get_current_profile(
    db: Session = Depends(get_db), token: str = Depends(oauth2_scheme)
) -> FamilyMember:
    """Profil aktif dari token.

    Menolak 403 kalau token belum memuat profil — endpoint data kesehatan
    butuh subjek yang jelas, dan memilih profil secara diam-diam berisiko
    menulis data ke orang yang salah.
    """
    payload = decode_access_token(token)
    account = _account_from_payload(db, payload)

    profile_id = payload.get("profile")
    if not profile_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Pilih profil dulu sebelum mengakses data",
        )

    try:
        profile = db.get(FamilyMember, uuid.UUID(profile_id))
    except ValueError:
        raise credentials_error from None

    # Profil harus milik akun di token yang sama: tanpa cek ini, token bisa
    # disusun ulang untuk menunjuk profil milik akun lain.
    if profile is None or profile.account_id != account.id or not profile.is_active:
        raise credentials_error

    return profile


# --- Otorisasi -------------------------------------------------------------


def require_admin_profile(profile: FamilyMember = Depends(get_current_profile)):
    """Pastikan profil aktif punya role admin, kalau tidak 403.

    Admin adalah profil yang dibuat saat registrasi; hanya dia yang boleh
    menambah, mengubah, dan menghapus profil lain (FR-6.4).
    """
    if profile.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Hanya profil admin yang bisa melakukan ini",
        )
    return profile
