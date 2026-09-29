"""Koneksi PostgreSQL berbasis environment variable."""

from __future__ import annotations

import functools
import os

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine, text


@functools.lru_cache(maxsize=4)
def _create_cached_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)


def get_engine(database_url: str | None = None) -> Engine:
    """Buat engine sekali per URL tanpa mengekspos credential ke log."""
    load_dotenv()
    url = database_url or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL belum dikonfigurasi.")
    if not url.startswith(("postgresql://", "postgresql+psycopg2://")):
        raise RuntimeError("DATABASE_URL harus menggunakan PostgreSQL.")
    return _create_cached_engine(url)


def check_database_connection(database_url: str | None = None) -> tuple[bool, str]:
    """Uji konektivitas ke database PostgreSQL."""
    try:
        engine = get_engine(database_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "Koneksi database PostgreSQL berhasil."
    except Exception as exc:
        return False, str(exc)

