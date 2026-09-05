"""Endpoint registrasi, login, dan pemilihan profil.

Login terjadi di level akun; data kesehatan melekat pada profil. Karena itu
token dari `/login` belum menunjuk profil mana pun — frontend memanggil
`/select-profile` setelah pengguna memilih siapa yang sedang memakai
aplikasi (ERD §0).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import (
    authenticate_account,
    create_access_token,
    get_current_account,
    hash_password,
    verify_pin,
)
from app.db.models import Account, FamilyMember
from app.db.session import get_db
from app.schemas import (
    EmailCheckResponse,
    ProfileSelectRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
)


router = APIRouter()


@router.post(
    "/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED
)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Buat akun keluarga beserta profil admin-nya.

    Keduanya dibuat sekaligus: akun tanpa profil tidak bisa berbuat apa-apa,
    dan orang yang mendaftar otomatis jadi kepala keluarga (FR-6.4).
    """
    existing = db.execute(
        select(Account).where(func.lower(Account.email) == payload.email)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email sudah terdaftar",
        )

    account = Account(
        email=payload.email,
        password_hash=hash_password(payload.password),
    )
    db.add(account)
    db.flush()

    admin = FamilyMember(
        account_id=account.id,
        full_name=payload.full_name,
        date_of_birth=payload.date_of_birth,
        gender=payload.gender,
        role="admin",
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)

    # Pendaftar langsung mendapat token berprofil: dia baru saja membuktikan
    # dirinya, jadi tidak ada gunanya memaksa satu langkah pilih profil lagi.
    return TokenResponse(access_token=create_access_token(account.id, admin.id))


@router.post("/login", response_model=TokenResponse)
def login(
    form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)
) -> TokenResponse:
    """Login pakai form OAuth2 standar, jadi tombol Authorize di /docs berfungsi.

    Field `username` diisi email. Token yang dikembalikan belum menunjuk
    profil; panggil `/select-profile` berikutnya.
    """
    account = authenticate_account(db, form.username.lower(), form.password)
    if account is None:
        # Pesan sengaja disamakan untuk email tidak dikenal maupun password
        # salah — membedakannya akan membocorkan siapa saja yang punya akun.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email atau password salah",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return TokenResponse(access_token=create_access_token(account.id))


@router.get("/check-email", response_model=EmailCheckResponse)
def check_email(email: str, db: Session = Depends(get_db)) -> EmailCheckResponse:
    """Cek apakah sebuah email sudah terdaftar.

    ponytail: publik dan tanpa rate-limit atas permintaan eksplisit —
    endpoint ini adalah user-enumeration yang disengaja, bukan bug. Tambah
    rate-limit per-IP kalau mulai terlihat discraping.
    """
    existing = db.execute(
        select(Account).where(func.lower(Account.email) == email.lower())
    ).scalar_one_or_none()
    return EmailCheckResponse(exists=existing is not None)


@router.post("/reset-password", response_model=TokenResponse)
def reset_password(
    payload: ResetPasswordRequest, db: Session = Depends(get_db)
) -> TokenResponse:
    """Ganti password langsung lewat email, tanpa OTP atau token verifikasi.

    ponytail: siapa pun yang tahu email seseorang bisa mereset passwordnya.
    Tambahkan verifikasi (OTP/email link) begitu ada infrastruktur
    pengiriman (SMTP/Telegram) — ini bukan alur yang aman untuk produksi.
    """
    account = db.execute(
        select(Account).where(func.lower(Account.email) == payload.email)
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Email tidak ditemukan"
        )

    account.password_hash = hash_password(payload.new_password)
    db.commit()

    return TokenResponse(access_token=create_access_token(account.id))


@router.post("/select-profile", response_model=TokenResponse)
def select_profile(
    payload: ProfileSelectRequest,
    account: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
) -> TokenResponse:
    """Tukar token akun dengan token yang menunjuk satu profil.

    Profil ber-PIN butuh PIN yang benar. Tanpa langkah ini, siapa pun yang
    memegang ponsel yang sudah login bisa membuka profil terkunci.
    """
    profile = db.get(FamilyMember, payload.profile_id)

    # Profil milik akun lain dijawab sama dengan profil tidak ada, supaya
    # tidak bisa dipakai menebak profil siapa saja yang terdaftar.
    if profile is None or profile.account_id != account.id or not profile.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Profil tidak ditemukan"
        )

    if profile.pin_hash is not None:
        if not payload.pin or not verify_pin(payload.pin, profile.pin_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="PIN salah"
            )

    return TokenResponse(access_token=create_access_token(account.id, profile.id))
