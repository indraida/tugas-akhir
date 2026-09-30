"""Abstraction sumber presensi canonical untuk Excel dan PostgreSQL."""

from __future__ import annotations

import os

import pandas as pd

from database.connection import get_engine
from database.repository import load_attendance_from_db
from modules.employee_type import add_employee_type_columns
from modules.excel_parser import build_dashboard_dataframe, load_semua_presensi
from services.work_calendar import apply_work_calendar


MONTH_NAMES = {
    1: "Januari", 2: "Februari", 3: "Maret", 4: "April", 5: "Mei", 6: "Juni",
    7: "Juli", 8: "Agustus", 9: "September", 10: "Oktober", 11: "November", 12: "Desember",
}
DAY_NAMES = {0: "Senin", 1: "Selasa", 2: "Rabu", 3: "Kamis", 4: "Jumat", 5: "Sabtu", 6: "Minggu"}


def active_source_name() -> str:
    value = os.getenv("DATA_SOURCE", "postgres").strip().lower()
    if value in {"postgres", "postgresql"}:
        return "PostgreSQL"
    if value == "excel":
        return "Excel"
    return "PostgreSQL"


def postgres_to_canonical(frame: pd.DataFrame) -> pd.DataFrame:
    """Adaptasikan hasil query DB ke kontrak daily yang sama dengan parser Excel."""
    if frame.empty:
        return pd.DataFrame()
    source = frame.copy()
    dates = pd.to_datetime(source["tanggal"], errors="coerce")
    status = source["status_presensi"].fillna("").astype(str)
    status_upper = status.str.upper()
    result = pd.DataFrame({
        "NIP": source["nip"].astype(str),
        "Nama": source["nama_pegawai"].fillna("-"),
        "OPD": source["opd"].fillna("-"),
        "Unit Kerja": source["opd"].fillna("-"),
        "Tanggal": dates,
        "Jam_Masuk": source["jam_masuk"].map(lambda value: value.strftime("%H:%M") if hasattr(value, "strftime") else ""),
        "Jam_Pulang": source["jam_pulang"].map(lambda value: value.strftime("%H:%M") if hasattr(value, "strftime") else ""),
        "Status": status_upper.map(lambda value: "LIBUR" if value == "LIBUR" else value or "HADIR"),
        "Sumber_Datang": status_upper.map(lambda value: value if value in {"TK", "CUTI", "WFH", "WFA", "DL"} else "MESIN"),
        "Sumber_Pulang": status_upper.map(lambda value: value if value in {"TK", "CUTI", "WFH", "WFA", "DL"} else "MESIN"),
        "Menit_Terlambat": pd.to_numeric(source["keterlambatan_menit"], errors="coerce").fillna(0).astype(int),
        "Tahun": dates.dt.year,
        "Bulan": dates.dt.month,
        "Nama_Bulan": dates.dt.month.map(MONTH_NAMES),
        "Hari": dates.dt.dayofweek.map(DAY_NAMES),
        "Terlambat": status_upper.eq("TERLAMBAT") | pd.to_numeric(source["keterlambatan_menit"], errors="coerce").fillna(0).gt(0),
        "Pulang_Awal": False,
        "TK": status_upper.eq("TK"),
        "Presensi_Tidak_Lengkap": False,
        "Tidak_Absen_Masuk": False,
        "Tidak_Absen_Pulang": False,
        "Sumber_File": source["sumber_file"].fillna("PostgreSQL"),
    })
    result = result.dropna(subset=["Tanggal"]).reset_index(drop=True)
    result = add_employee_type_columns(result)
    result.attrs["source_info"] = {"name": "PostgreSQL", "connection_status": "Terhubung"}
    return apply_work_calendar(result)


def load_daily_data(*, year: int | None = None, month: int | None = None, opd: str | None = None) -> pd.DataFrame:
    """Muat data canonical; fallback hanya jika secara eksplisit diizinkan."""
    if active_source_name() == "Excel":
        result = apply_work_calendar(load_semua_presensi())
        result.attrs["source_info"] = {"name": "Excel", "connection_status": "File tersedia"}
        return result
    try:
        return postgres_to_canonical(load_attendance_from_db(get_engine(), year=year, month=month, opd=opd))
    except Exception as exc:
        if os.getenv("ALLOW_EXCEL_FALLBACK", "false").strip().lower() != "true":
            raise RuntimeError(f"PostgreSQL gagal dimuat dan fallback Excel tidak diizinkan: {exc}") from exc
        result = apply_work_calendar(load_semua_presensi())
        result.attrs["source_info"] = {"name": "Excel (fallback)", "connection_status": f"PostgreSQL gagal: {exc}"}
        return result


def load_dashboard_data(**filters) -> pd.DataFrame:
    return build_dashboard_dataframe(load_daily_data(**filters))
