"""Script untuk inisialisasi tabel users dan seed akun default ke PostgreSQL.

Jalankan dengan:
    python -m database.seed_users
"""

from __future__ import annotations

import logging
import sys

from database.connection import get_engine
from database.auth import seed_default_users, list_users

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
LOGGER = logging.getLogger(__name__)


def main() -> None:
    LOGGER.info("Menghubungkan ke PostgreSQL...")
    try:
        engine = get_engine()
        created = seed_default_users(engine)
        if created:
            LOGGER.info("Berhasil membuat user default: %s", ", ".join(created))
        else:
            LOGGER.info("Semua user default sudah ada di database.")
            
        users = list_users(engine)
        LOGGER.info("Total pengguna terdaftar di PostgreSQL: %d", len(users))
        for u in users:
            status = "Aktif" if u["is_active"] else "Nonaktif"
            LOGGER.info(
                " - [%s] %s (%s) - Role: %s - Status: %s",
                u["id"], u["username"], u["nama_lengkap"], u["role"], status
            )
    except Exception as exc:
        LOGGER.error("Gagal melakukan seed user ke PostgreSQL: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
