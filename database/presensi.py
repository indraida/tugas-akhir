"""Modul schema & operasi database PostgreSQL untuk Data Presensi Pegawai Harian."""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any

import pandas as pd
from sqlalchemy import (
    BigInteger, Column, Date, DateTime, ForeignKey, Index,
    Integer, String, Table, Time, UniqueConstraint, func, select, update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from database.opd import list_opds, opd_table
from database.pegawai import get_pegawai_by_nip, list_pegawai, pegawai_table
from database.periode import get_or_create_periode, periode_table
from database.schema import metadata

LOGGER = logging.getLogger(__name__)

presensi_table = Table(
    "presensi",
    metadata,
    Column("id_presensi", BigInteger, primary_key=True, autoincrement=True),
    Column("id_pegawai", BigInteger, ForeignKey("pegawai.id_pegawai"), nullable=False),
    Column("id_periode", BigInteger, ForeignKey("periode.id_periode"), nullable=True),
    Column("tanggal_presensi", Date, nullable=False),
    Column("jam_masuk", Time, nullable=True),
    Column("jam_pulang", Time, nullable=True),
    Column("status_presensi", String(50), nullable=True),
    Column("keterlambatan_menit", Integer, nullable=False, server_default="0"),
    Column("sumber_data", String(255), nullable=True),
    Column("waktu_insert", DateTime, nullable=False, server_default=func.now()),
    Column("waktu_update", DateTime, nullable=False, server_default=func.now()),
    UniqueConstraint("id_pegawai", "tanggal_presensi", name="uq_presensi_pegawai_tanggal"),
)

Index("idx_presensi_pegawai", presensi_table.c.id_pegawai)
Index("idx_presensi_periode_fk", presensi_table.c.id_periode)
Index("idx_presensi_tanggal_presensi", presensi_table.c.tanggal_presensi)
Index("idx_presensi_status_presensi", presensi_table.c.status_presensi)


def init_presensi_schema(engine: Engine) -> None:
    """Buat tabel presensi dan relasinya jika belum ada."""
    metadata.create_all(engine)


def _parse_time(value: Any) -> time | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, time):
        return value
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "-"}:
        return None
    parsed = pd.to_datetime(text, format="%H:%M", errors="coerce")
    if pd.isna(parsed):
        parsed = pd.to_datetime(text, errors="coerce")
    return None if pd.isna(parsed) else parsed.time()


