"""Task 4: password hashing, JWT, dependency current-user.

Acceptance criteria under test:
- hash_password/verify_password round-trip; hash bergaram dan beda tiap panggilan
- Token membawa sub & exp; token kedaluwarsa atau dipalsukan -> 401
- FamilyMember dependent tidak bisa dapat token
- Helper require_family_admin untuk route khusus admin
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.core.security import (
    authenticate_account,
    create_access_token,
    decode_access_token,
    get_current_profile,
    hash_password,
    require_admin_profile,
    resolve_current_account,
    verify_password,
)
from app.db.models import Account, Base, FamilyMember
from tests.conftest import make_account, make_profile_row


@pytest.fixture
def db():
    engine = create_engine("sqlite://", future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch):
    from app.core.config import get_settings

    monkeypatch.setenv("JWT_SECRET", "test-secret-key-for-signing-cukup-panjang")
    get_settings.cache_clear()
    yield "test-secret-key-for-signing-cukup-panjang"
    get_settings.cache_clear()


# --- Hashing ---------------------------------------------------------------


class TestPasswordHashing:
    def test_round_trip(self) -> None:
        hashed = hash_password("correct horse battery staple")
        assert verify_password("correct horse battery staple", hashed)

    def test_wrong_password_rejected(self) -> None:
        hashed = hash_password("benar")
        assert not verify_password("salah", hashed)

    def test_hash_is_not_plaintext(self) -> None:
        assert "rahasia" not in hash_password("rahasia")

    def test_salted_so_same_password_differs(self) -> None:
        """Tanpa garam, dua user berpassword sama punya hash identik —
        satu hash bocor langsung membocorkan keduanya."""
        assert hash_password("sama") != hash_password("sama")

    def test_verify_handles_malformed_hash(self) -> None:
        """Hash rusak harus jadi False, bukan melempar exception yang
        bisa jatuh jadi 500 di endpoint login."""
        assert not verify_password("apa pun", "bukan-hash-valid")

    def test_long_password_accepted(self) -> None:
        """bcrypt memotong di 72 byte; implementasi tidak boleh error."""
        long_password = "a" * 200
        assert verify_password(long_password, hash_password(long_password))


# --- JWT -------------------------------------------------------------------


class TestAccessToken:
    def test_token_carries_subject(self, secret) -> None:
        user_id = uuid.uuid4()
        payload = decode_access_token(create_access_token(user_id))
        assert payload["sub"] == str(user_id)

    def test_token_carries_expiry(self, secret) -> None:
        payload = decode_access_token(create_access_token(uuid.uuid4()))
        assert "exp" in payload

    def test_expired_token_rejected(self, secret) -> None:
        expired = create_access_token(uuid.uuid4(), expires_delta=timedelta(seconds=-1))
        with pytest.raises(HTTPException) as exc:
            decode_access_token(expired)
        assert exc.value.status_code == 401

    def test_tampered_token_rejected(self, secret) -> None:
        token = create_access_token(uuid.uuid4())
        head, payload, signature = token.split(".")
        forged = f"{head}.{payload}.{signature[:-4]}xxxx"
        with pytest.raises(HTTPException) as exc:
            decode_access_token(forged)
        assert exc.value.status_code == 401

    def test_token_signed_with_other_secret_rejected(
        self, secret, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Token dari secret lain tidak boleh diterima."""
        from app.core.config import get_settings

        token = create_access_token(uuid.uuid4())
        monkeypatch.setenv("JWT_SECRET", "secret-yang-berbeda-tapi-cukup-panjang-32b")
        get_settings.cache_clear()
        with pytest.raises(HTTPException) as exc:
            decode_access_token(token)
        assert exc.value.status_code == 401

    def test_token_without_expiry_rejected(self, secret) -> None:
        """Token tanpa exp akan berlaku selamanya kalau diterima."""
        import jwt as pyjwt

        from app.core.security import ALGORITHM

        forever = pyjwt.encode(
            {"sub": str(uuid.uuid4())}, secret, algorithm=ALGORITHM
        )
        with pytest.raises(HTTPException) as exc:
            decode_access_token(forever)
        assert exc.value.status_code == 401

    def test_unsigned_token_rejected(self, secret) -> None:
        """Serangan alg=none: token tanpa tanda tangan harus ditolak."""
        import jwt as pyjwt

        unsigned = pyjwt.encode({"sub": str(uuid.uuid4())}, "", algorithm="none")
        with pytest.raises(HTTPException) as exc:
            decode_access_token(unsigned)
        assert exc.value.status_code == 401

    def test_garbage_token_rejected(self, secret) -> None:
        with pytest.raises(HTTPException) as exc:
            decode_access_token("bukan.token.jwt")
        assert exc.value.status_code == 401

    def test_short_secret_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Kunci pendek bisa dipecahkan brute force, dan siapa pun yang
        berhasil bisa memalsukan token untuk membaca data kesehatan
        seluruh keluarga. PyJWT hanya memberi peringatan, yang tenggelam
        di log produksi — jadi ditolak di sini."""
        from app.core.config import get_settings

        monkeypatch.setenv("JWT_SECRET", "pendek")
        get_settings.cache_clear()
        with pytest.raises(Exception) as exc:
            create_access_token(uuid.uuid4())
        assert "pendek" in str(exc.value).lower() or "byte" in str(exc.value).lower()
        get_settings.cache_clear()

    def test_recommended_secret_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Panjang yang disarankan .env.example harus lolos."""
        import secrets as py_secrets

        from app.core.config import get_settings

        monkeypatch.setenv("JWT_SECRET", py_secrets.token_urlsafe(32))
        get_settings.cache_clear()
        assert create_access_token(uuid.uuid4())
        get_settings.cache_clear()

    def test_missing_secret_fails_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """JWT_SECRET kosong harus error jelas, bukan diam-diam
        menandatangani dengan string kosong."""
        from app.core.config import get_settings

        monkeypatch.setenv("JWT_SECRET", "")
        get_settings.cache_clear()
        with pytest.raises(Exception) as exc:
            create_access_token(uuid.uuid4())
        assert "JWT_SECRET" in str(exc.value)
        get_settings.cache_clear()


