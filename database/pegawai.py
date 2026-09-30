"""Modul schema & operasi database PostgreSQL untuk Master Data Pegawai."""

from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey, Index,
    MetaData, String, Table, UniqueConstraint, func, select, update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from database.opd import list_opds, opd_table
from database.schema import metadata

LOGGER = logging.getLogger(__name__)

pegawai_table = Table(
    "pegawai",
    metadata,
    Column("id_pegawai", BigInteger, primary_key=True, autoincrement=True),
    Column("nip", String(30), nullable=False, unique=True),
    Column("nama_pegawai", String(200), nullable=False),
    Column("id_opd", BigInteger, ForeignKey("opd.id"), nullable=True),
    Column("jabatan", String(150), nullable=True),
    Column("jenis_kelamin", String(20), nullable=True),
    Column("status_pegawai", String(50), nullable=False, server_default="PNS"),
    Column("aktif", Boolean, nullable=False, server_default="true"),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("updated_at", DateTime, nullable=False, server_default=func.now()),
    Column("deleted_at", DateTime, nullable=True),
    UniqueConstraint("nip", name="uq_pegawai_nip"),
)

Index("idx_pegawai_nip", pegawai_table.c.nip)
Index("idx_pegawai_nama", pegawai_table.c.nama_pegawai)
Index("idx_pegawai_id_opd", pegawai_table.c.id_opd)
Index("idx_pegawai_status", pegawai_table.c.status_pegawai)
Index("idx_pegawai_aktif", pegawai_table.c.aktif)
Index("idx_pegawai_deleted_at", pegawai_table.c.deleted_at)

STATUS_PEGAWAI_CHOICES = [
    "PNS",
    "PPPK",
    "CPNS",
    "NON-ASN",
    "HONORER",
    "KONTRAK",
]

JENIS_KELAMIN_CHOICES = [
    "Laki-laki",
    "Perempuan",
]


def init_pegawai_schema(engine: Engine) -> None:
    """Buat tabel pegawai jika belum ada."""
    metadata.create_all(engine)


def normalize_gender(value: Any) -> str:
    """Normalisasi input jenis kelamin ke format standar."""
    if not value or pd.isna(value):
        return "Laki-laki"
    text = str(value).strip().upper()
    if text in {"L", "LAKI-LAKI", "LAKI", "PRIA", "MALE", "M", "1"}:
        return "Laki-laki"
    if text in {"P", "PEREMPUAN", "WANITA", "FEMALE", "F", "2"}:
        return "Perempuan"
    return str(value).strip()


def normalize_status_pegawai(value: Any) -> str:
    """Normalisasi status kepegawaian."""
    if not value or pd.isna(value):
        return "PNS"
    text = str(value).strip().upper()
    for choice in STATUS_PEGAWAI_CHOICES:
        if choice in text:
            return choice
    return str(value).strip().upper()


def seed_default_pegawai(engine: Engine) -> list[str]:
    """Inisialisasi sampel data pegawai jika tabel masih kosong."""
    init_pegawai_schema(engine)
    
    # Ambil OPD pertama yang tersedia
    opds = list_opds(engine, aktif_only=True)
    opd_id_default = opds[0]["id"] if opds else None
    
    sample_data = [
        {
            "nip": "198501152010011012",
            "nama_pegawai": "Andi Pratama, S.Kom",
            "id_opd": opd_id_default,
            "jabatan": "Pranata Komputer Ahli Muda",
            "jenis_kelamin": "Laki-laki",
            "status_pegawai": "PNS",
            "aktif": True,
        },
        {
            "nip": "198804202014022003",
            "nama_pegawai": "Siti Rahma, S.E",
            "id_opd": opd_id_default,
            "jabatan": "Analis Keuangan Pusat dan Daerah",
            "jenis_kelamin": "Perempuan",
            "status_pegawai": "PNS",
            "aktif": True,
        },
        {
            "nip": "199207102022211005",
            "nama_pegawai": "Budi Santoso, S.Tr.Kom",
            "id_opd": opd_id_default,
            "jabatan": "Ahli Pertama - Pranata Komputer",
            "jenis_kelamin": "Laki-laki",
            "status_pegawai": "PPPK",
            "aktif": True,
        },
    ]

    created: list[str] = []
    with engine.begin() as conn:
        for item in sample_data:
            stmt = select(pegawai_table.c.id_pegawai).where(pegawai_table.c.nip == item["nip"])
            existing = conn.execute(stmt).scalar_one_or_none()
            if existing is None:
                conn.execute(pegawai_table.insert().values(**item))
                created.append(item["nama_pegawai"])
    return created


