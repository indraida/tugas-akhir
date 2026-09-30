"""Koneksi PostgreSQL berbasis environment variable."""

from __future__ import annotations

import functools
import logging
import os

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

LOGGER = logging.getLogger(__name__)


def ensure_database_exists(database_url: str) -> None:
    """Otomatis buat database target (misal: mydb) jika belum ada di server PostgreSQL."""
    try:
        url_obj = make_url(database_url)
        target_db = url_obj.database
        if not target_db:
            return
        
        # Coba koneksi langsung terlebih dahulu
        test_engine = create_engine(database_url, pool_pre_ping=True)
        try:
            with test_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as exc:
            # Jika errornya karena database target belum ada, buat otomatis
            err_msg = str(exc).lower()
            if f'database "{target_db.lower()}" does not exist' not in err_msg and "does not exist" not in err_msg:
                return
        finally:
            test_engine.dispose()

        # Koneksi ke database maintenance default 'postgres' untuk membuat database target
        root_url = url_obj.set(database="postgres")
        admin_engine = create_engine(root_url, isolation_level="AUTOCOMMIT")
        try:
            with admin_engine.connect() as conn:
                exists = conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"),
                    {"name": target_db},
                ).scalar()
                if not exists:
                    LOGGER.info("Database '%s' belum ada. Membuat database '%s' secara otomatis...", target_db, target_db)
                    conn.execute(text(f'CREATE DATABASE "{target_db}"'))
                    LOGGER.info("Database '%s' berhasil dibuat.", target_db)
        finally:
            admin_engine.dispose()
    except Exception as exc:
        LOGGER.debug("Gagal memeriksa / membuat database secara otomatis: %s", exc)


@functools.lru_cache(maxsize=4)
def _create_cached_engine(url: str) -> Engine:
    ensure_database_exists(url)
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

