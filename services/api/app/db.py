"""Database wiring — AWS RDS Postgres in prod, SQLite fallback locally.

Only std SQLAlchemy 2.0 (sync). Same `get_session` dependency is used by
every router, so swapping the URL is all it takes to go from laptop to RDS.
The schema is managed by Alembic (services/api/migrations); `init_db()` upgrades
to the latest revision on startup, so a fresh RDS database needs no manual step.
It also creates the database itself if the server doesn't have it yet, and recreates
any table that is missing even though the migration history says it was applied.
"""
import logging
import os

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

log = logging.getLogger("app.db")

EMBEDDING_DIM = 1024  # amazon.nova-2-multimodal-embeddings-v1:0 at embeddingDimension=1024
# set by init_db(): whether chunk_embeddings exists (Postgres with pgvector enabled)
vectors_ready: bool | None = None


class Base(DeclarativeBase):
    pass


@event.listens_for(Engine, "connect")
def _sqlite_foreign_keys(dbapi_conn, _):
    # SQLite ignores ON DELETE CASCADE unless asked; Postgres always enforces it
    if type(dbapi_conn).__module__.startswith("sqlite3"):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")


def _database_url() -> str:
    url = (settings.database_url or "").strip()
    if url:
        return url
    os.makedirs(settings.data_dir, exist_ok=True)
    return f"sqlite:///{os.path.join(settings.data_dir, 'sherpa.db')}"


def _make_engine(url: str | None = None):
    url = url or _database_url()
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    # Remote Postgres (RDS): require SSL unless the URL says otherwise; recycle pooled connections.
    host = url.split("@")[-1].split("/")[0].split(":")[0]
    if "sslmode" not in url and host not in ("localhost", "127.0.0.1", "db"):
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return create_engine(url, pool_pre_ping=True, pool_recycle=300)


def _backup_url() -> str:
    url = (settings.database_url_backup or "").strip()
    return "" if url == (settings.database_url or "").strip() else url


_primary_engine = _make_engine()
_backup_engine = _make_engine(_backup_url()) if _backup_url() else None
engine = _primary_engine
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
_active_db = "primary"


def active_database() -> str:
    """`primary` or `backup` — which DATABASE_URL requests are currently using."""
    return _active_db


def _ping(candidate: Engine, name: str) -> bool:
    try:
        with candidate.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:  # noqa: BLE001 — any failure means "not usable right now"
        log.warning("db: %s database unreachable (%s)", name, type(e).__name__)
        return False


def _use(target: Engine, name: str) -> None:
    global engine, SessionLocal, _active_db
    if engine is not target:
        engine = target
        SessionLocal = sessionmaker(bind=target, autoflush=False, expire_on_commit=False)
    _active_db = name
    log.warning("db: now using the %s database", name)


def choose_database() -> str:
    """At startup: keep the primary when it answers, otherwise fall back to the backup."""
    if _backup_engine is not None and not _ping(_primary_engine, "primary"):
        if _ping(_backup_engine, "backup"):
            _use(_backup_engine, "backup")
    return _active_db


@event.listens_for(_primary_engine, "handle_error")
def _fail_over_on_disconnect(ctx):
    """The primary dropped a connection: switch to the backup if it is usable.

    Only real disconnects trigger this (a syntax/constraint error must not); the request
    that hit the failure still errors, but every request after it uses the backup. We
    don't auto-switch back — a restart re-tries the primary, which keeps the check free
    and lets a free-tier database scale to zero between requests.
    """
    if _backup_engine is None or _active_db != "primary" or not ctx.is_disconnect:
        return
    if _ping(_backup_engine, "backup"):
        _use(_backup_engine, "backup")


API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ensure_database() -> None:
    """Create the Postgres database named in DATABASE_URL if the server doesn't have it
    (e.g. pointing at a new database name on the same RDS instance)."""
    if engine.dialect.name != "postgresql":
        return  # SQLite creates its file on first connect
    try:
        with engine.connect():
            return
    except OperationalError as e:
        if "does not exist" not in str(e.orig):
            raise
    name = engine.url.database
    admin = create_engine(engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql("CREATE DATABASE " + conn.dialect.identifier_preparer.quote(name))
        log.warning("startup: created database %s", name)
    except ProgrammingError as e:
        if "already exists" not in str(e.orig):  # another replica got there first
            raise
    finally:
        admin.dispose()


def ensure_vector_table(conn, dim: int = EMBEDDING_DIM) -> bool:
    """Enable pgvector and create chunk_embeddings if missing. Returns False when the
    extension can't be enabled (e.g. the DB user lacks rds_superuser): search then falls
    back to TF-IDF instead of the whole startup failing."""
    if conn.dialect.name != "postgresql":
        return False
    try:
        with conn.begin_nested():
            conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
            # Content-addressed: identical chunk text is embedded once, whoever indexes it.
            # Search scans one repo's rows exactly (filtered by repo_id), so no ANN index is
            # needed at this scale; add HNSW here if repos grow past ~50k chunks.
            conn.exec_driver_sql(f"""
                CREATE TABLE IF NOT EXISTS chunk_embeddings (
                    model     varchar(120) NOT NULL,
                    text_hash varchar(40)  NOT NULL,
                    embedding vector({dim}) NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (model, text_hash)
                )""")
        return True
    except DBAPIError as e:
        log.warning("startup: pgvector unavailable, embeddings off: %s", str(e.orig).strip())
        return False


def _create_missing_tables(conn) -> None:
    """Recreate tables the models define but the database lacks, e.g. dropped by hand while
    alembic_version still says head (an upgrade alone would then do nothing)."""
    from . import dbmodels  # noqa: F401 — register tables on Base.metadata

    have = set(inspect(conn).get_table_names())
    missing = [t for name, t in Base.metadata.tables.items() if name not in have]
    if missing:
        log.warning("startup: creating missing tables: %s", ", ".join(t.name for t in missing))
        Base.metadata.create_all(conn, tables=missing)


def init_db() -> None:
    """Make sure the database and every table exist, applying pending Alembic migrations."""
    global vectors_ready
    from alembic import command
    from alembic.config import Config

    _ensure_database()
    cfg = Config(os.path.join(API_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(API_DIR, "migrations"))
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            # several replicas starting at once must not migrate concurrently
            conn.exec_driver_sql("SELECT pg_advisory_xact_lock(724205)")
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        _create_missing_tables(conn)
        vectors_ready = ensure_vector_table(conn)


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
