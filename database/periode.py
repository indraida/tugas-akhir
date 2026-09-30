"""Modul schema & operasi database PostgreSQL untuk Master Periode Presensi."""

from __future__ import annotations

import calendar
import logging
from datetime import date
from typing import Any

from sqlalchemy import (
    BigInteger, Column, Date, Index, Integer, SmallInteger,
    String, Table, UniqueConstraint, func, select, update,
)
from sqlalchemy.engine import Engine

from database.schema import metadata

LOGGER = logging.getLogger(__name__)

periode_table = Table(
    "periode",
    metadata,
    Column("id_periode", BigInteger, primary_key=True, autoincrement=True),
    Column("bulan", SmallInteger, nullable=False),
    Column("tahun", SmallInteger, nullable=False),
    Column("tanggal_mulai", Date, nullable=False),
    Column("tanggal_selesai", Date, nullable=False),
    Column("keterangan", String(255), nullable=True),
    UniqueConstraint("bulan", "tahun", name="uq_periode_bulan_tahun"),
)

Index("idx_periode_tahun_bulan", periode_table.c.tahun, periode_table.c.bulan)

MONTH_NAMES_ID = {
    1: "Januari", 2: "Februari", 3: "Maret", 4: "April", 5: "Mei", 6: "Juni",
    7: "Juli", 8: "Agustus", 9: "September", 10: "Oktober", 11: "November", 12: "Desember",
}


def init_periode_schema(engine: Engine) -> None:
    """Buat tabel periode jika belum ada."""
    metadata.create_all(engine)


def get_or_create_periode(
    engine: Engine,
    bulan: int,
    tahun: int,
    keterangan: str | None = None,
) -> int:
    """Ambil ID periode yang cocok, atau buat baru jika belum ada."""
    init_periode_schema(engine)
    b = int(bulan)
    t = int(tahun)
    
    with engine.connect() as conn:
        stmt = select(periode_table.c.id_periode).where(
            periode_table.c.bulan == b,
            periode_table.c.tahun == t,
        )
        existing_id = conn.execute(stmt).scalar_one_or_none()
        if existing_id is not None:
            return int(existing_id)

    # Buat periode otomatis
    _, last_day = calendar.monthrange(t, b)
    tgl_mulai = date(t, b, 1)
    tgl_selesai = date(t, b, last_day)
    ket = keterangan or f"Periode Presensi {MONTH_NAMES_ID.get(b, str(b))} {t}"

    with engine.begin() as conn:
        stmt_check = select(periode_table.c.id_periode).where(
            periode_table.c.bulan == b,
            periode_table.c.tahun == t,
        )
        existing_id = conn.execute(stmt_check).scalar_one_or_none()
        if existing_id is not None:
            return int(existing_id)

        result = conn.execute(
            periode_table.insert().values(
                bulan=b,
                tahun=t,
                tanggal_mulai=tgl_mulai,
                tanggal_selesai=tgl_selesai,
                keterangan=ket,
            )
        )
        return int(result.inserted_primary_key[0])


def list_periode(engine: Engine) -> list[dict[str, Any]]:
    """Daftar semua periode yang tersimpan di PostgreSQL."""
    init_periode_schema(engine)
    with engine.connect() as conn:
        stmt = select(periode_table).order_by(
            periode_table.c.tahun.desc(),
            periode_table.c.bulan.desc(),
        )
        rows = conn.execute(stmt).mappings().all()
        return [
            {
                "id_periode": r["id_periode"],
                "bulan": r["bulan"],
                "nama_bulan": MONTH_NAMES_ID.get(r["bulan"], str(r["bulan"])),
                "tahun": r["tahun"],
                "tanggal_mulai": r["tanggal_mulai"],
                "tanggal_selesai": r["tanggal_selesai"],
                "keterangan": r["keterangan"] or "",
            }
            for r in rows
        ]


