"""Master Script Migrasi Skema & Seeding Data PostgreSQL.

Script ini menginisialisasi skema tabel, akun pengguna default, master OPD,
master Periode, master Pegawai, serta memigrasikan seluruh riwayat data presensi
dari file Excel (data/) ke database PostgreSQL.

Dapat dijalankan kapan saja oleh developer dengan perintah:
    python -m database.migrate_and_seed
atau via Docker:
    docker compose exec web python -m database.migrate_and_seed
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from database.auth import list_users, seed_default_users, users
from database.connection import get_engine
from database.opd import list_opds, opd_table, seed_default_opds
from database.pegawai import list_pegawai, pegawai_table
from database.periode import get_or_create_periode, list_periode, periode_table
from database.presensi import (
    init_presensi_schema,
    presensi_table,
    save_presensi_dataframe_to_db,
)
from database.repository import (
    create_schema,
    presensi_harian,
    save_daily_attendance_to_db,
)
from modules.excel_parser import load_semua_presensi

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
LOGGER = logging.getLogger("migration_seed")


def seed_all_periods(engine: Engine, year: int = 2026) -> list[int]:
    """Inisialisasi seluruh 12 periode bulanan untuk tahun berjalan."""
    created_ids: list[int] = []
    for month in range(1, 13):
        pid = get_or_create_periode(engine, month, year)
        created_ids.append(pid)
    return created_ids


def run_migrations_and_seeds(engine: Engine | None = None) -> dict[str, Any]:
    """Jalankan seluruh rangkaian migrasi skema dan seed data secara idempotent."""
    start_time = time.time()
    own_engine = False
    if engine is None:
        engine = get_engine()
        own_engine = True

    results: dict[str, Any] = {}

    try:
        LOGGER.info("============================================================")
        LOGGER.info("🚀 MEMULAI MIGRASI SKEMA & SEEDING DATABASE POSTGRESQL")
        LOGGER.info("============================================================")

        # 1. Buat / Update seluruh skema tabel
        LOGGER.info("1️⃣  Memverifikasi dan membuat skema tabel database...")
        create_schema(engine)
        init_presensi_schema(engine)
        LOGGER.info("    ✅ Skema tabel terverifikasi.")

        # 2. Seed Default Users (admin, operator, pimpinan)
        LOGGER.info("2️⃣  Melakukan seed akun pengguna default (users)...")
        seeded_users = seed_default_users(engine)
        all_users = list_users(engine)
        if seeded_users:
            LOGGER.info("    ✅ User baru dibuat: %s", ", ".join(seeded_users))
        else:
            LOGGER.info("    ℹ️  User default sudah tersedia (%d akun)", len(all_users))
        results["total_users"] = len(all_users)

        # 3. Seed Master OPD
        LOGGER.info("3️⃣  Melakukan seed master OPD / Dinas (opd)...")
        seeded_opds = seed_default_opds(engine)
        all_opds = list_opds(engine, include_deleted=True)
        if seeded_opds:
            LOGGER.info("    ✅ OPD baru dibuat: %s", ", ".join(seeded_opds))
        else:
            LOGGER.info("    ℹ️  Master OPD sudah tersedia (%d OPD)", len(all_opds))
        results["total_opds"] = len(all_opds)

        # 4. Seed Master Periode 2026
        LOGGER.info("4️⃣  Melakukan seed master Periode Bulanan 2026 (periode)...")
        seed_all_periods(engine, year=2026)
        all_periods = list_periode(engine)
        LOGGER.info("    ✅ Master periode aktif: %d periode", len(all_periods))
        results["total_periode"] = len(all_periods)

        # 5. Migrasi & Sinkronisasi Existing Data Presensi dari Excel (data/)
        LOGGER.info("5️⃣  Membaca dan memigrasikan data presensi dari file Excel (data/)...")
        clean_daily = load_semua_presensi()
        total_excel_rows = len(clean_daily)
        LOGGER.info("    📄 Total baris data presensi terbaca: %d baris", total_excel_rows)

        if total_excel_rows > 0:
            # 5a. Simpan ke presensi_harian (tabel flat/ETL)
            LOGGER.info("    🔄 Menyinkronkan ke tabel 'presensi_harian'...")
            rep_harian = save_daily_attendance_to_db(clean_daily, engine)
            LOGGER.info(
                "       Inserted: %d | Updated: %d | Rejected: %d",
                rep_harian["inserted"], rep_harian["updated"], rep_harian["rejected"]
            )

            # 5b. Simpan ke presensi relasional & pegawai (tabel presensi & pegawai)
            LOGGER.info("    🔄 Menyinkronkan ke tabel relasional 'presensi' & 'pegawai'...")
            rep_rel = save_presensi_dataframe_to_db(clean_daily, engine)
            LOGGER.info(
                "       Processed: %d | Records: %d | Rejected: %d",
                rep_rel["processed"], rep_rel["inserted"], rep_rel["rejected"]
            )
            results["presensi_migrated"] = rep_rel["inserted"]
        else:
            LOGGER.warning("    ⚠️ Tidak ada file presensi di direktori data/ yang ditemukan.")
            results["presensi_migrated"] = 0

        # Hitung statistik akhir di PostgreSQL
        with engine.connect() as conn:
            total_pegawai = conn.execute(select(func.count(pegawai_table.c.id_pegawai))).scalar() or 0
            total_presensi = conn.execute(select(func.count(presensi_table.c.id_presensi))).scalar() or 0
            total_presensi_harian = conn.execute(select(func.count(presensi_harian.c.id))).scalar() or 0

        results["total_pegawai"] = total_pegawai
        results["total_presensi"] = total_presensi
        results["total_presensi_harian"] = total_presensi_harian

        elapsed = time.time() - start_time
        LOGGER.info("============================================================")
        LOGGER.info("🎉 MIGRASI & SEEDING SELESAI DALAM %.2f DETIK", elapsed)
        LOGGER.info("============================================================")
        LOGGER.info("📊 Ringkasan Data PostgreSQL:")
        LOGGER.info("   - Pengguna (users)         : %d", results["total_users"])
        LOGGER.info("   - Master OPD (opd)          : %d", results["total_opds"])
        LOGGER.info("   - Master Pegawai (pegawai)  : %d", results["total_pegawai"])
        LOGGER.info("   - Master Periode (periode)  : %d", results["total_periode"])
        LOGGER.info("   - Presensi Relasional       : %d", results["total_presensi"])
        LOGGER.info("   - Presensi Harian (ETL)     : %d", results["total_presensi_harian"])
        LOGGER.info("============================================================")

        return results

    finally:
        if own_engine:
            engine.dispose()


if __name__ == "__main__":
    try:
        run_migrations_and_seeds()
    except Exception as exc:
        LOGGER.error("Terjadi kesalahan saat migrasi & seed: %s", exc, exc_info=True)
        sys.exit(1)