def list_pegawai(
    engine: Engine,
    include_deleted: bool = False,
    aktif_only: bool = False,
    id_opd: int | None = None,
) -> list[dict[str, Any]]:
    """Ambil daftar pegawai dari PostgreSQL beserta nama OPD."""
    init_pegawai_schema(engine)
    
    # Ambil map OPD
    opd_map: dict[int, dict[str, str]] = {
        o["id"]: {"nama": o["nama"], "kode": o["kode"], "singkatan": o["singkatan"]}
        for o in list_opds(engine, include_deleted=True)
    }

    with engine.connect() as conn:
        stmt = select(pegawai_table).order_by(pegawai_table.c.nama_pegawai)
        if not include_deleted:
            stmt = stmt.where(pegawai_table.c.deleted_at.is_(None))
        if aktif_only:
            stmt = stmt.where(pegawai_table.c.aktif.is_(True))
        if id_opd is not None:
            stmt = stmt.where(pegawai_table.c.id_opd == int(id_opd))
            
        rows = conn.execute(stmt).mappings().all()
        
        # Check jika kosong dan belum di-seed
        if not rows and not include_deleted:
            count_stmt = select(func.count(pegawai_table.c.id_pegawai))
            if (conn.execute(count_stmt).scalar() or 0) == 0:
                seed_default_pegawai(engine)
                rows = conn.execute(stmt).mappings().all()

        results = []
        for r in rows:
            opd_info = opd_map.get(r["id_opd"], {}) if r["id_opd"] else {}
            results.append({
                "id_pegawai": r["id_pegawai"],
                "nip": r["nip"],
                "nama_pegawai": r["nama_pegawai"],
                "id_opd": r["id_opd"],
                "nama_opd": opd_info.get("nama") or "-",
                "kode_opd": opd_info.get("kode") or "-",
                "singkatan_opd": opd_info.get("singkatan") or "",
                "jabatan": r["jabatan"] or "-",
                "jenis_kelamin": r["jenis_kelamin"] or "Laki-laki",
                "status_pegawai": r["status_pegawai"] or "PNS",
                "aktif": bool(r["aktif"]),
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "deleted_at": r["deleted_at"],
            })
        return results


