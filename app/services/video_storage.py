"""Penyimpanan video mentah di filesystem VPS (PRD A1, ERD §2.5).

Video wajah adalah data biometrik. Dua hal dijaga ketat di sini:

1. **Path tidak pernah dibentuk dari string user.** Nama file yang diunggah
   hanya dipakai untuk mengecek ekstensi, lalu dibuang. Path aslinya disusun
   dari UUID yang sudah tervalidasi, sehingga `../../etc/passwd` tidak punya
   jalan masuk.
2. **Isi file diperiksa, bukan hanya namanya.** Ekstensi `.mp4` mudah
   dipalsukan; signature di byte awal jauh lebih sulit.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import MeasurementSession, VideoStorageRef


class VideoValidationError(ValueError):
    """Upload ditolak sebelum menyentuh filesystem."""


ALLOWED_EXTENSIONS = {".mp4", ".mov"}

# 60 detik video wajah 720p ~15-40 MB; 100 MB sudah memberi ruang lega untuk
# bitrate tinggi. Batas ini juga membatasi pemakaian RAM, karena berkas dibaca
# utuh ke memori (lihat catatan di `save_video`).
# ponytail: baca ke memori, bukan streaming ke disk. Cukup untuk skala keluarga
# (2-8 orang, jarang bersamaan); ganti ke streaming kalau upload paralel jadi
# banyak atau RAM VPS ketat.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

MIN_UPLOAD_BYTES = 1024

# Video hanya boleh dibaca pemilik proses — di VPS bersama, mode default
# 0644 berarti user lain di mesin yang sama bisa membaca wajah orang.
FILE_MODE = 0o600
DIRECTORY_MODE = 0o700

# Signature kontainer di awal file. MP4/MOV menempatkan 'ftyp' pada byte 4-8;
# beberapa varian MOV memakai atom lain.
_MP4_BRANDS = (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip")


def build_storage_path(user_id: uuid.UUID, session_id: uuid.UUID) -> Path:
    """Susun path tujuan dari UUID saja.

    Menerima `uuid.UUID`; string sembarang ditolak, jadi tidak ada input
    user yang bisa membelokkan lokasi tulis.
    """
    safe_user = _require_uuid(user_id, "user_id")
    safe_session = _require_uuid(session_id, "session_id")

    root = Path(get_settings().video_storage_path).resolve()
    path = root / str(safe_user) / f"{safe_session}.mp4"

    # Sabuk pengaman kedua: apa pun yang terjadi di atas, hasilnya harus
    # tetap berada di dalam direktori penyimpanan.
    if not path.resolve().is_relative_to(root):
        raise VideoValidationError("Path video keluar dari direktori penyimpanan")
    return path


def _require_uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise VideoValidationError(f"{field} harus UUID yang valid") from None


def validate_upload(filename: str, content: bytes) -> None:
    """Periksa nama, ukuran, dan isi berkas. Lempar `VideoValidationError`
    kalau tidak memenuhi syarat."""
    extension = Path(filename or "").suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise VideoValidationError(
            f"Format tidak didukung. Gunakan {allowed}."
        )

    if len(content) > MAX_UPLOAD_BYTES:
        limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        raise VideoValidationError(f"Ukuran file terlalu besar, maksimal {limit_mb} MB")

    if len(content) < MIN_UPLOAD_BYTES:
        raise VideoValidationError("File kosong atau terlalu kecil untuk sebuah video")

    if not _looks_like_video(content):
        raise VideoValidationError("Isi file bukan video yang dikenali")


def _looks_like_video(content: bytes) -> bool:
    """Cek signature kontainer, bukan sekadar percaya ekstensi."""
    return any(brand in content[:32] for brand in _MP4_BRANDS)


def save_video(
    db: Session,
    session: MeasurementSession,
    filename: str,
    content: bytes,
) -> VideoStorageRef:
    """Simpan video ke disk lalu catat metadatanya.

    File ditulis lebih dulu; baris DB baru ditambahkan setelah penulisan
    berhasil, supaya tidak ada metadata yang menunjuk file tidak ada.
    Pemanggil yang melakukan `commit`.
    """
    validate_upload(filename, content)

    path = build_storage_path(session.user_id, session.id)
    _write_file(path, content)

    ref = VideoStorageRef(
        measurement_session_id=session.id,
        storage_provider="vps_local",
        storage_path=str(path),
        file_size_bytes=len(content),
    )
    db.add(ref)
    return ref


def _write_file(path: Path, content: bytes) -> None:
    _mkdir_private(path.parent)
    path.write_bytes(content)
    path.chmod(FILE_MODE)


def _mkdir_private(directory: Path) -> None:
    """Buat direktori beserta induknya dengan permission ketat.

    `mkdir(parents=True, mode=...)` hanya menerapkan mode ke direktori
    terdalam — induknya memakai umask default (biasanya 0755), sehingga
    daftar UUID pengguna bisa dibaca user lain di VPS bersama.
    """
    root = Path(get_settings().video_storage_path).resolve()
    directory.mkdir(parents=True, exist_ok=True)

    # Rapikan permission dari root penyimpanan ke bawah.
    for level in [root, *reversed(directory.parents), directory]:
        if level.is_relative_to(root) and level.exists():
            level.chmod(DIRECTORY_MODE)


def delete_video(db: Session, ref: VideoStorageRef) -> None:
    """Hapus file fisik dan tandai barisnya.

    Baris tidak dihapus — jejak bahwa video pernah ada tetap disimpan
    (ERD §2.5). File yang sudah hilang duluan bukan kesalahan.
    """
    Path(ref.storage_path).unlink(missing_ok=True)
    ref.deleted_at = datetime.now(UTC)
