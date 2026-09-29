"""Unit test untuk modul database/opd.py (Master OPD / Dinas PostgreSQL/SQLAlchemy)."""

from datetime import datetime
import pytest
from sqlalchemy import create_engine

from database.opd import (
    create_opd,
    delete_opd,
    get_opd_by_id,
    get_opd_by_kode,
    init_opd_schema,
    list_opds,
    restore_opd,
    seed_default_opds,
    toggle_opd_status,
    update_opd,
)


@pytest.fixture
def in_memory_engine():
    """Engine SQLite in-memory untuk testing schema & CRUD OPD secara isolated."""
    engine = create_engine("sqlite:///:memory:")
    init_opd_schema(engine)
    return engine


def test_seed_default_opds(in_memory_engine):
    created = seed_default_opds(in_memory_engine)
    assert len(created) >= 5
    
    # Idempotent: running seed again should not duplicate
    created_again = seed_default_opds(in_memory_engine)
    assert created_again == []

    opds = list_opds(in_memory_engine)
    assert len(opds) >= 5
    names = {o["nama"] for o in opds}
    assert "Dinas Pendidikan" in names
    assert "Dinas Kesehatan" in names


def test_create_and_get_opd_with_all_columns(in_memory_engine):
    payload = {
        "kode": "2.01.01",
        "kode_sipd": "2.01.01.01",
        "nama": "Dinas Lingkungan Hidup",
        "singkatan": "DLH",
        "jenis": "DINAS",
        "parent_id": None,
        "alamat": "Jl. Asri No. 12",
        "telepon": "(021) 555-0999",
        "email": "dlh@pemda.go.id",
        "website": "https://dlh.pemda.go.id",
        "kepala_nama": "Ir. Bambang Wijaya, M.Env",
        "kepala_nip": "197505052000031002",
        "aktif": True,
    }
    created = create_opd(in_memory_engine, payload)
    assert created["id"] is not None
    assert created["kode"] == "2.01.01"
    assert created["kode_sipd"] == "2.01.01.01"
    assert created["nama"] == "Dinas Lingkungan Hidup"
    assert created["singkatan"] == "DLH"
    assert created["jenis"] == "DINAS"
    assert created["alamat"] == "Jl. Asri No. 12"
    assert created["telepon"] == "(021) 555-0999"
    assert created["email"] == "dlh@pemda.go.id"
    assert created["website"] == "https://dlh.pemda.go.id"
    assert created["kepala_nama"] == "Ir. Bambang Wijaya, M.Env"
    assert created["kepala_nip"] == "197505052000031002"
    assert created["aktif"] is True
    assert created["deleted_at"] is None

    # Get by ID
    fetched_by_id = get_opd_by_id(in_memory_engine, created["id"])
    assert fetched_by_id is not None
    assert fetched_by_id["nama"] == "Dinas Lingkungan Hidup"

    # Get by Kode
    fetched_by_kode = get_opd_by_kode(in_memory_engine, "2.01.01")
    assert fetched_by_kode is not None
    assert fetched_by_kode["id"] == created["id"]


def test_create_duplicate_kode_raises_error(in_memory_engine):
    create_opd(in_memory_engine, {"kode": "3.01.01", "nama": "Dinas A"})
    with pytest.raises(ValueError, match="sudah digunakan"):
        create_opd(in_memory_engine, {"kode": "3.01.01", "nama": "Dinas B"})


def test_update_opd(in_memory_engine):
    opd = create_opd(in_memory_engine, {"kode": "4.01.01", "nama": "Dinas PU Awal"})
    opd_id = opd["id"]

    success, msg = update_opd(
        in_memory_engine,
        opd_id,
        {
            "kode": "4.01.01",
            "nama": "Dinas Pekerjaan Umum dan Penataan Ruang",
            "singkatan": "PUPR",
            "jenis": "DINAS",
            "kepala_nama": "H. Surya, S.T",
            "aktif": True,
        },
    )
    assert success is True

    updated = get_opd_by_id(in_memory_engine, opd_id)
    assert updated["nama"] == "Dinas Pekerjaan Umum dan Penataan Ruang"
    assert updated["singkatan"] == "PUPR"


def test_soft_delete_and_restore_opd(in_memory_engine):
    opd = create_opd(in_memory_engine, {"kode": "5.01.01", "nama": "Dinas Sementara"})
    opd_id = opd["id"]

    # Soft delete
    del_ok, msg = delete_opd(in_memory_engine, opd_id, hard_delete=False)
    assert del_ok is True
    assert "sampah" in msg.lower()

    # list_opds default should not include deleted
    active_list = list_opds(in_memory_engine, include_deleted=False)
    assert all(o["id"] != opd_id for o in active_list)

    # list_opds with include_deleted=True should have it
    all_list = list_opds(in_memory_engine, include_deleted=True)
    deleted_item = next(o for o in all_list if o["id"] == opd_id)
    assert deleted_item["deleted_at"] is not None
    assert deleted_item["aktif"] is False

    # Restore
    rest_ok, rmsg = restore_opd(in_memory_engine, opd_id)
    assert rest_ok is True

    restored_list = list_opds(in_memory_engine, include_deleted=False)
    assert any(o["id"] == opd_id for o in restored_list)


def test_toggle_opd_status(in_memory_engine):
    opd = create_opd(in_memory_engine, {"kode": "6.01.01", "nama": "Kantor Arsip", "aktif": True})
    opd_id = opd["id"]

    toggle_opd_status(in_memory_engine, opd_id, False)
    assert get_opd_by_id(in_memory_engine, opd_id)["aktif"] is False

    toggle_opd_status(in_memory_engine, opd_id, True)
    assert get_opd_by_id(in_memory_engine, opd_id)["aktif"] is True
