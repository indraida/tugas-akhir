"""Unit test untuk modul database/periode.py dan database/presensi.py di PostgreSQL."""

from datetime import date, time
import pandas as pd
import pytest
from sqlalchemy import create_engine

from database.opd import create_opd, init_opd_schema
from database.pegawai import create_pegawai, init_pegawai_schema
from database.periode import (
    create_periode,
    delete_periode,
    get_or_create_periode,
    get_periode_by_id,
    init_periode_schema,
    list_periode,
    update_periode,
)
from database.presensi import (
    create_presensi_manual,
    delete_presensi_record,
    init_presensi_schema,
    list_presensi_data,
    save_presensi_dataframe_to_db,
)


@pytest.fixture
def in_memory_engine():
    """Engine SQLite in-memory untuk testing schema & CRUD Periode & Presensi."""
    engine = create_engine("sqlite:///:memory:")
    init_opd_schema(engine)
    init_pegawai_schema(engine)
    init_periode_schema(engine)
    init_presensi_schema(engine)
    return engine


def test_periode_lifecycle(in_memory_engine):
    # Auto get or create
    pid1 = get_or_create_periode(in_memory_engine, 1, 2026)
    assert pid1 > 0

    # Idempotent
    pid2 = get_or_create_periode(in_memory_engine, 1, 2026)
    assert pid1 == pid2

    # Fetch
    fetched = get_periode_by_id(in_memory_engine, pid1)
    assert fetched is not None
    assert fetched["bulan"] == 1
    assert fetched["tahun"] == 2026
    assert fetched["tanggal_mulai"] == date(2026, 1, 1)
    assert fetched["tanggal_selesai"] == date(2026, 1, 31)

    # List
    periodes = list_periode(in_memory_engine)
    assert len(periodes) >= 1

    # Update
    ok, msg = update_periode(in_memory_engine, pid1, {"keterangan": "Periode Khusus Jan 2026"})
    assert ok is True
    assert get_periode_by_id(in_memory_engine, pid1)["keterangan"] == "Periode Khusus Jan 2026"

    # Delete
    del_ok, _ = delete_periode(in_memory_engine, pid1)
    assert del_ok is True
    assert get_periode_by_id(in_memory_engine, pid1) is None


def test_presensi_manual_and_relational_query(in_memory_engine):
    opd = create_opd(in_memory_engine, {"kode": "1.01", "nama": "Dinas Kesehatan"})
    peg = create_pegawai(
        in_memory_engine,
        {
            "nip": "199001012015011001",
            "nama_pegawai": "dr. Siti Rahma",
            "id_opd": opd["id"],
            "jabatan": "Dokter Pertama",
            "jenis_kelamin": "Perempuan",
            "status_pegawai": "PNS",
        },
    )

    tgl = date(2026, 3, 10)
    pres = create_presensi_manual(
        in_memory_engine,
        {
            "id_pegawai": peg["id_pegawai"],
            "tanggal_presensi": tgl,
            "jam_masuk": "07:45",
            "jam_pulang": "16:05",
            "status_presensi": "Terlambat",
            "keterlambatan_menit": 15,
            "sumber_data": "Fingerprint",
        },
    )
    assert pres["id_presensi"] is not None
    assert pres["keterlambatan_menit"] == 15

    # Query relational list
    df = list_presensi_data(in_memory_engine)
    assert len(df) == 1
    row = df.iloc[0]
    assert row["nip"] == "199001012015011001"
    assert row["nama_pegawai"] == "dr. Siti Rahma"
    assert row["nama_opd"] == "Dinas Kesehatan"
    assert row["status_presensi"] == "Terlambat"
    assert row["keterlambatan_menit"] == 15
    assert row["sumber_data"] == "Fingerprint"


def test_save_presensi_dataframe_batch_upsert(in_memory_engine):
    clean_df = pd.DataFrame([
        {
            "NIP": "198801012012011001",
            "Nama": "Budi Hartono",
            "Unit Kerja": "Dinas Pendidikan",
            "Tanggal": "2026-04-01",
            "Jam_Masuk": "07:25",
            "Jam_Pulang": "16:00",
            "Status": "Hadir",
            "Menit_Terlambat": 0,
            "Sumber_File": "rekap_april.xlsx",
        },
        {
            "NIP": "198801012012011001",
            "Nama": "Budi Hartono",
            "Unit Kerja": "Dinas Pendidikan",
            "Tanggal": "2026-04-02",
            "Jam_Masuk": "07:50",
            "Jam_Pulang": "16:00",
            "Status": "Terlambat",
            "Menit_Terlambat": 20,
            "Sumber_File": "rekap_april.xlsx",
        },
    ])

    result = save_presensi_dataframe_to_db(clean_df, in_memory_engine)
    assert result["processed"] == 2
    assert result["inserted"] == 2
    assert result["updated"] == 0

    df_loaded = list_presensi_data(in_memory_engine)
    assert len(df_loaded) == 2

    # Re-running batch update (UPSERT)
    result_re = save_presensi_dataframe_to_db(clean_df, in_memory_engine)
    assert result_re["inserted"] == 0
    assert result_re["updated"] == 2
