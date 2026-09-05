from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings
from app.db.models import Base


config = context.config

# URL diambil dari settings (environment), bukan dari alembic.ini, supaya
# kredensial tidak ikut ter-commit.
config.set_main_option("sqlalchemy.url", get_settings().database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Objek yang dibuat lewat SQL mentah di migrasi, bukan lewat model. Tanpa
# daftar ini autogenerate mengira keduanya "kelebihan" dan menyusun perintah
# DROP di setiap migrasi baru — menghapusnya akan merusak pencarian RAG dan
# hypertable Timescale.
MANUALLY_MANAGED_INDEXES = {
    "ix_health_facts_embedding",      # HNSW untuk similarity search pgvector
    "vitals_readings_recorded_at_idx",  # dibuat otomatis oleh create_hypertable
}


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "index" and name in MANUALLY_MANAGED_INDEXES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
