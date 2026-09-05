"""Task 8: penyimpanan video mentah di filesystem VPS.

Video wajah adalah data biometrik (PRD §6.1), jadi jalur penyimpanannya
diperlakukan sebagai batas kepercayaan: tidak ada string dari user yang
boleh sampai ke filesystem.

Acceptance criteria under test:
- Path dibangun dari UUID tervalidasi saja
- Tolak format selain mp4/mov, durasi terlalu pendek, resolusi terlalu kecil
- Ukuran maksimal ditegakkan
- Menulis storage_path, file_size_bytes, storage_provider='vps_local'
- Gagal tulis tidak meninggalkan baris DB yatim
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import MeasurementSession, FamilyMember, VideoStorageRef
from app.services import video_storage
from app.services.video_storage import (
    MAX_UPLOAD_BYTES,
    VideoValidationError,
    build_storage_path,
    delete_video,
    save_video,
    validate_upload,
)
from tests.conftest import make_account, make_profile_row


@pytest.fixture
def storage_root(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from app.core.config import get_settings

    root = tmp_path / "videos"
    monkeypatch.setenv("VIDEO_STORAGE_PATH", str(root))
    monkeypatch.setenv("JWT_SECRET", "test-secret-yang-cukup-panjang-untuk-hmac")
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


@pytest.fixture
def session(db_session):
    """Sesi pengukuran siap pakai."""
    from datetime import UTC, datetime

    user = make_profile_row(db_session, full_name="Budi")
    db_session.add(user)
    db_session.flush()
    measurement = MeasurementSession(
        family_member_id=user.id,
        initiated_by_family_member_id=user.id,
        capture_method="upload",
        started_at=datetime.now(UTC),
    )
    db_session.add(measurement)
    db_session.commit()
    return measurement


def fake_mp4(size_bytes: int = 2048) -> bytes:
    """Byte yang diawali signature ftyp mp4, cukup untuk uji jalur penyimpanan."""
    header = b"\x00\x00\x00\x20ftypmp42\x00\x00\x00\x00mp42isom"
    return header + b"\x00" * max(0, size_bytes - len(header))


# --- Pembentukan path ------------------------------------------------------


class TestBuildStoragePath:
    def test_path_uses_user_and_session_id(self, storage_root) -> None:
        user_id, session_id = uuid.uuid4(), uuid.uuid4()
        path = build_storage_path(user_id, session_id)
        assert str(user_id) in str(path)
        assert str(session_id) in str(path)
        assert path.suffix == ".mp4"

    def test_path_stays_inside_storage_root(self, storage_root) -> None:
        path = build_storage_path(uuid.uuid4(), uuid.uuid4())
        assert path.resolve().is_relative_to(storage_root.resolve())

    @pytest.mark.parametrize(
        "evil",
        [
            "../../../etc/passwd",
            "..%2f..%2fetc",
            "/etc/shadow",
            "a/../../b",
            "....//....//etc",
        ],
    )
    def test_rejects_non_uuid_input(self, storage_root, evil: str) -> None:
        """Hanya UUID yang boleh membentuk path — string bebas ditolak
        sebelum menyentuh filesystem."""
        with pytest.raises((VideoValidationError, ValueError, AttributeError, TypeError)):
            build_storage_path(evil, uuid.uuid4())

    def test_different_sessions_get_different_paths(self, storage_root) -> None:
        user_id = uuid.uuid4()
        first = build_storage_path(user_id, uuid.uuid4())
        second = build_storage_path(user_id, uuid.uuid4())
        assert first != second


# --- Validasi upload -------------------------------------------------------


class TestValidateUpload:
    def test_accepts_mp4(self, storage_root) -> None:
        validate_upload("rekaman.mp4", fake_mp4())

    def test_accepts_mov(self, storage_root) -> None:
        validate_upload("rekaman.mov", fake_mp4())

    def test_accepts_uppercase_extension(self, storage_root) -> None:
        validate_upload("REKAMAN.MP4", fake_mp4())

    @pytest.mark.parametrize(
        "filename",
        ["skrip.sh", "gambar.jpg", "dokumen.pdf", "tanpa-ekstensi", "video.mp4.exe"],
    )
    def test_rejects_other_formats(self, storage_root, filename: str) -> None:
        with pytest.raises(VideoValidationError):
            validate_upload(filename, fake_mp4())

    def test_rejects_oversize(self, storage_root) -> None:
        oversize = b"\x00" * (MAX_UPLOAD_BYTES + 1)
        with pytest.raises(VideoValidationError) as exc:
            validate_upload("besar.mp4", oversize)
        assert "besar" in str(exc.value).lower() or "size" in str(exc.value).lower()

    def test_rejects_empty_file(self, storage_root) -> None:
        with pytest.raises(VideoValidationError):
            validate_upload("kosong.mp4", b"")

    def test_rejects_content_that_is_not_video(self, storage_root) -> None:
        """Nama berakhiran .mp4 tidak cukup — isinya harus benar video."""
        with pytest.raises(VideoValidationError):
            validate_upload("palsu.mp4", b"#!/bin/sh\nrm -rf /\n" + b"\x00" * 2000)


# --- Menyimpan video -------------------------------------------------------


class TestSaveVideo:
    def test_writes_file_to_disk(self, storage_root, db_session, session) -> None:
        ref = save_video(db_session, session, "rekaman.mp4", fake_mp4())
        db_session.commit()
        assert Path(ref.storage_path).exists()

    def test_records_metadata(self, storage_root, db_session, session) -> None:
        content = fake_mp4(4096)
        ref = save_video(db_session, session, "rekaman.mp4", content)
        db_session.commit()
        assert ref.storage_provider == "vps_local"
        assert ref.file_size_bytes == len(content)
        assert ref.measurement_session_id == session.id

    def test_file_content_matches(self, storage_root, db_session, session) -> None:
        content = fake_mp4(3000)
        ref = save_video(db_session, session, "rekaman.mp4", content)
        db_session.commit()
        assert Path(ref.storage_path).read_bytes() == content

    def test_creates_directory_if_missing(self, storage_root, db_session, session) -> None:
        assert not storage_root.exists()
        ref = save_video(db_session, session, "rekaman.mp4", fake_mp4())
        db_session.commit()
        assert Path(ref.storage_path).exists()

    def test_invalid_upload_writes_nothing(self, storage_root, db_session, session) -> None:
        """Upload ditolak tidak boleh meninggalkan file maupun baris DB."""
        with pytest.raises(VideoValidationError):
            save_video(db_session, session, "skrip.sh", fake_mp4())
        db_session.rollback()
        assert db_session.execute(select(VideoStorageRef)).first() is None

    def test_write_failure_leaves_no_orphan_row(
        self, storage_root, db_session, session, monkeypatch
    ) -> None:
        """Kalau penulisan file gagal, baris DB tidak boleh tertinggal —
        metadata yang menunjuk file tidak ada lebih buruk daripada gagal total."""

        def gagal_menulis(*args, **kwargs):
            raise OSError("disk penuh")

        monkeypatch.setattr(video_storage, "_write_file", gagal_menulis)
        with pytest.raises(OSError):
            save_video(db_session, session, "rekaman.mp4", fake_mp4())
        db_session.rollback()
        assert db_session.execute(select(VideoStorageRef)).first() is None

    def test_file_permission_is_restrictive(
        self, storage_root, db_session, session
    ) -> None:
        """Video wajah tidak boleh terbaca user lain di VPS (PRD §6.1)."""
        ref = save_video(db_session, session, "rekaman.mp4", fake_mp4())
        db_session.commit()
        mode = Path(ref.storage_path).stat().st_mode & 0o777
        assert mode & 0o077 == 0, f"file terbaca pihak lain: {oct(mode)}"

    def test_every_directory_level_is_private(
        self, storage_root, db_session, session
    ) -> None:
        """Termasuk direktori root-nya. `mkdir(parents=True, mode=...)` hanya
        menerapkan mode ke direktori terdalam, sehingga daftar UUID pengguna
        bisa terbaca user lain kalau induknya tidak ikut dirapikan."""
        ref = save_video(db_session, session, "rekaman.mp4", fake_mp4())
        db_session.commit()

        directory = Path(ref.storage_path).parent
        root = storage_root.resolve()
        while directory.is_relative_to(root):
            mode = directory.stat().st_mode & 0o777
            assert mode & 0o077 == 0, f"{directory} terbuka: {oct(mode)}"
            if directory == root:
                break
            directory = directory.parent


# --- Menghapus video -------------------------------------------------------


class TestDeleteVideo:
    def test_removes_file_and_marks_row(self, storage_root, db_session, session) -> None:
        ref = save_video(db_session, session, "rekaman.mp4", fake_mp4())
        db_session.commit()
        path = Path(ref.storage_path)

        delete_video(db_session, ref)
        db_session.commit()

        assert not path.exists()
        assert ref.deleted_at is not None

    def test_missing_file_does_not_raise(self, storage_root, db_session, session) -> None:
        """File sudah hilang duluan tidak boleh membuat penghapusan gagal."""
        ref = save_video(db_session, session, "rekaman.mp4", fake_mp4())
        db_session.commit()
        Path(ref.storage_path).unlink()

        delete_video(db_session, ref)
        db_session.commit()
        assert ref.deleted_at is not None
