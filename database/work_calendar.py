"""Persistensi master kalender kerja di PostgreSQL."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Boolean, Column, Date, DateTime, SmallInteger, String, Table, Text, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from database.schema import metadata


work_calendar_table = Table(
    "kalender_kerja",
    metadata,
    Column("tanggal", Date, primary_key=True),
    Column("jenis_hari", String(30), nullable=False),
    Column("keterangan", Text, nullable=False, server_default=""),
    Column("is_hari_kerja", Boolean, nullable=False),
    Column("dasar_hukum", Text, nullable=False, server_default=""),
    Column("tahun", SmallInteger, nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

DATABASE_COLUMNS = [
    "tanggal", "jenis_hari", "keterangan", "is_hari_kerja", "dasar_hukum",
    "tahun", "created_at", "updated_at",
]
VALID_DAY_TYPES = {"HARI_KERJA", "CUTI_BERSAMA", "LIBUR_NASIONAL", "AKHIR_PEKAN"}


def init_work_calendar_schema(engine: Engine) -> None:
    """Buat tabel kalender kerja bila belum tersedia."""
    metadata.create_all(engine, tables=[work_calendar_table])


def list_work_calendar(engine: Engine, year: int) -> pd.DataFrame:
    """Ambil kalender khusus untuk satu tahun dari PostgreSQL."""
    init_work_calendar_schema(engine)
    statement = (
        select(work_calendar_table)
        .where(work_calendar_table.c.tahun == int(year))
        .order_by(work_calendar_table.c.tanggal)
    )
    with engine.connect() as connection:
        rows = connection.execute(statement).mappings().all()
    result = pd.DataFrame(rows, columns=DATABASE_COLUMNS)
    if not result.empty:
        result["tanggal"] = pd.to_datetime(result["tanggal"]).dt.normalize()
    return result


def upsert_work_calendar(engine: Engine, frame: pd.DataFrame) -> int:
    """Validasi lalu insert/update kalender berdasarkan tanggal."""
    init_work_calendar_schema(engine)
    required = {"tanggal", "jenis_hari", "keterangan", "is_hari_kerja", "dasar_hukum", "tahun"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError("Kolom kalender tidak tersedia: " + ", ".join(missing))
    if frame.empty:
        return 0

    source = frame.copy()
    source["tanggal"] = pd.to_datetime(source["tanggal"], errors="coerce").dt.normalize()
    if source["tanggal"].isna().any():
        raise ValueError("Master kalender memuat tanggal yang tidak valid")
    source["jenis_hari"] = source["jenis_hari"].fillna("").astype(str).str.strip().str.upper()
    invalid = sorted(set(source["jenis_hari"]).difference(VALID_DAY_TYPES))
    if invalid:
        raise ValueError("Jenis hari tidak dikenal: " + ", ".join(invalid))
    if source["tanggal"].duplicated().any():
        raise ValueError("Konflik tanggal duplicate pada master kalender")
    source["tahun"] = pd.to_numeric(source["tahun"], errors="coerce")
    if source["tahun"].isna().any() or not source["tanggal"].dt.year.eq(source["tahun"]).all():
        raise ValueError("Nilai tahun kalender tidak sesuai dengan tanggal")

    records: list[dict[str, Any]] = []
    for row in source.itertuples(index=False):
        records.append({
            "tanggal": row.tanggal.date(),
            "jenis_hari": row.jenis_hari,
            "keterangan": "" if pd.isna(row.keterangan) else str(row.keterangan),
            "is_hari_kerja": bool(row.is_hari_kerja),
            "dasar_hukum": "" if pd.isna(row.dasar_hukum) else str(row.dasar_hukum),
            "tahun": int(row.tahun),
        })

    statement = insert(work_calendar_table).values(records)
    statement = statement.on_conflict_do_update(
        index_elements=[work_calendar_table.c.tanggal],
        set_={
            "jenis_hari": statement.excluded.jenis_hari,
            "keterangan": statement.excluded.keterangan,
            "is_hari_kerja": statement.excluded.is_hari_kerja,
            "dasar_hukum": statement.excluded.dasar_hukum,
            "tahun": statement.excluded.tahun,
            "updated_at": func.now(),
        },
    )
    with engine.begin() as connection:
        connection.execute(statement)
    return len(records)


def replace_work_calendar(engine: Engine, frame: pd.DataFrame) -> int:
    """Sinkronkan kalender per tahun dan hapus tanggal lama yang tidak lagi ada."""
    count = upsert_work_calendar(engine, frame)
    if frame.empty:
        return count
    dates = pd.to_datetime(frame["tanggal"], errors="raise").dt.date
    years = sorted(set(pd.to_numeric(frame["tahun"], errors="raise").astype(int)))
    with engine.begin() as connection:
        for year in years:
            retained = [value for value in dates if value.year == year]
            connection.execute(
                delete(work_calendar_table).where(
                    work_calendar_table.c.tahun == year,
                    work_calendar_table.c.tanggal.not_in(retained),
                )
            )
    return count


def seed_work_calendar_from_csv(engine: Engine, path: str | Path) -> int:
    """Migrasikan CSV lama ke PostgreSQL; fungsi ini hanya untuk proses seed."""
    csv_path = Path(path)
    if not csv_path.is_file() or csv_path.stat().st_size == 0:
        return 0
    source = pd.read_csv(csv_path, dtype=str).fillna("")
    required = {"tanggal", "jenis_hari", "keterangan", "dasar_hukum"}
    missing = sorted(required.difference(source.columns))
    if missing:
        raise ValueError("Kolom kalender CSV tidak tersedia: " + ", ".join(missing))
    source["tanggal"] = pd.to_datetime(source["tanggal"], errors="coerce")
    source["tahun"] = source["tanggal"].dt.year
    source["jenis_hari"] = source["jenis_hari"].str.strip().str.upper()
    source["is_hari_kerja"] = source["jenis_hari"].isin({"HARI_KERJA", "CUTI_BERSAMA"})
    return replace_work_calendar(engine, source)
