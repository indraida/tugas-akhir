"""Modul schema & operasi database PostgreSQL untuk Master Organisasi Perangkat Daerah (OPD / Dinas)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey, Index,
    MetaData, String, Table, Text, UniqueConstraint, func, select, update,
)
from sqlalchemy.engine import Engine

from database.schema import metadata

LOGGER = logging.getLogger(__name__)

opd_table = Table(
    "opd",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("kode", String(50), nullable=True, unique=True),
    Column("kode_sipd", String(50), nullable=True),
    Column("nama", String(255), nullable=False),
    Column("singkatan", String(50), nullable=True),
    Column("jenis", String(50), nullable=False, server_default="DINAS"),
    Column("parent_id", BigInteger, ForeignKey("opd.id"), nullable=True),
    Column("alamat", Text, nullable=True),
    Column("telepon", String(50), nullable=True),
    Column("email", String(100), nullable=True),
    Column("website", String(150), nullable=True),
    Column("kepala_nama", String(200), nullable=True),
    Column("kepala_nip", String(30), nullable=True),
    Column("aktif", Boolean, nullable=False, server_default="true"),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("updated_at", DateTime, nullable=False, server_default=func.now()),
    Column("deleted_at", DateTime, nullable=True),
    UniqueConstraint("kode", name="uq_opd_kode"),
)

Index("idx_opd_kode", opd_table.c.kode)
Index("idx_opd_nama", opd_table.c.nama)
Index("idx_opd_jenis", opd_table.c.jenis)
Index("idx_opd_aktif", opd_table.c.aktif)
Index("idx_opd_deleted_at", opd_table.c.deleted_at)


OPD_JENIS_CHOICES = [
    "DINAS",
    "BADAN",
    "SEKRETARIAT",
    "INSPEKTORAT",
    "KECAMATAN",
    "RSUD",
    "BAGIAN",
    "UPTD",
    "LAINNYA",
]


DEFAULT_SEEDED_OPDS = [
    {
        "kode": "1.01.01",
        "kode_sipd": "1.01.01.01",
        "nama": "Badan Kepegawaian dan Pengembangan Sumber Daya Manusia",
        "singkatan": "BKPSDM",
        "jenis": "BADAN",
        "alamat": "Jl. Pemuda No. 1",
        "telepon": "(021) 555-0101",
        "email": "bkpsdm@pemda.go.id",
        "website": "https://bkpsdm.pemda.go.id",
        "kepala_nama": "Dr. H. Ahmad Fauzi, M.Si",
        "kepala_nip": "197501012000031001",
        "aktif": True,
    },
    {
        "kode": "1.02.01",
        "kode_sipd": "1.02.01.01",
        "nama": "Dinas Pendidikan",
        "singkatan": "DISDIK",
        "jenis": "DINAS",
        "alamat": "Jl. Pendidikan No. 45",
        "telepon": "(021) 555-0102",
        "email": "disdik@pemda.go.id",
        "website": "https://disdik.pemda.go.id",
        "kepala_nama": "Hj. Nuraini, S.Pd., M.Pd",
        "kepala_nip": "197805122002122003",
        "aktif": True,
    },
    {
        "kode": "1.03.01",
        "kode_sipd": "1.03.01.01",
        "nama": "Dinas Kesehatan",
        "singkatan": "DINKES",
        "jenis": "DINAS",
        "alamat": "Jl. Kesehatan No. 10",
        "telepon": "(021) 555-0103",
        "email": "dinkes@pemda.go.id",
        "website": "https://dinkes.pemda.go.id",
        "kepala_nama": "dr. Hendra Setiawan, Sp.A",
        "kepala_nip": "198003152006041005",
        "aktif": True,
    },
    {
        "kode": "1.04.01",
        "kode_sipd": "1.04.01.01",
        "nama": "Inspektorat Daerah",
        "singkatan": "INSPEKTORAT",
        "jenis": "INSPEKTORAT",
        "alamat": "Jl. Merdeka No. 3",
        "telepon": "(021) 555-0104",
        "email": "inspektorat@pemda.go.id",
        "website": "https://inspektorat.pemda.go.id",
        "kepala_nama": "Drs. Bambang Sudarsono, M.M",
        "kepala_nip": "197208201998031002",
        "aktif": True,
    },
    {
        "kode": "1.05.01",
        "kode_sipd": "1.05.01.01",
        "nama": "Sekretariat Daerah",
        "singkatan": "SETDA",
        "jenis": "SEKRETARIAT",
        "alamat": "Jl. Pahlawan No. 1",
        "telepon": "(021) 555-0105",
        "email": "setda@pemda.go.id",
        "website": "https://setda.pemda.go.id",
        "kepala_nama": "Ir. Joko Pramono, M.T",
        "kepala_nip": "197004101995031004",
        "aktif": True,
    },
    {
        "kode": "1.06.01",
        "kode_sipd": "1.06.01.01",
        "nama": "Dinas Komunikasi dan Informatika",
        "singkatan": "DISKOMINFO",
        "jenis": "DINAS",
        "alamat": "Jl. Teknologi No. 8",
        "telepon": "(021) 555-0106",
        "email": "diskominfo@pemda.go.id",
        "website": "https://diskominfo.pemda.go.id",
        "kepala_nama": "Rian Anggoro, S.Kom., M.Cs",
        "kepala_nip": "198309142008011009",
        "aktif": True,
    },
    {
        "kode": "1.07.01",
        "kode_sipd": "1.07.01.01",
        "nama": "Badan Perencanaan Pembangunan Daerah",
        "singkatan": "BAPPEDA",
        "jenis": "BADAN",
        "alamat": "Jl. Perencanaan No. 12",
        "telepon": "(021) 555-0107",
        "email": "bappeda@pemda.go.id",
        "website": "https://bappeda.pemda.go.id",
        "kepala_nama": "Dr. Ir. Wahyu Hidayat, M.Si",
        "kepala_nip": "197406181999031003",
        "aktif": True,
    },
    {
        "kode": "1.08.01",
        "kode_sipd": "1.08.01.01",
        "nama": "Badan Keuangan dan Aset Daerah",
        "singkatan": "BKAD",
        "jenis": "BADAN",
        "alamat": "Jl. Keuangan No. 5",
        "telepon": "(021) 555-0108",
        "email": "bkad@pemda.go.id",
        "website": "https://bkad.pemda.go.id",
        "kepala_nama": "Dra. Sri Wahyuni, M.Ak",
        "kepala_nip": "197602102001122001",
        "aktif": True,
    },
    {
        "kode": "1.09.01",
        "kode_sipd": "1.09.01.01",
        "nama": "Badan Kepegawaian Daerah",
        "singkatan": "BKD",
        "jenis": "BADAN",
        "alamat": "Jl. Pegawai No. 2",
        "telepon": "(021) 555-0109",
        "email": "bkd@pemda.go.id",
        "website": "https://bkd.pemda.go.id",
        "kepala_nama": "H. Mulyadi, S.Sos., M.Si",
        "kepala_nip": "197311251997031002",
        "aktif": True,
    },
    {
        "kode": "1.10.01",
        "kode_sipd": "1.10.01.01",
        "nama": "Biro Hukum",
        "singkatan": "ROHUKUM",
        "jenis": "BIRO",
        "alamat": "Jl. Pahlawan No. 1 Gedung B",
        "telepon": "(021) 555-0110",
        "email": "hukum@pemda.go.id",
        "website": "https://jdih.pemda.go.id",
        "kepala_nama": "Agus Salim, S.H., M.H",
        "kepala_nip": "198104152005011007",
        "aktif": True,
    },
    {
        "kode": "1.11.01",
        "kode_sipd": "1.11.01.01",
        "nama": "Biro Organisasi",
        "singkatan": "ROORGANISASI",
        "jenis": "BIRO",
        "alamat": "Jl. Pahlawan No. 1 Gedung C",
        "telepon": "(021) 555-0111",
        "email": "organisasi@pemda.go.id",
        "website": "https://organisasi.pemda.go.id",
        "kepala_nama": "Dra. Ratna Juwita, M.Si",
        "kepala_nip": "197908082003122004",
        "aktif": True,
    },
]


def init_opd_schema(engine: Engine) -> None:
    """Buat tabel opd jika belum ada."""
    metadata.create_all(engine)


def seed_default_opds(engine: Engine) -> list[str]:
    """Inisialisasi data OPD default jika tabel masih kosong."""
    init_opd_schema(engine)
    created: list[str] = []
    with engine.begin() as conn:
        for item in DEFAULT_SEEDED_OPDS:
            kode = item.get("kode")
            stmt = select(opd_table.c.id).where(
                (opd_table.c.kode == kode) | (func.lower(opd_table.c.nama) == item["nama"].strip().lower())
            )
            existing = conn.execute(stmt).scalar_one_or_none()
            if existing is None:
                conn.execute(
                    opd_table.insert().values(
                        kode=item.get("kode"),
                        kode_sipd=item.get("kode_sipd"),
                        nama=item["nama"].strip(),
                        singkatan=item.get("singkatan"),
                        jenis=item.get("jenis", "DINAS"),
                        parent_id=item.get("parent_id"),
                        alamat=item.get("alamat"),
                        telepon=item.get("telepon"),
                        email=item.get("email"),
                        website=item.get("website"),
                        kepala_nama=item.get("kepala_nama"),
                        kepala_nip=item.get("kepala_nip"),
                        aktif=item.get("aktif", True),
                    )
                )
                created.append(item["nama"])
    return created


def list_opds(
    engine: Engine,
    include_deleted: bool = False,
    aktif_only: bool = False,
) -> list[dict[str, Any]]:
    """Ambil daftar OPD dari PostgreSQL."""
    init_opd_schema(engine)
    with engine.connect() as conn:
        stmt = select(opd_table).order_by(opd_table.c.kode.nulls_last(), opd_table.c.nama)
        if not include_deleted:
            stmt = stmt.where(opd_table.c.deleted_at.is_(None))
        if aktif_only:
            stmt = stmt.where(opd_table.c.aktif.is_(True))
            
        rows = conn.execute(stmt).mappings().all()
        
        # Check jika kosong dan belum di-seed
        if not rows and not include_deleted:
            count_stmt = select(func.count(opd_table.c.id))
            if (conn.execute(count_stmt).scalar() or 0) == 0:
                seed_default_opds(engine)
                rows = conn.execute(stmt).mappings().all()

        parent_map: dict[int, str] = {r["id"]: r["nama"] for r in rows}

        results = []
        for r in rows:
            p_id = r["parent_id"]
            p_nama = parent_map.get(p_id) if p_id else None
            results.append({
                "id": r["id"],
                "kode": r["kode"] or "",
                "kode_sipd": r["kode_sipd"] or "",
                "nama": r["nama"],
                "singkatan": r["singkatan"] or "",
                "jenis": r["jenis"] or "DINAS",
                "parent_id": p_id,
                "parent_nama": p_nama or "-",
                "alamat": r["alamat"] or "",
                "telepon": r["telepon"] or "",
                "email": r["email"] or "",
                "website": r["website"] or "",
                "kepala_nama": r["kepala_nama"] or "",
                "kepala_nip": r["kepala_nip"] or "",
                "aktif": bool(r["aktif"]),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "deleted_at": r["deleted_at"],
            })
        return results


def get_opd_by_id(engine: Engine, opd_id: int) -> dict[str, Any] | None:
    """Ambil satu data OPD berdasarkan ID."""
    if not opd_id:
        return None
    init_opd_schema(engine)
    with engine.connect() as conn:
        stmt = select(opd_table).where(opd_table.c.id == int(opd_id))
        row = conn.execute(stmt).mappings().one_or_none()
        if row is None:
            return None
            
        parent_nama = None
        if row["parent_id"]:
            pstmt = select(opd_table.c.nama).where(opd_table.c.id == row["parent_id"])
            parent_nama = conn.execute(pstmt).scalar()
            
        return {
            "id": row["id"],
            "kode": row["kode"] or "",
            "kode_sipd": row["kode_sipd"] or "",
            "nama": row["nama"],
            "singkatan": row["singkatan"] or "",
            "jenis": row["jenis"] or "DINAS",
            "parent_id": row["parent_id"],
            "parent_nama": parent_nama or "-",
            "alamat": row["alamat"] or "",
            "telepon": row["telepon"] or "",
            "email": row["email"] or "",
            "website": row["website"] or "",
            "kepala_nama": row["kepala_nama"] or "",
            "kepala_nip": row["kepala_nip"] or "",
            "aktif": bool(row["aktif"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "deleted_at": row["deleted_at"],
        }


def get_opd_by_kode(engine: Engine, kode: str) -> dict[str, Any] | None:
    """Ambil satu data OPD berdasarkan Kode unik."""
    if not kode:
        return None
    init_opd_schema(engine)
    with engine.connect() as conn:
        stmt = select(opd_table).where(opd_table.c.kode == kode.strip())
        row = conn.execute(stmt).mappings().one_or_none()
        if row is None:
            return None
        return get_opd_by_id(engine, row["id"])


def create_opd(engine: Engine, data: dict[str, Any]) -> dict[str, Any]:
    """Tambah OPD baru ke database PostgreSQL."""
    nama = (data.get("nama") or "").strip()
    if not nama:
        raise ValueError("Nama OPD / Dinas wajib diisi.")
        
    kode = (data.get("kode") or "").strip() or None
    init_opd_schema(engine)
    
    with engine.begin() as conn:
        if kode:
            check_stmt = select(opd_table.c.id).where(opd_table.c.kode == kode)
            if conn.execute(check_stmt).scalar_one_or_none() is not None:
                raise ValueError(f"Kode OPD '{kode}' sudah digunakan.")
                
        parent_id = data.get("parent_id")
        if parent_id in ("", None, "None", 0, "-"):
            parent_id = None
        else:
            parent_id = int(parent_id)

        values = {
            "kode": kode,
            "kode_sipd": (data.get("kode_sipd") or "").strip() or None,
            "nama": nama,
            "singkatan": (data.get("singkatan") or "").strip() or None,
            "jenis": (data.get("jenis") or "DINAS").strip().upper(),
            "parent_id": parent_id,
            "alamat": (data.get("alamat") or "").strip() or None,
            "telepon": (data.get("telepon") or "").strip() or None,
            "email": (data.get("email") or "").strip() or None,
            "website": (data.get("website") or "").strip() or None,
            "kepala_nama": (data.get("kepala_nama") or "").strip() or None,
            "kepala_nip": (data.get("kepala_nip") or "").strip() or None,
            "aktif": bool(data.get("aktif", True)),
        }
        result = conn.execute(opd_table.insert().values(**values))
        opd_id = result.inserted_primary_key[0]
        
    return get_opd_by_id(engine, opd_id) or {"id": opd_id, **values}


def update_opd(engine: Engine, opd_id: int, data: dict[str, Any]) -> tuple[bool, str]:
    """Perbarui informasi OPD di database PostgreSQL."""
    if not opd_id:
        return False, "ID OPD tidak valid."
        
    nama = (data.get("nama") or "").strip()
    if not nama:
        return False, "Nama OPD wajib diisi."
        
    kode = (data.get("kode") or "").strip() or None
    init_opd_schema(engine)
    
    with engine.begin() as conn:
        stmt = select(opd_table).where(opd_table.c.id == int(opd_id))
        target = conn.execute(stmt).mappings().one_or_none()
        if target is None:
            return False, "Data OPD tidak ditemukan."

        if kode:
            check_stmt = select(opd_table.c.id).where(
                opd_table.c.kode == kode,
                opd_table.c.id != int(opd_id),
            )
            if conn.execute(check_stmt).scalar_one_or_none() is not None:
                return False, f"Kode OPD '{kode}' sudah digunakan oleh OPD lain."

        parent_id = data.get("parent_id")
        if parent_id in ("", None, "None", 0, "-"):
            parent_id = None
        else:
            parent_id = int(parent_id)
            if parent_id == int(opd_id):
                return False, "OPD tidak dapat menjadi induk bagi dirinya sendiri."

        values = {
            "kode": kode,
            "kode_sipd": (data.get("kode_sipd") or "").strip() or None,
            "nama": nama,
            "singkatan": (data.get("singkatan") or "").strip() or None,
            "jenis": (data.get("jenis") or "DINAS").strip().upper(),
            "parent_id": parent_id,
            "alamat": (data.get("alamat") or "").strip() or None,
            "telepon": (data.get("telepon") or "").strip() or None,
            "email": (data.get("email") or "").strip() or None,
            "website": (data.get("website") or "").strip() or None,
            "kepala_nama": (data.get("kepala_nama") or "").strip() or None,
            "kepala_nip": (data.get("kepala_nip") or "").strip() or None,
            "aktif": bool(data.get("aktif", True)),
            "updated_at": func.now(),
        }
        conn.execute(
            update(opd_table)
            .where(opd_table.c.id == int(opd_id))
            .values(**values)
        )
    return True, f"Data OPD '{nama}' berhasil diperbarui."


def delete_opd(engine: Engine, opd_id: int, hard_delete: bool = False) -> tuple[bool, str]:
    """Hapus OPD dari database (default soft-delete dengan deleted_at)."""
    init_opd_schema(engine)
    with engine.begin() as conn:
        stmt = select(opd_table).where(opd_table.c.id == int(opd_id))
        target = conn.execute(stmt).mappings().one_or_none()
        if target is None:
            return False, "Data OPD tidak ditemukan."

        # Cek apakah ada child OPD
        child_stmt = select(func.count(opd_table.c.id)).where(
            opd_table.c.parent_id == int(opd_id),
            opd_table.c.deleted_at.is_(None),
        )
        child_count = conn.execute(child_stmt).scalar() or 0
        if child_count > 0:
            return False, f"Tidak dapat menghapus OPD ini karena memiliki {child_count} sub-unit/OPD turunan aktif."

        if hard_delete:
            conn.execute(opd_table.delete().where(opd_table.c.id == int(opd_id)))
            msg = f"OPD '{target['nama']}' berhasil dihapus permanen dari database."
        else:
            conn.execute(
                update(opd_table)
                .where(opd_table.c.id == int(opd_id))
                .values(deleted_at=func.now(), aktif=False, updated_at=func.now())
            )
            msg = f"OPD '{target['nama']}' berhasil dinonaktifkan dan dipindahkan ke sampah (soft delete)."
            
    return True, msg


def restore_opd(engine: Engine, opd_id: int) -> tuple[bool, str]:
    """Pulihkan OPD yang sebelumnya di-soft delete."""
    init_opd_schema(engine)
    with engine.begin() as conn:
        stmt = select(opd_table).where(opd_table.c.id == int(opd_id))
        target = conn.execute(stmt).mappings().one_or_none()
        if target is None:
            return False, "Data OPD tidak ditemukan."

        conn.execute(
            update(opd_table)
            .where(opd_table.c.id == int(opd_id))
            .values(deleted_at=None, aktif=True, updated_at=func.now())
        )
    return True, f"OPD '{target['nama']}' berhasil dipulihkan dan diaktifkan kembali."


def toggle_opd_status(engine: Engine, opd_id: int, aktif: bool) -> tuple[bool, str]:
    """Ubah status aktif OPD."""
    init_opd_schema(engine)
    with engine.begin() as conn:
        stmt = select(opd_table).where(opd_table.c.id == int(opd_id))
        target = conn.execute(stmt).mappings().one_or_none()
        if target is None:
            return False, "Data OPD tidak ditemukan."

        conn.execute(
            update(opd_table)
            .where(opd_table.c.id == int(opd_id))
            .values(aktif=bool(aktif), updated_at=func.now())
        )
    status_text = "diaktifkan" if aktif else "dinonaktifkan"
    return True, f"Status OPD '{target['nama']}' berhasil {status_text}."
