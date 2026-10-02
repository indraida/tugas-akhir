"""Unit test untuk modul database/pegawai.py (Master Pegawai & Excel Import)."""

import io
from datetime import datetime
import pandas as pd
import pytest

from database.opd import create_opd, init_opd_schema
from database.pegawai import (
    create_pegawai,
    delete_pegawai,
    generate_pegawai_excel_template,
    get_pegawai_by_id,
    get_pegawai_by_nip,
    import_pegawai_from_excel,
    init_pegawai_schema,
    list_pegawai,
    restore_pegawai,
    seed_default_pegawai,
    toggle_pegawai_status,
    update_pegawai,
)


@pytest.fixture
def in_memory_engine(postgres_engine):
    """Engine PostgreSQL dengan schema sementara untuk testing schema & CRUD Pegawai."""
    engine = postgres_engine
    init_opd_schema(engine)
    init_pegawai_schema(engine)
    return engine


def test_seed_default_pegawai(in_memory_engine):
    created = seed_default_pegawai(in_memory_engine)
    assert len(created) >= 3
    
    # Idempotent: running seed again should not duplicate
    created_again = seed_default_pegawai(in_memory_engine)
    assert created_again == []

    pegawai_list = list_pegawai(in_memory_engine)
    assert len(pegawai_list) >= 3
    nips = {p["nip"] for p in pegawai_list}
    assert "198501152010011012" in nips


def test_create_and_get_pegawai_with_all_columns(in_memory_engine):
    opd = create_opd(in_memory_engine, {"kode": "1.01", "nama": "Dinas Kominfo"})
    
    payload = {
        "nip": "199001012015011001",
        "nama_pegawai": "Fajar Ramadhan, S.T",
        "id_opd": opd["id"],
        "jabatan": "Pranata Komputer",
        "jenis_kelamin": "Laki-laki",
        "status_pegawai": "PNS",
        "aktif": True,
    }
    created = create_pegawai(in_memory_engine, payload)
    assert created["id_pegawai"] is not None
    assert created["nip"] == "199001012015011001"
    assert created["nama_pegawai"] == "Fajar Ramadhan, S.T"
    assert created["id_opd"] == opd["id"]
    assert created["nama_opd"] == "Dinas Kominfo"
    assert created["jabatan"] == "Pranata Komputer"
    assert created["jenis_kelamin"] == "Laki-laki"
    assert created["status_pegawai"] == "PNS"
    assert created["aktif"] is True

    # Get by ID
    fetched_by_id = get_pegawai_by_id(in_memory_engine, created["id_pegawai"])
    assert fetched_by_id is not None
    assert fetched_by_id["nip"] == "199001012015011001"

    # Get by NIP
    fetched_by_nip = get_pegawai_by_nip(in_memory_engine, "199001012015011001")
    assert fetched_by_nip is not None
    assert fetched_by_nip["id_pegawai"] == created["id_pegawai"]


def test_duplicate_nip_raises_error(in_memory_engine):
    create_pegawai(in_memory_engine, {"nip": "199102022016021002", "nama_pegawai": "Pegawai A"})
    with pytest.raises(ValueError, match="sudah terdaftar"):
        create_pegawai(in_memory_engine, {"nip": "199102022016021002", "nama_pegawai": "Pegawai B"})


def test_update_pegawai(in_memory_engine):
    pegawai = create_pegawai(in_memory_engine, {"nip": "199303032017031003", "nama_pegawai": "Pegawai Lama"})
    peg_id = pegawai["id_pegawai"]

    success, msg = update_pegawai(
        in_memory_engine,
        peg_id,
        {
            "nip": "199303032017031003",
            "nama_pegawai": "Pegawai Baru, S.Sos",
            "jabatan": "Kepala Seksi",
            "jenis_kelamin": "Perempuan",
            "status_pegawai": "PNS",
            "aktif": True,
        },
    )
    assert success is True

    updated = get_pegawai_by_id(in_memory_engine, peg_id)
    assert updated["nama_pegawai"] == "Pegawai Baru, S.Sos"
    assert updated["jabatan"] == "Kepala Seksi"
    assert updated["jenis_kelamin"] == "Perempuan"


def test_soft_delete_and_restore_pegawai(in_memory_engine):
    pegawai = create_pegawai(in_memory_engine, {"nip": "199404042018041004", "nama_pegawai": "Pegawai Cuti"})
    peg_id = pegawai["id_pegawai"]

    # Soft delete
    del_ok, msg = delete_pegawai(in_memory_engine, peg_id, hard_delete=False)
    assert del_ok is True
    assert "sampah" in msg.lower()

    # list default excludes deleted
    active_list = list_pegawai(in_memory_engine, include_deleted=False)
    assert all(p["id_pegawai"] != peg_id for p in active_list)

    # Restore
    rest_ok, _ = restore_pegawai(in_memory_engine, peg_id)
    assert rest_ok is True
    assert any(p["id_pegawai"] == peg_id for p in list_pegawai(in_memory_engine, include_deleted=False))


def test_import_pegawai_from_excel_dataframe(in_memory_engine):
    opd = create_opd(in_memory_engine, {"kode": "1.02", "nama": "Dinas Pendidikan"})
    
    import_df = pd.DataFrame([
        {
            "NIP": "199505052019051005",
            "Nama Pegawai": "Guru Pertama, S.Pd",
            "Nama OPD": "Dinas Pendidikan",
            "Jabatan": "Guru Ahli Pertama",
            "Jenis Kelamin": "Laki-laki",
            "Status Pegawai": "PNS",
            "Status Aktif": "Aktif",
        },
        {
            "NIP": "199606062020062006",
            "Nama Pegawai": "Guru Kedua, S.Pd",
            "Nama OPD": "Dinas Pendidikan",
            "Jabatan": "Guru Kelas",
            "Jenis Kelamin": "Perempuan",
            "Status Pegawai": "PPPK",
            "Status Aktif": "Aktif",
        },
    ])

    result = import_pegawai_from_excel(in_memory_engine, import_df)
    assert result["total_rows"] == 2
    assert result["inserted"] == 2
    assert result["updated"] == 0
    assert result["failed"] == 0

    p1 = get_pegawai_by_nip(in_memory_engine, "199505052019051005")
    assert p1 is not None
    assert p1["nama_pegawai"] == "Guru Pertama, S.Pd"
    assert p1["id_opd"] == opd["id"]

    # Re-importing same dataframe should perform UPSERT (updated=2, inserted=0)
    result_update = import_pegawai_from_excel(in_memory_engine, import_df)
    assert result_update["inserted"] == 0
    assert result_update["updated"] == 2


def test_generate_pegawai_excel_template():
    template_bytes = generate_pegawai_excel_template()
    assert len(template_bytes) > 0
    df = pd.read_excel(io.BytesIO(template_bytes))
    assert "NIP" in df.columns
    assert "Nama Pegawai" in df.columns
