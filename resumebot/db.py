"""Engine, sessions, and small helpers used everywhere."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import event as sa_event
from sqlmodel import Session, SQLModel, create_engine, select

from . import models
from .config import DATA_DIR, ensure_dirs

ensure_dirs()
engine = create_engine(
    f"sqlite:///{DATA_DIR / 'resumebot.db'}",
    connect_args={"check_same_thread": False, "timeout": 30},
)


@sa_event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    add_missing_columns()


def add_missing_columns() -> list[str]:
    """Tiny forward-only migration: add columns that newer models declare to existing tables.

    SQLite can add a column in place; the Python default becomes the column default so old rows
    read back correctly. Returns the columns added (for logs/tests).
    """
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    added: list[str] = []
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
                default = col.default.arg if col.default is not None else None
                if isinstance(default, bool):
                    ddl += f" DEFAULT {int(default)}"
                elif isinstance(default, (int, float)):
                    ddl += f" DEFAULT {default}"
                elif isinstance(default, str):
                    ddl += " DEFAULT '" + default.replace("'", "''") + "'"
                conn.execute(text(ddl))
                added.append(f"{table.name}.{col.name}")
    return added


@contextmanager
def session() -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as s:
        yield s


def log(message: str, *, level: str = "info", source: str = "", kind: str = "system",
        job_id: int | None = None) -> None:
    import sys
    from . import diagnostics
    message = diagnostics.redact(message)
    diagnostics.record(message, level=level, kind=kind, job_id=job_id,
                       error=sys.exception() if level in ("error", "warning") else None)
    with session() as s:
        s.add(models.Event(message=message, level=level, source=source, kind=kind, job_id=job_id))
        s.commit()


def kv_get(key: str, default: Any = None) -> Any:
    with session() as s:
        row = s.get(models.KV, key)
        return row.value if row else default


def kv_set(key: str, value: Any) -> None:
    with session() as s:
        row = s.get(models.KV, key) or models.KV(key=key)
        row.value = value
        s.add(row)
        s.commit()


def source_state(source: str) -> models.SourceState:
    with session() as s:
        st = s.get(models.SourceState, source)
        if st is None:
            st = models.SourceState(source=source)
            s.add(st)
            s.commit()
        return st


def save(obj: Any) -> Any:
    with session() as s:
        s.add(obj)
        s.commit()
        s.refresh(obj)
        return obj


__all__ = ["engine", "init_db", "session", "log", "kv_get", "kv_set", "source_state", "save", "select"]
