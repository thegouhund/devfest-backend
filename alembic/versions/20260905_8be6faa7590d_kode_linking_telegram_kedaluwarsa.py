"""kode linking telegram kedaluwarsa

Kontrak API menjanjikan kode linking yang kedaluwarsa, tapi ERD belum punya
kolomnya. Ditambahkan di sini karena kode yang bocor lewat screenshot atau
salah kirim tidak boleh berlaku selamanya — siapa pun yang menukarkannya
akan menerima notifikasi kesehatan keluarga ini.

`telegram_chat_id` juga dilonggarkan jadi nullable: saat kode diterbitkan,
chat id-nya memang belum diketahui, baru terisi ketika user mengirim kode
ke bot.

Catatan: autogenerate juga mengusulkan menghapus `ix_health_facts_embedding`
(HNSW) dan `vitals_readings_recorded_at_idx` (dibuat TimescaleDB). Keduanya
sengaja tidak diikutkan — objek itu dibuat manual di migrasi awal dan tidak
tercermin di model, jadi menghapusnya akan merusak RAG dan hypertable.

Revision ID: 8be6faa7590d
Revises: 12da8427fe1f
Create Date: 2026-09-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8be6faa7590d"
down_revision: Union[str, None] = "12da8427fe1f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "telegram_links",
        sa.Column("link_code_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column(
        "telegram_links",
        "telegram_chat_id",
        existing_type=sa.TEXT(),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "telegram_links",
        "telegram_chat_id",
        existing_type=sa.TEXT(),
        nullable=False,
    )
    op.drop_column("telegram_links", "link_code_expires_at")