def save_presensi_dataframe_to_db(frame: pd.DataFrame, engine: Engine) -> dict[str, Any]:
    """Simpan dan UPSERT data presensi dari DataFrame hasil cleaning ke tabel PostgreSQL presensi."""
    init_presensi_schema(engine)
    if frame.empty:
        return {"processed": 0, "inserted": 0, "updated": 0, "rejected": 0}

    # Pre-fetch pegawai lookup map by NIP
    with engine.connect() as conn:
        peg_rows = conn.execute(select(pegawai_table.c.id_pegawai, pegawai_table.c.nip)).all()
        pegawai_map: dict[str, int] = {str(r.nip).strip(): r.id_pegawai for r in peg_rows}

    # Pre-fetch OPD lookup (nama, singkatan, kode)
    opds = list_opds(engine, include_deleted=True)
    opd_lookup: dict[str, int] = {}
    for o in opds:
        if o.get("nama"):
            opd_lookup[str(o["nama"]).strip().lower()] = o["id"]
        if o.get("singkatan"):
            opd_lookup[str(o["singkatan"]).strip().lower()] = o["id"]
        if o.get("kode"):
            opd_lookup[str(o["kode"]).strip().lower()] = o["id"]

    work = frame.copy()
    dates = pd.to_datetime(work.get("Tanggal", work.get("tanggal_presensi")), errors="coerce")
    work["_parsed_date"] = dates

    # Identifikasi pegawai baru yang perlu di-register sekaligus
    new_pegawai_dict: dict[str, dict[str, Any]] = {}
    for _, row in work.iterrows():
        raw_nip = str(row.get("NIP", row.get("nip", ""))).strip().replace(" ", "").replace("-", "")
        if not raw_nip or raw_nip.lower() in {"nan", "none", ""}:
            continue
        if raw_nip not in pegawai_map and raw_nip not in new_pegawai_dict:
            raw_nama = str(row.get("Nama", row.get("nama_pegawai", raw_nip))).strip() or raw_nip
            raw_opd_name = str(row.get("Unit Kerja", row.get("OPD", ""))).strip()
            id_opd_val = opd_lookup.get(raw_opd_name.lower()) if raw_opd_name else None
            new_pegawai_dict[raw_nip] = {
                "nip": raw_nip,
                "nama_pegawai": raw_nama,
                "id_opd": id_opd_val,
                "jabatan": "Pelaksana",
                "jenis_kelamin": "Laki-laki",
                "status_pegawai": "PNS",
                "aktif": True,
            }

    if new_pegawai_dict:
        with engine.begin() as conn:
            stmt_peg = pg_insert(pegawai_table).values(list(new_pegawai_dict.values()))
            conn.execute(stmt_peg.on_conflict_do_nothing(constraint="uq_pegawai_nip"))
            peg_rows = conn.execute(select(pegawai_table.c.id_pegawai, pegawai_table.c.nip)).all()
            pegawai_map = {str(r.nip).strip(): r.id_pegawai for r in peg_rows}

    # Pre-populate Periode Cache
    periode_cache: dict[tuple[int, int], int] = {}
    unique_periods = work[["_parsed_date"]].dropna().copy()
    for _, prow in unique_periods.iterrows():
        pdate = prow["_parsed_date"]
        pyear, pmonth = int(pdate.year), int(pdate.month)
        if (pmonth, pyear) not in periode_cache:
            periode_cache[(pmonth, pyear)] = get_or_create_periode(engine, pmonth, pyear)

    records_to_upsert: list[dict[str, Any]] = []
    rejected_count = 0

    for _, row in work.iterrows():
        tgl = row["_parsed_date"]
        if pd.isna(tgl):
            rejected_count += 1
            continue

        raw_nip = str(row.get("NIP", row.get("nip", ""))).strip().replace(" ", "").replace("-", "")
        if not raw_nip or raw_nip.lower() in {"nan", "none", ""}:
            rejected_count += 1
            continue

        id_pegawai = pegawai_map.get(raw_nip)
        if id_pegawai is None:
            rejected_count += 1
            continue

        year = int(tgl.year)
        month = int(tgl.month)
        id_periode = periode_cache.get((month, year))

        jam_masuk_val = _parse_time(row.get("Jam_Masuk", row.get("jam_masuk")))
        jam_pulang_val = _parse_time(row.get("Jam_Pulang", row.get("jam_pulang")))
        status_val = str(row.get("Status", row.get("status_presensi", "Hadir"))).strip()
        late_val = int(pd.to_numeric(row.get("Menit_Terlambat", row.get("keterlambatan_menit", 0)), errors="coerce") or 0)
        sumber_val = str(row.get("Sumber_File", row.get("sumber_data", "PostgreSQL"))).strip() or "PostgreSQL"

        records_to_upsert.append({
            "id_pegawai": id_pegawai,
            "id_periode": id_periode,
            "tanggal_presensi": tgl.date(),
            "jam_masuk": jam_masuk_val,
            "jam_pulang": jam_pulang_val,
            "status_presensi": status_val,
            "keterlambatan_menit": max(0, late_val),
            "sumber_data": sumber_val,
        })

    if not records_to_upsert:
        return {"processed": len(frame), "inserted": 0, "updated": 0, "rejected": rejected_count}

    # Deduplikasi dalam batch sebelum batch insert PostgreSQL
    unique_dict: dict[tuple[int, Any], dict[str, Any]] = {}
    for r in records_to_upsert:
        unique_dict[(r["id_pegawai"], r["tanggal_presensi"])] = r
    deduped_records = list(unique_dict.values())

    # Eksekusi Batch UPSERT ke PostgreSQL dalam chunks
    chunk_size = 1000
    with engine.begin() as conn:
        for i in range(0, len(deduped_records), chunk_size):
            chunk = deduped_records[i : i + chunk_size]
            stmt = pg_insert(presensi_table).values(chunk)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_presensi_pegawai_tanggal",
                set_={
                    "id_periode": stmt.excluded.id_periode,
                    "jam_masuk": stmt.excluded.jam_masuk,
                    "jam_pulang": stmt.excluded.jam_pulang,
                    "status_presensi": stmt.excluded.status_presensi,
                    "keterlambatan_menit": stmt.excluded.keterlambatan_menit,
                    "sumber_data": stmt.excluded.sumber_data,
                    "waktu_update": func.now(),
                },
            )
            conn.execute(stmt)

    return {
        "processed": len(frame),
        "inserted": len(deduped_records),
        "updated": 0,
        "rejected": rejected_count,
    }


