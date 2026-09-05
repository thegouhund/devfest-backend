"""Tools LangChain untuk health companion (PRD FR-4.1, FR-4.3).

Setiap tool adalah pembungkus tipis di atas fungsi `app/services/` yang
sudah ada — tidak ada yang query baris mentah, mengagregasi, atau menghitung
sendiri. Ini disengaja: dengan melewatkan semua tool ke service yang sama
dengan endpoint REST, angka yang disebut chatbot selalu sama dengan yang
tampil di dashboard, dan aturan privasi berlaku identik di kedua jalur.

**Id pelaku diikat lewat closure, tidak pernah jadi parameter tool.** Kalau
model bisa mengisi user id sendiri, satu kalimat yang dirancang khusus bisa
membujuknya membaca data kesehatan orang lain.

**Tiap tool membuka Session sendiri.** LangGraph menjalankan tool sinkron di
kumpulan thread pekerja, dan satu Session SQLAlchemy tidak aman dipakai
bersamaan lintas thread.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from langchain_core.tools import tool
from sqlalchemy import select

from app.db.models import Anomaly, FamilyMember
from app.schemas import ACTIVITY_CATEGORIES
from app.services import activity as activity_service
from app.services import statistics
from app.services.activity import NotAuthorisedToLog
from app.services.visibility import accessible_profile_ids


# Kategori aktivitas yang dikenali (ERD §2.10), diambil dari schema supaya
# tidak ada dua daftar yang bisa berbeda.
VALID_CATEGORIES = tuple(ACTIVITY_CATEGORIES.__args__)

# Batas rentang waktu yang boleh diminta model. Tanpa ini, satu pertanyaan
# bisa menarik data bertahun-tahun ke dalam jendela konteks.
MAX_DAYS = 365
DEFAULT_DAYS = 7

MAX_ROWS = 20


def _format_quantity(quantity, unit: str | None) -> str:
    """Format jumlah tanpa presisi berlebih.

    Kolom `Numeric` Postgres mengembalikan `Decimal`, dan format `:g` pada
    Decimal mempertahankan nol di belakang koma — "2.0000000000 cups" akan
    disalin apa adanya oleh model ke jawabannya.
    """
    if not quantity:
        return ""
    return f" {float(quantity):g} {unit or ''}".rstrip()


def make_tools(session_factory, actor: FamilyMember) -> list:
    """Bangun perangkat tool untuk satu user.

    `actor` diikat lewat closure: seluruh pemeriksaan izin memakai identitas
    ini, bukan apa pun yang ditulis model.
    """
    actor_id = actor.id

    def _clamp_days(days: int) -> int:
        return max(1, min(days or DEFAULT_DAYS, MAX_DAYS))

    def _resolve_subject(db, member_name: str | None) -> tuple[uuid.UUID | None, str | None]:
        """Terjemahkan nama anggota jadi id yang boleh dilihat pemanggil.

        Mengembalikan `(id, None)` bila boleh, atau `(None, pesan)` bila
        tidak — pesannya dikembalikan apa adanya ke model supaya ia
        menyampaikan penolakan, bukan menampilkan data.
        """
        if not member_name:
            return actor_id, None

        visible = accessible_profile_ids(db, actor_id, "vitals")
        kandidat = (
            db.execute(select(FamilyMember).where(FamilyMember.id.in_(visible)))
            .scalars()
            .all()
        )

        cocok = [
            u for u in kandidat if member_name.strip().lower() in u.full_name.lower()
        ]
        if not cocok:
            return None, (
                f"Tidak ada anggota keluarga bernama '{member_name}' yang datanya "
                "bisa Anda lihat."
            )
        if len(cocok) > 1:
            nama = ", ".join(u.full_name for u in cocok)
            return None, f"Ada beberapa anggota yang cocok: {nama}. Yang mana?"

        return cocok[0].id, None

    @tool
    def get_vitals_stats(
        metric_type: str, days: int = DEFAULT_DAYS, member_name: str | None = None
    ) -> str:
        """Ambil ringkasan statistik vital sign: rata-rata, minimum, maksimum,
        dan jumlah pengukuran dalam beberapa hari terakhir. metric_type diisi
        heart_rate, hrv_rmssd, atau respiration_rate. days adalah berapa hari
        ke belakang. member_name diisi hanya kalau user bertanya tentang
        anggota keluarga lain; kosongkan untuk data user sendiri."""
        db = session_factory()
        try:
            if not statistics.metric_exists(db, metric_type):
                pilihan = "heart_rate, hrv_rmssd, respiration_rate"
                return (
                    f"Metrik '{metric_type}' tidak dikenal. Yang tersedia: {pilihan}."
                )

            subject_id, penolakan = _resolve_subject(db, member_name)
            if penolakan:
                return penolakan

            end = datetime.now(UTC)
            start = end - timedelta(days=_clamp_days(days))

            hasil = statistics.aggregate(db, {subject_id}, metric_type, start, end)
            if hasil is None:
                siapa = member_name or "Anda"
                return f"Belum ada data {metric_type} untuk {siapa} pada periode itu."

            satuan = statistics.metric_unit(db, metric_type) or ""
            baseline = statistics.active_baseline(db, subject_id, metric_type)
            catatan_baseline = (
                f" Rata-rata normalnya {float(baseline.mean_value):.1f} {satuan}."
                if baseline
                else " Baseline personal belum terbentuk (butuh data lebih banyak)."
            )

            return (
                f"{metric_type}: rata-rata {hasil['avg']:.1f} {satuan}, "
                f"terendah {hasil['min']:.1f}, tertinggi {hasil['max']:.1f}, "
                f"dari {hasil['count']} pengukuran.{catatan_baseline}"
            )
        finally:
            db.close()

    @tool
    def get_recent_activities(
        days: int = DEFAULT_DAYS, category: str | None = None
    ) -> str:
        """Ambil riwayat aktivitas harian user seperti kopi, olahraga, tidur,
        merokok, alkohol, atau makan. days adalah berapa hari ke belakang.
        category diisi kalau user menanyakan satu jenis aktivitas saja."""
        db = session_factory()
        try:
            end = datetime.now(UTC)
            start = end - timedelta(days=_clamp_days(days))

            rows, total = activity_service.list_activities(
                db,
                viewer_id=actor_id,
                category=category,
                start=start,
                end=end,
                limit=MAX_ROWS,
            )
            if not rows:
                return "Belum ada aktivitas yang tercatat pada periode itu."

            baris = [
                f"- {r.category}"
                + _format_quantity(r.quantity, r.unit)
                + f" pada {r.occurred_at:%d %b %H:%M}"
                for r in rows
            ]
            return f"{total} aktivitas tercatat:\n" + "\n".join(baris)
        finally:
            db.close()

    @tool
    def get_anomaly_events(days: int = 30, member_name: str | None = None) -> str:
        """Ambil daftar anomali vital sign user: pengukuran yang menyimpang
        jauh dari kondisi normalnya. days adalah berapa hari ke belakang.
        Pakai ini kalau user bertanya soal peringatan, kejadian tidak biasa,
        atau kenapa dia dapat notifikasi. member_name diisi hanya kalau user
        bertanya tentang anggota keluarga lain; kosongkan untuk data user
        sendiri — walau user ini admin, jangan tarik data seluruh keluarga
        kalau tidak diminta eksplisit."""
        db = session_factory()
        try:
            subject_id, penolakan = _resolve_subject(db, member_name)
            if penolakan:
                return penolakan

            end = datetime.now(UTC)
            start = end - timedelta(days=_clamp_days(days))

            rows = (
                db.execute(
                    select(Anomaly)
                    .where(
                        Anomaly.family_member_id == subject_id,
                        Anomaly.detected_at >= start,
                        Anomaly.detected_at <= end,
                    )
                    .order_by(Anomaly.detected_at.desc())
                    .limit(MAX_ROWS)
                )
                .scalars()
                .all()
            )
            if not rows:
                return "Tidak ada anomali yang terdeteksi pada periode itu."

            baris = [
                f"- {r.metric_type} {float(r.observed_value):.0f} "
                f"(normalnya {float(r.baseline_mean):.0f}), "
                f"tingkat {r.severity}, {r.detected_at:%d %b %H:%M}"
                for r in rows
            ]
            return f"{len(rows)} anomali terdeteksi:\n" + "\n".join(baris)
        finally:
            db.close()

    @tool
    def log_activity(
        category: str,
        quantity: float | None = None,
        unit: str | None = None,
        note: str | None = None,
    ) -> str:
        """Catat aktivitas yang baru dilakukan user, misalnya ketika dia
        bilang "baru ngopi 2 cangkir" atau "tadi olahraga 30 menit".
        category harus salah satu dari coffee, exercise, smoking, alcohol,
        sleep, meal, other. quantity dan unit diisi kalau disebutkan."""
        db = session_factory()
        try:
            if category not in VALID_CATEGORIES:
                pilihan = ", ".join(VALID_CATEGORIES)
                return (
                    f"Kategori '{category}' tidak dikenal. "
                    f"Pilih salah satu: {pilihan}."
                )

            actor_row = db.get(FamilyMember, actor_id)
            try:
                activity_service.create_activity(
                    db,
                    actor=actor_row,
                    subject_id=None,
                    category=category,
                    quantity=quantity,
                    unit=unit,
                    note=note,
                    occurred_at=None,
                    # Membedakan entri chat dari tombol quick-menu (FR-4.3).
                    source="chat",
                )
            except NotAuthorisedToLog as exc:
                return str(exc)

            db.commit()
            return f"Tercatat: {category}{_format_quantity(quantity, unit)}."
        finally:
            db.close()

    @tool
    def get_user_profile() -> str:
        """Ambil profil fisik user: nama, tinggi badan, dan berat badan.
        Pakai ini kalau butuh konteks tubuh untuk menjelaskan angka vital
        sign, misalnya saat membahas rentang normal."""
        db = session_factory()
        try:
            user = db.get(FamilyMember, actor_id)
            if user is None:
                return "Profil tidak ditemukan."

            bagian = [f"Nama: {user.full_name}"]
            if user.height_cm:
                bagian.append(f"tinggi {float(user.height_cm):g} cm")
            if user.weight:
                bagian.append(f"berat {float(user.weight):g} kg")
            if user.date_of_birth:
                bagian.append(f"lahir {user.date_of_birth:%d %b %Y}")

            if len(bagian) == 1:
                bagian.append("data tinggi dan berat badan belum diisi")

            return ", ".join(bagian) + "."
        finally:
            db.close()

    return [
        get_vitals_stats,
        get_recent_activities,
        get_anomaly_events,
        log_activity,
        get_user_profile,
    ]
