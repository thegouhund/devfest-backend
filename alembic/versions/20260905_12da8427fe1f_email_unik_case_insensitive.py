"""email unik case-insensitive

Ganti unique constraint biasa pada `users.email` dengan unique index pada
`lower(email)`, supaya "Budi@x.com" dan "budi@x.com" tidak bisa jadi dua
akun berbeda lewat jalur mana pun (register, seed, import, profil dependent).

Index-nya partial (`WHERE email IS NOT NULL`) karena dependent tidak punya
email dan banyak baris NULL harus tetap boleh berdampingan.

Catatan: autogenerate juga mengusulkan menghapus `ix_health_facts_embedding`
(HNSW) dan `vitals_readings_recorded_at_idx` (dibuat TimescaleDB). Keduanya
sengaja tidak diikutkan — objek itu dibuat manual di migrasi awal dan tidak
tercermin di model, jadi menghapusnya akan merusak RAG dan hypertable.

Revision ID: 12da8427fe1f
Revises: 69c074b30f2e
Create Date: 2026-09-05 15:31:04.700942

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '12da8427fe1f'
down_revision: Union[str, None] = '69c074b30f2e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('users_email_key', 'users', type_='unique')
    op.create_index(
        'uq_users_email_lower',
        'users',
        [sa.text('lower(email)')],
        unique=True,
        postgresql_where=sa.text('email IS NOT NULL'),
        sqlite_where=sa.text('email IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_users_email_lower', table_name='users')
    op.create_unique_constraint('users_email_key', 'users', ['email'])
