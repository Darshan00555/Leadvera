import os
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import Config


class Base(DeclarativeBase):
    pass


def _make_engine(url):
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(eng, "connect")
        def _pragmas(conn, _):
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
        return eng
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)


engine = _make_engine(Config.DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def configure(url):
    """Rebind to another database (used by tests and the CLI)."""
    global engine
    engine = _make_engine(url)
    SessionLocal.configure(bind=engine)
    return engine


def init_db():
    from . import models  # noqa: F401  (register tables)
    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns():
    """create_all() never alters existing tables: add nullable columns introduced since the database was created."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    pg = engine.dialect.name == "postgresql"
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have or col.primary_key or not col.nullable:
                continue
            ddl = (f'ALTER TABLE {table.name} ADD COLUMN {"IF NOT EXISTS " if pg else ""}{col.name} '
                   f'{col.type.compile(dialect=engine.dialect)}')
            try:
                with engine.begin() as conn:
                    conn.execute(text(ddl))
            except Exception:  # another process (web / worker) added it first
                pass


@contextmanager
def session_scope():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