def get_pegawai_by_id(engine: Engine, id_pegawai: int) -> dict[str, Any] | None:
    """Ambil data pegawai berdasarkan ID."""
    if not id_pegawai:
        return None
    init_pegawai_schema(engine)
    with engine.connect() as conn:
        stmt = select(pegawai_table).where(pegawai_table.c.id_pegawai == int(id_pegawai))
        row = conn.execute(stmt).mappings().one_or_none()
        if row is None:
            return None
            
        nama_opd = "-"
        if row["id_opd"]:
            opd_stmt = select(opd_table.c.nama).where(opd_table.c.id == row["id_opd"])
            nama_opd = conn.execute(opd_stmt).scalar() or "-"

        return {
            "id_pegawai": row["id_pegawai"],
            "nip": row["nip"],
            "nama_pegawai": row["nama_pegawai"],
            "id_opd": row["id_opd"],
            "nama_opd": nama_opd,
            "jabatan": row["jabatan"] or "-",
            "jenis_kelamin": row["jenis_kelamin"] or "Laki-laki",
            "status_pegawai": row["status_pegawai"] or "PNS",
            "aktif": bool(row["aktif"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "deleted_at": row["deleted_at"],
        }


def get_pegawai_by_nip(engine: Engine, nip: str) -> dict[str, Any] | None:
    """Ambil data pegawai berdasarkan NIP."""
    if not nip:
        return None
    clean_nip = str(nip).strip().replace(" ", "").replace("-", "")
    init_pegawai_schema(engine)
    with engine.connect() as conn:
        stmt = select(pegawai_table).where(pegawai_table.c.nip == clean_nip)
        row = conn.execute(stmt).mappings().one_or_none()
        if row is None:
            return None
        return get_pegawai_by_id(engine, row["id_pegawai"])


def create_pegawai(engine: Engine, data: dict[str, Any]) -> dict[str, Any]:
    """Tambah pegawai baru ke database PostgreSQL."""
    nip = str(data.get("nip") or "").strip().replace(" ", "").replace("-", "")
    nama = str(data.get("nama_pegawai") or "").strip()
    
    if not nip:
        raise ValueError("NIP wajib diisi.")
    if not nama:
        raise ValueError("Nama Pegawai wajib diisi.")
        
    init_pegawai_schema(engine)
    
    with engine.begin() as conn:
        check_stmt = select(pegawai_table.c.id_pegawai).where(pegawai_table.c.nip == nip)
        if conn.execute(check_stmt).scalar_one_or_none() is not None:
            raise ValueError(f"NIP '{nip}' sudah terdaftar.")
            
        id_opd = data.get("id_opd")
        if id_opd in ("", None, "None", 0, "-"):
            id_opd = None
        else:
            id_opd = int(id_opd)

        values = {
            "nip": nip,
            "nama_pegawai": nama,
            "id_opd": id_opd,
            "jabatan": str(data.get("jabatan") or "").strip() or None,
            "jenis_kelamin": normalize_gender(data.get("jenis_kelamin")),
            "status_pegawai": normalize_status_pegawai(data.get("status_pegawai")),
            "aktif": bool(data.get("aktif", True)),
        }
        result = conn.execute(pegawai_table.insert().values(**values))
        id_pegawai = result.inserted_primary_key[0]
        
    return get_pegawai_by_id(engine, id_pegawai) or {"id_pegawai": id_pegawai, **values}


def update_pegawai(engine: Engine, id_pegawai: int, data: dict[str, Any]) -> tuple[bool, str]:
    """Perbarui informasi pegawai di database PostgreSQL."""
    if not id_pegawai:
        return False, "ID Pegawai tidak valid."
        
    nip = str(data.get("nip") or "").strip().replace(" ", "").replace("-", "")
    nama = str(data.get("nama_pegawai") or "").strip()
    
    if not nip:
        return False, "NIP wajib diisi."
    if not nama:
        return False, "Nama Pegawai wajib diisi."
        
    init_pegawai_schema(engine)
    
    with engine.begin() as conn:
        stmt = select(pegawai_table).where(pegawai_table.c.id_pegawai == int(id_pegawai))
        target = conn.execute(stmt).mappings().one_or_none()
        if target is None:
            return False, "Data Pegawai tidak ditemukan."

        check_stmt = select(pegawai_table.c.id_pegawai).where(
            pegawai_table.c.nip == nip,
            pegawai_table.c.id_pegawai != int(id_pegawai),
        )
        if conn.execute(check_stmt).scalar_one_or_none() is not None:
            return False, f"NIP '{nip}' sudah digunakan oleh pegawai lain."

        id_opd = data.get("id_opd")
        if id_opd in ("", None, "None", 0, "-"):
            id_opd = None
        else:
            id_opd = int(id_opd)

        values = {
            "nip": nip,
            "nama_pegawai": nama,
            "id_opd": id_opd,
            "jabatan": str(data.get("jabatan") or "").strip() or None,
            "jenis_kelamin": normalize_gender(data.get("jenis_kelamin")),
            "status_pegawai": normalize_status_pegawai(data.get("status_pegawai")),
            "aktif": bool(data.get("aktif", True)),
            "updated_at": func.now(),
        }
        conn.execute(
            update(pegawai_table)
            .where(pegawai_table.c.id_pegawai == int(id_pegawai))
            .values(**values)
        )
    return True, f"Data pegawai '{nama}' berhasil diperbarui."


def delete_pegawai(engine: Engine, id_pegawai: int, hard_delete: bool = False) -> tuple[bool, str]:
    """Hapus pegawai dari database (default soft-delete dengan deleted_at)."""
    init_pegawai_schema(engine)
    with engine.begin() as conn:
        stmt = select(pegawai_table).where(pegawai_table.c.id_pegawai == int(id_pegawai))
        target = conn.execute(stmt).mappings().one_or_none()
        if target is None:
            return False, "Data Pegawai tidak ditemukan."

        if hard_delete:
            conn.execute(pegawai_table.delete().where(pegawai_table.c.id_pegawai == int(id_pegawai)))
            msg = f"Pegawai '{target['nama_pegawai']}' ({target['nip']}) berhasil dihapus permanen."
        else:
            conn.execute(
                update(pegawai_table)
                .where(pegawai_table.c.id_pegawai == int(id_pegawai))
                .values(deleted_at=func.now(), aktif=False, updated_at=func.now())
            )
            msg = f"Pegawai '{target['nama_pegawai']}' ({target['nip']}) dipindahkan ke tempat sampah (soft delete)."
            
    return True, msg


def restore_pegawai(engine: Engine, id_pegawai: int) -> tuple[bool, str]:
    """Pulihkan pegawai yang di-soft delete."""
    init_pegawai_schema(engine)
    with engine.begin() as conn:
        stmt = select(pegawai_table).where(pegawai_table.c.id_pegawai == int(id_pegawai))
        target = conn.execute(stmt).mappings().one_or_none()
        if target is None:
            return False, "Data Pegawai tidak ditemukan."

        conn.execute(
            update(pegawai_table)
            .where(pegawai_table.c.id_pegawai == int(id_pegawai))
            .values(deleted_at=None, aktif=True, updated_at=func.now())
        )
    return True, f"Pegawai '{target['nama_pegawai']}' berhasil dipulihkan."


def toggle_pegawai_status(engine: Engine, id_pegawai: int, aktif: bool) -> tuple[bool, str]:
    """Ubah status aktif pegawai."""
    init_pegawai_schema(engine)
    with engine.begin() as conn:
        stmt = select(pegawai_table).where(pegawai_table.c.id_pegawai == int(id_pegawai))
        target = conn.execute(stmt).mappings().one_or_none()
        if target is None:
            return False, "Data Pegawai tidak ditemukan."

        conn.execute(
            update(pegawai_table)
            .where(pegawai_table.c.id_pegawai == int(id_pegawai))
            .values(aktif=bool(aktif), updated_at=func.now())
        )
    status_text = "diaktifkan" if aktif else "dinonaktifkan"
    return True, f"Status pegawai '{target['nama_pegawai']}' berhasil {status_text}."


# ============================================================================
# Excel Import & Export Helpers
# ============================================================================

def generate_pegawai_excel_template() -> bytes:
    """Buat file template Excel (.xlsx) siap pakai untuk import data pegawai."""
    sample_df = pd.DataFrame([
        {
            "NIP": "198501152010011012",
            "Nama Pegawai": "Andi Pratama, S.Kom",
            "Nama OPD": "Badan Kepegawaian dan Pengembangan Sumber Daya Manusia",
            "Jabatan": "Pranata Komputer Ahli Muda",
            "Jenis Kelamin": "Laki-laki",
            "Status Pegawai": "PNS",
            "Status Aktif": "Aktif",
        },
        {
            "NIP": "198804202014022003",
            "Nama Pegawai": "Siti Rahma, S.E",
            "Nama OPD": "Dinas Pendidikan",
            "Jabatan": "Analis Keuangan",
            "Jenis Kelamin": "Perempuan",
            "Status Pegawai": "PNS",
            "Status Aktif": "Aktif",
        },
        {
            "NIP": "199207102022211005",
            "Nama Pegawai": "Budi Santoso, S.Tr.Kom",
            "Nama OPD": "Dinas Komunikasi dan Informatika",
            "Jabatan": "Ahli Pertama - Pranata Komputer",
            "Jenis Kelamin": "Laki-laki",
            "Status Pegawai": "PPPK",
            "Status Aktif": "Aktif",
        },
    ])
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        sample_df.to_excel(writer, index=False, sheet_name="Master Pegawai")
    return output.getvalue()


def import_pegawai_from_excel(engine: Engine, file_or_bytes: Any) -> dict[str, Any]:
    """Import data pegawai dari file Excel / DataFrame ke PostgreSQL."""
    init_pegawai_schema(engine)
    
    if isinstance(file_or_bytes, pd.DataFrame):
        df = file_or_bytes
    else:
        df = pd.read_excel(file_or_bytes, dtype=str)
        
    if df.empty:
        return {"total_rows": 0, "inserted": 0, "updated": 0, "failed": 0, "errors": ["File kosong."]}

    # Map OPD lookup (by nama, kode, singkatan)
    opds = list_opds(engine, include_deleted=True)
    opd_lookup: dict[str, int] = {}
    for o in opds:
        opd_lookup[str(o["id"])] = o["id"]
        if o["nama"]:
            opd_lookup[o["nama"].strip().lower()] = o["id"]
        if o["kode"]:
            opd_lookup[o["kode"].strip().lower()] = o["id"]
        if o["singkatan"]:
            opd_lookup[o["singkatan"].strip().lower()] = o["id"]

    # Identifikasi kolom
    col_nip = next((c for c in df.columns if c.strip().lower() in {"nip", "nomor induk", "no_induk", "nik"}), None)
    col_nama = next((c for c in df.columns if c.strip().lower() in {"nama", "nama pegawai", "nama_pegawai", "nama lengkap"}), None)
    col_opd = next((c for c in df.columns if c.strip().lower() in {"opd", "nama opd", "unit kerja", "id_opd", "unit_kerja"}), None)
    col_jabatan = next((c for c in df.columns if c.strip().lower() in {"jabatan", "posisi"}), None)
    col_jk = next((c for c in df.columns if c.strip().lower() in {"jenis kelamin", "jenis_kelamin", "jk", "gender", "l/p"}), None)
    col_status = next((c for c in df.columns if c.strip().lower() in {"status pegawai", "status_pegawai", "status", "jenis pegawai"}), None)
    col_aktif = next((c for c in df.columns if c.strip().lower() in {"aktif", "status aktif", "is_active"}), None)

    if not col_nip or not col_nama:
        return {
            "total_rows": len(df),
            "inserted": 0,
            "updated": 0,
            "failed": len(df),
            "errors": [f"Kolom wajib 'NIP' dan 'Nama Pegawai' tidak ditemukan. Kolom yang terdeteksi: {list(df.columns)}"],
        }

    inserted = 0
    updated_cnt = 0
    failed = 0
    errors: list[str] = []

    with engine.begin() as conn:
        for idx, row in df.iterrows():
            raw_nip = row.get(col_nip)
            raw_nama = row.get(col_nama)
            
            nip = str(raw_nip or "").strip().replace(" ", "").replace("-", "")
            nama = str(raw_nama or "").strip()
            
            if not nip or nip.lower() in {"nan", "none", ""}:
                failed += 1
                errors.append(f"Baris {idx + 2}: NIP kosong, baris dilewati.")
                continue
            if not nama or nama.lower() in {"nan", "none", ""}:
                failed += 1
                errors.append(f"Baris {idx + 2} (NIP {nip}): Nama pegawai kosong, baris dilewati.")
                continue

            # Resolve id_opd
            id_opd_val = None
            if col_opd and not pd.isna(row.get(col_opd)):
                raw_opd_str = str(row[col_opd]).strip().lower()
                id_opd_val = opd_lookup.get(raw_opd_str)

            jabatan_val = str(row.get(col_jabatan) or "").strip() or None if col_jabatan and not pd.isna(row.get(col_jabatan)) else None
            jk_val = normalize_gender(row.get(col_jk)) if col_jk and not pd.isna(row.get(col_jk)) else "Laki-laki"
            status_val = normalize_status_pegawai(row.get(col_status)) if col_status and not pd.isna(row.get(col_status)) else "PNS"
            
            aktif_val = True
            if col_aktif and not pd.isna(row.get(col_aktif)):
                text_aktif = str(row[col_aktif]).strip().lower()
                aktif_val = text_aktif in {"1", "true", "aktif", "ya", "active"}

            # Cek eksistensi untuk UPSERT
            stmt_check = select(pegawai_table.c.id_pegawai).where(pegawai_table.c.nip == nip)
            existing_id = conn.execute(stmt_check).scalar_one_or_none()

            if existing_id is not None:
                # Update
                conn.execute(
                    update(pegawai_table)
                    .where(pegawai_table.c.id_pegawai == existing_id)
                    .values(
                        nama_pegawai=nama,
                        id_opd=id_opd_val,
                        jabatan=jabatan_val,
                        jenis_kelamin=jk_val,
                        status_pegawai=status_val,
                        aktif=aktif_val,
                        deleted_at=None,
                        updated_at=func.now(),
                    )
                )
                updated_cnt += 1
            else:
                # Insert
                conn.execute(
                    pegawai_table.insert().values(
                        nip=nip,
                        nama_pegawai=nama,
                        id_opd=id_opd_val,
                        jabatan=jabatan_val,
                        jenis_kelamin=jk_val,
                        status_pegawai=status_val,
                        aktif=aktif_val,
                    )
                )
                inserted += 1

    return {
        "total_rows": len(df),
        "inserted": inserted,
        "updated": updated_cnt,
        "failed": failed,
        "errors": errors[:10],  # tampilkan 10 error pertama
    }
