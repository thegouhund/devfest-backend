"""urutan pesan percakapan

Tambah nomor urut giliran. `created_at` saja tidak cukup: beberapa giliran
sering tertulis dalam detik yang sama, dan tanpa penomoran eksplisit
urutannya jatuh ke `id` yang berupa UUID acak — jawaban bot bisa muncul
sebelum pertanyaannya saat riwayat dimuat ulang.

Revision ID: 74eb6801858a
Revises: 8be6faa7590d
Create Date: 2026-09-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "74eb6801858a"
down_revision: Union[str, None] = "8be6faa7590d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversation_messages",
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_unique_constraint(
        "uq_message_sequence", "conversation_messages", ["conversation_id", "sequence"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_message_sequence", "conversation_messages", type_="unique")
    op.drop_column("conversation_messages", "sequence")
