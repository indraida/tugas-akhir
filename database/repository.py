"""Schema, validasi, UPSERT, dan query data presensi harian."""

from __future__ import annotations

from datetime import date, time
from typing import Any, Iterable

import pandas as pd
from sqlalchemy import (
    BigInteger, Column, Date, DateTime, Index, Integer, MetaData, SmallInteger,
    String, Table, Time, UniqueConstraint, func, select,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine


from database.schema import metadata

presensi_harian = Table(
    "presensi_harian",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("nip", String(30), nullable=False),
    Column("nama_pegawai", String(200)),
    Column("opd", String(255)),
    Column("tanggal", Date, nullable=False),
    Column("jam_masuk", Time),
    Column("jam_pulang", Time),
    Column("status_presensi", String(50)),
    Column("keterlambatan_menit", Integer, nullable=False, server_default="0"),
    Column("periode_bulan", SmallInteger, nullable=False),
    Column("periode_tahun", SmallInteger, nullable=False),
    Column("sumber_file", String(255)),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("updated_at", DateTime, nullable=False, server_default=func.now()),
    UniqueConstraint("nip", "tanggal", name="uq_presensi_nip_tanggal"),
)
Index("idx_presensi_nip", presensi_harian.c.nip)
Index("idx_presensi_tanggal", presensi_harian.c.tanggal)
Index("idx_presensi_opd", presensi_harian.c.opd)
Index("idx_presensi_periode", presensi_harian.c.periode_tahun, presensi_harian.c.periode_bulan)


def create_schema(engine: Engine) -> None:
    metadata.create_all(engine)
    from database.work_calendar import init_work_calendar_schema
    init_work_calendar_schema(engine)
    from database.auth import init_auth_schema
    init_auth_schema(engine)
    from database.opd import init_opd_schema
    init_opd_schema(engine)
    from database.pegawai import init_pegawai_schema
    init_pegawai_schema(engine)
    from database.periode import init_periode_schema
    init_periode_schema(engine)
    from database.presensi import init_presensi_schema
    init_presensi_schema(engine)


def _time_or_none(value: object) -> time | None:
    text = "" if value is None or pd.isna(value) else str(value).strip()
    parsed = pd.to_datetime(text, format="%H:%M", errors="coerce")
    return None if pd.isna(parsed) else parsed.time()


def _canonical_status(frame: pd.DataFrame) -> pd.Series:
    source = (
        frame["Sumber_Datang"].fillna("").astype(str)
        + "/" + frame["Sumber_Pulang"].fillna("").astype(str)
    ).str.upper()
    result = pd.Series("Hadir", index=frame.index, dtype="string")
    result.loc[frame["Status"].fillna("").astype(str).str.upper().eq("LIBUR")] = "Libur"
    result.loc[source.str.contains(r"\bWFH\b|\bWFA\b", regex=True)] = "WFH"
    result.loc[source.str.contains(r"\bDL\b", regex=True)] = "DL"
    result.loc[source.str.contains(r"\b(?:CLTN|TB|MPP)\b|CUTI", regex=True)] = "Cuti"
    result.loc[frame["TK"].fillna(False).astype(bool)] = "TK"
    result.loc[frame["Terlambat"].fillna(False).astype(bool) & result.eq("Hadir")] = "Terlambat"
    return result


def prepare_daily_records(frame: pd.DataFrame) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Map hasil cleaning existing dan laporkan setiap record yang ditolak."""
    required = {
        "NIP", "Nama", "Unit Kerja", "Tanggal", "Jam_Masuk", "Jam_Pulang",
        "Status", "Sumber_Datang", "Sumber_Pulang", "Menit_Terlambat", "TK",
        "Terlambat", "Sumber_File",
    }
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"Kolom hasil cleaning tidak lengkap: {', '.join(missing)}")

    work = frame.copy()
    work["_nip"] = work["NIP"].astype("string").str.strip()
    work["_tanggal"] = pd.to_datetime(work["Tanggal"], errors="coerce")
    work["_late"] = pd.to_numeric(work["Menit_Terlambat"], errors="coerce")
    invalid_nip = work["_nip"].isna() | work["_nip"].eq("") | work["_nip"].str.lower().eq("nan")
    invalid_date = work["_tanggal"].isna()
    invalid_late = work["_late"].isna() | work["_late"].lt(0)
    duplicate = work.duplicated(["_nip", "_tanggal"], keep=False) & ~invalid_nip & ~invalid_date
    rejected_mask = invalid_nip | invalid_date | invalid_late | duplicate
    report: dict[str, Any] = {
        "processed": len(work),
        "valid": int((~rejected_mask).sum()),
        "rejected": int(rejected_mask.sum()),
        "rejection_reasons": {
            "nip_kosong": int(invalid_nip.sum()),
            "tanggal_invalid": int(invalid_date.sum()),
            "keterlambatan_invalid": int(invalid_late.sum()),
            "duplicate_nip_tanggal": int(duplicate.sum()),
        },
    }
    valid = work.loc[~rejected_mask].copy()
    statuses = _canonical_status(valid)
    records: list[dict[str, Any]] = []
    for index, row in valid.iterrows():
        timestamp = row["_tanggal"]
        records.append({
            "nip": str(row["_nip"]),
            "nama_pegawai": None if pd.isna(row["Nama"]) else str(row["Nama"]).strip() or None,
            "opd": None if pd.isna(row["Unit Kerja"]) else str(row["Unit Kerja"]).strip() or None,
            "tanggal": timestamp.date(),
            "jam_masuk": _time_or_none(row["Jam_Masuk"]),
            "jam_pulang": _time_or_none(row["Jam_Pulang"]),
            "status_presensi": str(statuses.loc[index]),
            "keterlambatan_menit": int(row["_late"]),
            "periode_bulan": int(timestamp.month),
            "periode_tahun": int(timestamp.year),
            "sumber_file": None if pd.isna(row["Sumber_File"]) else str(row["Sumber_File"]).strip() or None,
        })
    return records, report


def save_daily_attendance_to_db(frame: pd.DataFrame, engine: Engine) -> dict[str, Any]:
    """UPSERT satu batch dalam transaction dan kembalikan statistik terukur."""
    records, report = prepare_daily_records(frame)
    report.update({"inserted": 0, "updated": 0})
    if not records:
        return report
    create_schema(engine)
    keys = [(record["nip"], record["tanggal"]) for record in records]
    with engine.begin() as connection:
        existing = set(connection.execute(
            select(presensi_harian.c.nip, presensi_harian.c.tanggal).where(
                presensi_harian.c.nip.in_({key[0] for key in keys}),
                presensi_harian.c.tanggal.in_({key[1] for key in keys}),
            )
        ).tuples())
        for offset in range(0, len(records), 1000):
            statement = insert(presensi_harian).values(records[offset : offset + 1000])
            update_columns = {
                name: getattr(statement.excluded, name)
                for name in ["nama_pegawai", "opd", "jam_masuk", "jam_pulang", "status_presensi", "keterlambatan_menit", "periode_bulan", "periode_tahun", "sumber_file"]
            }
            update_columns["updated_at"] = func.now()
            connection.execute(statement.on_conflict_do_update(
                constraint="uq_presensi_nip_tanggal", set_=update_columns
            ))
    report["updated"] = sum(key in existing for key in keys)
    report["inserted"] = len(keys) - report["updated"]
    return report


def find_existing_attendance_keys(engine: Engine, keys: Iterable[tuple[str, date]]) -> set[tuple[str, date]]:
    """Cari business key NIP + tanggal yang sudah ada, tanpa mutasi database."""
    normalized = {(str(nip).strip(), attendance_date) for nip, attendance_date in keys}
    if not normalized:
        return set()
    nips = {key[0] for key in normalized}
    dates = {key[1] for key in normalized}
    with engine.connect() as connection:
        rows = connection.execute(
            select(presensi_harian.c.nip, presensi_harian.c.tanggal).where(
                presensi_harian.c.nip.in_(nips), presensi_harian.c.tanggal.in_(dates)
            )
        ).tuples()
        return {tuple(row) for row in rows if tuple(row) in normalized}


def list_available_attendance_periods(engine: Engine) -> list[tuple[int, int]]:
    """Daftar (tahun, bulan) aktual dari tabel sumber dashboard."""
    with engine.connect() as connection:
        rows = connection.execute(
            select(presensi_harian.c.periode_tahun, presensi_harian.c.periode_bulan)
            .distinct()
            .order_by(presensi_harian.c.periode_tahun.desc(), presensi_harian.c.periode_bulan.desc())
        ).all()
    return [(int(row[0]), int(row[1])) for row in rows]


def load_attendance_from_db(
    engine: Engine, year: int | None = None, month: int | None = None,
    opd: str | None = None,
) -> pd.DataFrame:
    create_schema(engine)
    statement = select(presensi_harian)
    if year is not None:
        statement = statement.where(presensi_harian.c.periode_tahun == int(year))
    if month is not None:
        statement = statement.where(presensi_harian.c.periode_bulan == int(month))
    if opd is not None and opd != "Semua OPD":
        statement = statement.where(presensi_harian.c.opd == opd)
    with engine.connect() as connection:
        df = pd.read_sql(statement, connection)
    
    if not df.empty:
        return df

    # Jika tabel presensi_harian kosong, ambil dari tabel relasional presensi
    try:
        from database.presensi import list_presensi_data
        rel_df = list_presensi_data(engine, limit=100000)
        if not rel_df.empty:
            mapped = pd.DataFrame({
                "id": rel_df["id_presensi"],
                "nip": rel_df["nip"],
                "nama_pegawai": rel_df["nama_pegawai"],
                "opd": rel_df["nama_opd"].fillna(rel_df["singkatan_opd"]),
                "tanggal": rel_df["tanggal_presensi"],
                "jam_masuk": rel_df["jam_masuk"],
                "jam_pulang": rel_df["jam_pulang"],
                "status_presensi": rel_df["status_presensi"],
                "keterlambatan_menit": rel_df["keterlambatan_menit"],
                "periode_bulan": rel_df["periode_bulan"],
                "periode_tahun": rel_df["periode_tahun"],
                "sumber_file": rel_df["sumber_data"],
                "created_at": rel_df["waktu_insert"],
                "updated_at": rel_df["waktu_update"],
            })
            if year is not None:
                mapped = mapped[mapped["periode_tahun"] == int(year)]
            if month is not None:
                mapped = mapped[mapped["periode_bulan"] == int(month)]
            if opd is not None and opd != "Semua OPD":
                mapped = mapped[mapped["opd"] == opd]
            return mapped
    except Exception:
        pass

    return df