def get_periode_by_id(engine: Engine, id_periode: int) -> dict[str, Any] | None:
    """Ambil data satu periode berdasarkan ID."""
    if not id_periode:
        return None
    init_periode_schema(engine)
    with engine.connect() as conn:
        stmt = select(periode_table).where(periode_table.c.id_periode == int(id_periode))
        r = conn.execute(stmt).mappings().one_or_none()
        if r is None:
            return None
        return {
            "id_periode": r["id_periode"],
            "bulan": r["bulan"],
            "nama_bulan": MONTH_NAMES_ID.get(r["bulan"], str(r["bulan"])),
            "tahun": r["tahun"],
            "tanggal_mulai": r["tanggal_mulai"],
            "tanggal_selesai": r["tanggal_selesai"],
            "keterangan": r["keterangan"] or "",
        }


def create_periode(engine: Engine, data: dict[str, Any]) -> dict[str, Any]:
    """Tambah periode baru manual."""
    bulan = int(data["bulan"])
    tahun = int(data["tahun"])
    tgl_mulai = data.get("tanggal_mulai")
    tgl_selesai = data.get("tanggal_selesai")
    keterangan = (data.get("keterangan") or "").strip()

    if not (1 <= bulan <= 12):
        raise ValueError("Bulan harus di antara 1 sampai 12.")
    if tahun < 2000 or tahun > 2100:
        raise ValueError("Tahun tidak valid.")

    if not tgl_mulai or not tgl_selesai:
        _, last_day = calendar.monthrange(tahun, bulan)
        tgl_mulai = date(tahun, bulan, 1)
        tgl_selesai = date(tahun, bulan, last_day)

    init_periode_schema(engine)
    with engine.begin() as conn:
        stmt = select(periode_table.c.id_periode).where(
            periode_table.c.bulan == bulan,
            periode_table.c.tahun == tahun,
        )
        if conn.execute(stmt).scalar_one_or_none() is not None:
            raise ValueError(f"Periode {MONTH_NAMES_ID.get(bulan, bulan)} {tahun} sudah ada di database.")

        result = conn.execute(
            periode_table.insert().values(
                bulan=bulan,
                tahun=tahun,
                tanggal_mulai=tgl_mulai,
                tanggal_selesai=tgl_selesai,
                keterangan=keterangan or f"Periode Presensi {MONTH_NAMES_ID.get(bulan, bulan)} {tahun}",
            )
        )
        new_id = result.inserted_primary_key[0]
        
    return get_periode_by_id(engine, new_id) or {"id_periode": new_id, "bulan": bulan, "tahun": tahun}


def update_periode(engine: Engine, id_periode: int, data: dict[str, Any]) -> tuple[bool, str]:
    """Perbarui informasi periode."""
    init_periode_schema(engine)
    with engine.begin() as conn:
        stmt = select(periode_table).where(periode_table.c.id_periode == int(id_periode))
        target = conn.execute(stmt).mappings().one_or_none()
        if target is None:
            return False, "Periode tidak ditemukan."

        values: dict[str, Any] = {}
        if "tanggal_mulai" in data and data["tanggal_mulai"]:
            values["tanggal_mulai"] = data["tanggal_mulai"]
        if "tanggal_selesai" in data and data["tanggal_selesai"]:
            values["tanggal_selesai"] = data["tanggal_selesai"]
        if "keterangan" in data:
            values["keterangan"] = str(data["keterangan"]).strip()

        if values:
            conn.execute(
                update(periode_table)
                .where(periode_table.c.id_periode == int(id_periode))
                .values(**values)
            )
    return True, "Data periode berhasil diperbarui."


def delete_periode(engine: Engine, id_periode: int) -> tuple[bool, str]:
    """Hapus periode dari database."""
    init_periode_schema(engine)
    with engine.begin() as conn:
        stmt = select(periode_table).where(periode_table.c.id_periode == int(id_periode))
        target = conn.execute(stmt).mappings().one_or_none()
        if target is None:
            return False, "Periode tidak ditemukan."

        conn.execute(periode_table.delete().where(periode_table.c.id_periode == int(id_periode)))
    return True, f"Periode {MONTH_NAMES_ID.get(target['bulan'], target['bulan'])} {target['tahun']} berhasil dihapus."
