"""Status populasi presensi berbasis NIP dan tanggal efektif.

Modul ini tidak menghapus histori. Ia hanya memberi anotasi dan, bila diminta,
memilih baris yang masih termasuk populasi aktif pada periodenya.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd


STATUS_FILE = Path(__file__).resolve().parents[1] / "data" / "nonaktif_pegawai.csv"
MONTH_NUMBERS = {
    "Januari": 1, "Februari": 2, "Maret": 3, "April": 4,
    "Mei": 5, "Juni": 6, "Juli": 7, "Agustus": 8,
    "September": 9, "Oktober": 10, "November": 11, "Desember": 12,
}


def load_inactive_employee_config(path: str | Path = STATUS_FILE) -> pd.DataFrame:
    """Baca status pegawai nonaktif dari PostgreSQL atau fallback ke file konfigurasi."""
    columns = ["NIP", "Tanggal Efektif", "Status", "Keterangan"]
    
    # 1. Coba baca dari database PostgreSQL
    try:
        from database.connection import get_engine
        from database.pegawai import pegawai_table
        from sqlalchemy import select, or_
        engine = get_engine()
        with engine.connect() as conn:
            stmt = select(
                pegawai_table.c.nip,
                pegawai_table.c.aktif,
                pegawai_table.c.deleted_at,
                pegawai_table.c.updated_at,
            ).where(
                or_(pegawai_table.c.aktif.is_(False), pegawai_table.c.deleted_at.is_not(None))
            )
            rows = conn.execute(stmt).all()
            if rows:
                records = []
                for r in rows:
                    eff_date = r.deleted_at or r.updated_at or pd.Timestamp("2026-01-01")
                    records.append({
                        "NIP": str(r.nip).strip(),
                        "Tanggal Efektif": pd.to_datetime(eff_date).normalize(),
                        "Status": "NONAKTIF" if not r.aktif else "DIHAPUS",
                        "Keterangan": "Status nonaktif / mutasi dari Database PostgreSQL",
                    })
                return pd.DataFrame(records)
    except Exception:
        pass

    # 2. Fallback baca dari file CSV
    source_path = Path(path)
    if not source_path.exists():
        return pd.DataFrame(columns=columns)
    result = pd.read_csv(source_path, dtype={"nip": "string"})
    result.columns = [str(column).strip().lower() for column in result.columns]
    required = {"nip", "tanggal_efektif", "status", "keterangan"}
    if not required.issubset(result.columns):
        raise ValueError(f"Kolom konfigurasi pegawai nonaktif belum lengkap: {sorted(required - set(result.columns))}")
    result = result[list(required)].copy()
    result["nip"] = result["nip"].astype("string").str.strip()
    result["tanggal_efektif"] = pd.to_datetime(result["tanggal_efektif"], errors="raise").dt.normalize()
    if result["nip"].duplicated().any():
        raise ValueError("NIP duplikat pada konfigurasi pegawai nonaktif")
    return result.rename(columns={
        "nip": "NIP", "tanggal_efektif": "Tanggal Efektif",
        "status": "Status", "keterangan": "Keterangan",
    })


def _row_period_dates(frame: pd.DataFrame) -> pd.Series:
    if "Tanggal" in frame.columns:
        return pd.to_datetime(frame["Tanggal"], errors="coerce").dt.normalize()
    if {"Tahun", "Bulan"}.issubset(frame.columns):
        years = pd.to_numeric(frame["Tahun"], errors="coerce")
        months = frame["Bulan"].map(MONTH_NUMBERS)
        if months.isna().all():
            months = pd.to_numeric(frame["Bulan"], errors="coerce")
        return pd.to_datetime({"year": years, "month": months, "day": 1}, errors="coerce")
    raise ValueError("Data harus memiliki Tanggal atau kombinasi Tahun + Bulan")


def apply_employee_active_status(
    frame: pd.DataFrame, analysis_date=None, *, active_only: bool = True,
    config_path: str | Path = STATUS_FILE,
) -> pd.DataFrame:
    """Tentukan keaktifan per periode; histori sebelum tanggal efektif dipertahankan."""
    result = frame.copy()
    if result.empty:
        result["is_active_for_period"] = pd.Series(dtype=bool)
        result["Tanggal Efektif Nonaktif"] = pd.Series(dtype="datetime64[ns]")
        return result
    if "NIP" not in result.columns:
        raise ValueError("Kolom NIP diperlukan untuk menentukan status aktif")
    config = load_inactive_employee_config(config_path)
    effective_by_nip = config.set_index("NIP")["Tanggal Efektif"] if not config.empty else pd.Series(dtype="datetime64[ns]")
    result["Tanggal Efektif Nonaktif"] = result["NIP"].astype(str).str.strip().map(effective_by_nip)
    period_dates = pd.Series(pd.Timestamp(analysis_date).normalize(), index=result.index) if analysis_date is not None else _row_period_dates(result)
    result["is_active_for_period"] = result["Tanggal Efektif Nonaktif"].isna() | period_dates.lt(result["Tanggal Efektif Nonaktif"])
    return result[result["is_active_for_period"]].copy() if active_only else result


def get_employee_inactive_status(nip: str, path: str | Path = STATUS_FILE) -> dict[str, object] | None:
    config = load_inactive_employee_config(path)
    match = config[config["NIP"].astype(str).eq(str(nip).strip())]
    if match.empty:
        return None
    row = match.iloc[0]
    return {"status": str(row["Status"]), "effective_date": pd.Timestamp(row["Tanggal Efektif"]), "note": str(row["Keterangan"])}