def list_presensi_data(
    engine: Engine,
    *,
    id_periode: int | None = None,
    id_opd: int | None = None,
    id_pegawai: int | None = None,
    status: str | None = None,
    tanggal_start: date | None = None,
    tanggal_end: date | None = None,
    limit: int = 2000,
) -> pd.DataFrame:
    """Query data presensi relasional lengkap (Join dengan pegawai, opd, periode)."""
    init_presensi_schema(engine)
    
    stmt = (
        select(
            presensi_table.c.id_presensi,
            presensi_table.c.id_pegawai,
            pegawai_table.c.nip,
            pegawai_table.c.nama_pegawai,
            pegawai_table.c.jabatan,
            pegawai_table.c.jenis_kelamin,
            pegawai_table.c.status_pegawai,
            opd_table.c.nama.label("nama_opd"),
            opd_table.c.singkatan.label("singkatan_opd"),
            presensi_table.c.id_periode,
            periode_table.c.bulan.label("periode_bulan"),
            periode_table.c.tahun.label("periode_tahun"),
            periode_table.c.keterangan.label("keterangan_periode"),
            presensi_table.c.tanggal_presensi,
            presensi_table.c.jam_masuk,
            presensi_table.c.jam_pulang,
            presensi_table.c.status_presensi,
            presensi_table.c.keterlambatan_menit,
            presensi_table.c.sumber_data,
            presensi_table.c.waktu_insert,
            presensi_table.c.waktu_update,
        )
        .select_from(
            presensi_table
            .join(pegawai_table, presensi_table.c.id_pegawai == pegawai_table.c.id_pegawai)
            .outerjoin(opd_table, pegawai_table.c.id_opd == opd_table.c.id)
            .outerjoin(periode_table, presensi_table.c.id_periode == periode_table.c.id_periode)
        )
        .order_by(presensi_table.c.tanggal_presensi.desc(), pegawai_table.c.nama_pegawai)
    )

    if id_periode is not None:
        stmt = stmt.where(presensi_table.c.id_periode == int(id_periode))
    if id_opd is not None:
        stmt = stmt.where(pegawai_table.c.id_opd == int(id_opd))
    if id_pegawai is not None:
        stmt = stmt.where(presensi_table.c.id_pegawai == int(id_pegawai))
    if status and status != "Semua Status":
        stmt = stmt.where(presensi_table.c.status_presensi == status)
    if tanggal_start is not None:
        stmt = stmt.where(presensi_table.c.tanggal_presensi >= tanggal_start)
    if tanggal_end is not None:
        stmt = stmt.where(presensi_table.c.tanggal_presensi <= tanggal_end)

    if limit > 0:
        stmt = stmt.limit(int(limit))

    with engine.connect() as conn:
        return pd.read_sql(stmt, conn)


def create_presensi_manual(engine: Engine, data: dict[str, Any]) -> dict[str, Any]:
    """Input satu record presensi manual."""
    id_pegawai = int(data["id_pegawai"])
    tgl = data["tanggal_presensi"]
    if isinstance(tgl, str):
        tgl = datetime.strptime(tgl, "%Y-%m-%d").date()

    init_presensi_schema(engine)
    id_periode = data.get("id_periode")
    if not id_periode:
        id_periode = get_or_create_periode(engine, tgl.month, tgl.year)

    jam_masuk = _parse_time(data.get("jam_masuk"))
    jam_pulang = _parse_time(data.get("jam_pulang"))
    status = str(data.get("status_presensi", "Hadir")).strip()
    late = int(data.get("keterlambatan_menit", 0) or 0)
    sumber = str(data.get("sumber_data", "Manual Input")).strip()

    values = {
        "id_pegawai": id_pegawai,
        "id_periode": int(id_periode),
        "tanggal_presensi": tgl,
        "jam_masuk": jam_masuk,
        "jam_pulang": jam_pulang,
        "status_presensi": status,
        "keterlambatan_menit": max(0, late),
        "sumber_data": sumber,
    }

    with engine.begin() as conn:
        stmt = select(presensi_table.c.id_presensi).where(
            presensi_table.c.id_pegawai == id_pegawai,
            presensi_table.c.tanggal_presensi == tgl,
        )
        existing = conn.execute(stmt).scalar_one_or_none()
        if existing is not None:
            conn.execute(
                update(presensi_table)
                .where(presensi_table.c.id_presensi == existing)
                .values(**values, waktu_update=func.now())
            )
            rec_id = existing
        else:
            res = conn.execute(presensi_table.insert().values(**values))
            rec_id = res.inserted_primary_key[0]

    return {"id_presensi": rec_id, **values}


def delete_presensi_record(engine: Engine, id_presensi: int) -> tuple[bool, str]:
    """Hapus satu record presensi."""
    init_presensi_schema(engine)
    with engine.begin() as conn:
        stmt = select(presensi_table.c.id_presensi).where(presensi_table.c.id_presensi == int(id_presensi))
        target = conn.execute(stmt).scalar_one_or_none()
        if target is None:
            return False, "Record presensi tidak ditemukan."

        conn.execute(presensi_table.delete().where(presensi_table.c.id_presensi == int(id_presensi)))
    return True, "Record presensi berhasil dihapus."