# --- Autentikasi user ------------------------------------------------------


class TestAuthenticateUser:
    def test_valid_credentials(self, db) -> None:
        db.add(
            Account(
                email="budi@example.com",
                password_hash=hash_password("rahasia123"),
            )
        )
        db.commit()
        assert authenticate_account(db, "budi@example.com", "rahasia123") is not None

    def test_wrong_password(self, db) -> None:
        db.add(
            Account(
                email="budi@example.com",
                password_hash=hash_password("rahasia123"),
            )
        )
        db.commit()
        assert authenticate_account(db, "budi@example.com", "salah") is None

    def test_unknown_email(self, db) -> None:
        assert authenticate_account(db, "tidakada@example.com", "apa pun") is None

    def test_unknown_email_takes_similar_time(self, db) -> None:
        """Waktu respons tidak boleh membocorkan email mana yang terdaftar.

        Tanpa dummy-verify, email tak dikenal balik ~1000x lebih cepat karena
        melewati bcrypt — cukup untuk memetakan siapa saja yang punya akun.
        """
        import statistics
        import time

        db.add(
            Account(
                email="ada@example.com",
                password_hash=hash_password("rahasia123"),
            )
        )
        db.commit()

        def median_ms(email: str) -> float:
            samples = []
            for _ in range(5):
                start = time.perf_counter()
                authenticate_account(db, email, "tebakan-salah")
                samples.append(time.perf_counter() - start)
            return statistics.median(samples) * 1000

        known = median_ms("ada@example.com")
        unknown = median_ms("tidakada@example.com")
        assert 0.2 < unknown / known < 5, (
            f"selisih waktu terlalu mencolok: terdaftar {known:.1f}ms vs "
            f"tidak terdaftar {unknown:.1f}ms"
        )

    def test_inactive_account_cannot_authenticate(self, db) -> None:
        db.add(
            Account(
                email="off@example.com",
                password_hash=hash_password("rahasia123"),
                is_active=False,
            )
        )
        db.commit()
        assert authenticate_account(db, "off@example.com", "rahasia123") is None


# --- Dependency current account ---------------------------------------------


class TestResolveCurrentAccount:
    def test_valid_token_returns_account(self, db, secret) -> None:
        account = Account(email="budi@example.com", password_hash=hash_password("x"))
        db.add(account)
        db.commit()
        resolved = resolve_current_account(db, create_access_token(account.id))
        assert resolved.id == account.id

    def test_token_for_deleted_account_rejected(self, db, secret) -> None:
        """Token valid tapi akun sudah tidak ada -> 401, bukan 500."""
        with pytest.raises(HTTPException) as exc:
            resolve_current_account(db, create_access_token(uuid.uuid4()))
        assert exc.value.status_code == 401

    def test_token_for_deactivated_account_rejected(self, db, secret) -> None:
        """Akun dinonaktifkan setelah token terbit harus langsung ditolak."""
        account = Account(email="budi@example.com", password_hash=hash_password("x"))
        db.add(account)
        db.commit()
        token = create_access_token(account.id)
        account.is_active = False
        db.commit()
        with pytest.raises(HTTPException) as exc:
            resolve_current_account(db, token)
        assert exc.value.status_code == 401


# --- Otorisasi admin profil ------------------------------------------------


class TestRequireAdminProfile:
    """Hanya profil admin yang boleh mengelola profil lain (FR-6.4)."""

    @pytest.fixture
    def profiles(self, db):
        account = Account(email="a@x.com", password_hash=hash_password("x"))
        db.add(account)
        db.flush()
        admin = FamilyMember(
            account_id=account.id, full_name="Admin", role="admin"
        )
        member = FamilyMember(
            account_id=account.id, full_name="Anggota", role="member"
        )
        db.add_all([admin, member])
        db.commit()
        return admin, member

    def test_admin_allowed(self, profiles) -> None:
        admin, _ = profiles
        assert require_admin_profile(admin) is admin

    def test_member_forbidden(self, profiles) -> None:
        _, member = profiles
        with pytest.raises(HTTPException) as exc:
            require_admin_profile(member)
        assert exc.value.status_code == 403


class TestGetCurrentProfile:
    """Token tanpa profil tidak boleh diam-diam memilih profil pertama:
    salah subjek berarti data kesehatan tertulis ke orang yang keliru."""

    @pytest.fixture
    def account_with_profile(self, db, secret):
        account = Account(email="b@x.com", password_hash=hash_password("x"))
        db.add(account)
        db.flush()
        profile = FamilyMember(
            account_id=account.id, full_name="Budi", role="admin"
        )
        db.add(profile)
        db.commit()
        return account, profile

    def test_token_without_profile_rejected(self, db, account_with_profile) -> None:
        account, _ = account_with_profile
        token = create_access_token(account.id)
        with pytest.raises(HTTPException) as exc:
            get_current_profile(db, token)
        assert exc.value.status_code == 403

    def test_token_with_profile_resolves(self, db, account_with_profile) -> None:
        account, profile = account_with_profile
        token = create_access_token(account.id, profile.id)
        assert get_current_profile(db, token).id == profile.id

    def test_profile_from_other_account_rejected(
        self, db, account_with_profile
    ) -> None:
        """Token disusun ulang menunjuk profil akun lain harus ditolak,
        bukan dilayani."""
        account, _ = account_with_profile

        lain = Account(email="c@x.com", password_hash=hash_password("x"))
        db.add(lain)
        db.flush()
        profil_lain = FamilyMember(account_id=lain.id, full_name="Orang Lain")
        db.add(profil_lain)
        db.commit()

        token = create_access_token(account.id, profil_lain.id)
        with pytest.raises(HTTPException) as exc:
            get_current_profile(db, token)
        assert exc.value.status_code == 401

    def test_deactivated_profile_rejected(self, db, account_with_profile) -> None:
        account, profile = account_with_profile
        token = create_access_token(account.id, profile.id)
        profile.is_active = False
        db.commit()
        with pytest.raises(HTTPException) as exc:
            get_current_profile(db, token)
        assert exc.value.status_code == 401
