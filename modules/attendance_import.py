"""Preview dan validasi upload presensi sebelum memakai ETL database existing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

import pandas as pd

from database.repository import find_existing_attendance_keys, prepare_daily_records
from modules.excel_parser import KONTRAK_LONG, read_attendance_excel


@dataclass
class AttendanceImportPreview:
    data: pd.DataFrame
    valid_data: pd.DataFrame
    invalid_data: pd.DataFrame
    files: list[str]
    periods: list[tuple[int, int]]
    duplicate_count: int
    existing_count: int = 0


def _reason_series(frame: pd.DataFrame) -> pd.Series:
    nip = frame["NIP"].astype("string").str.strip()
    dates = pd.to_datetime(frame["Tanggal"], errors="coerce")
    late = pd.to_numeric(frame["Menit_Terlambat"], errors="coerce")
    names = frame["Nama"].astype("string").str.strip()
    opd = frame["Unit Kerja"].astype("string").str.strip()
    source_in = frame["Sumber_Datang"].astype("string").str.strip().str.upper()
    source_out = frame["Sumber_Pulang"].astype("string").str.strip().str.upper()
    now_year = datetime.now().year
    reasons = pd.Series("", index=frame.index, dtype="string")

    def add(mask: pd.Series, label: str) -> None:
        nonlocal reasons
        reasons.loc[mask] = reasons.loc[mask].map(lambda value: f"{value}; {label}".strip("; "))

    invalid_nip = nip.isna() | nip.eq("") | ~nip.fillna("").str.fullmatch(r"\d+")
    invalid_date = dates.isna()
    unreasonable_date = dates.notna() & ~dates.dt.year.between(2000, now_year + 1)
    invalid_opd = opd.isna() | opd.eq("") | opd.str.lower().isin({"nan", "none", "-"})
    invalid_name = names.isna() | names.eq("") | names.str.lower().isin({"nan", "none"})
    invalid_late = late.isna() | late.lt(0)
    # Kode sumber yang memang sudah muncul pada laporan existing. Mapping ke
    # status aplikasi tetap dilakukan oleh ``prepare_daily_records``; daftar
    # ini tidak memperkenalkan status aplikasi baru.
    allowed_statuses = {
        "MESIN", "HADIR", "TK", "CUTI", "CUTI BERSAMA", "CUTIBESAR",
        "WFH", "WFA", "DL", "LIBUR", "CLTN", "MR", "PBT", "SK", "SL", "TB", "MPP",
    }
    invalid_status = ~source_in.fillna("").isin(allowed_statuses) | ~source_out.fillna("").isin(allowed_statuses)
    duplicate = frame.assign(_nip=nip, _date=dates).duplicated(["_nip", "_date"], keep=False)
    add(invalid_nip, "NIP kosong/tidak valid")
    add(invalid_date, "tanggal invalid")
    add(unreasonable_date, "tanggal di luar rentang yang masuk akal")
    add(invalid_opd, "OPD kosong")
    add(invalid_name, "nama pegawai kosong")
    add(invalid_late, "keterlambatan invalid")
    add(invalid_status, "status presensi tidak dikenal")
    add(duplicate, "duplikat NIP + tanggal dalam file")
    return reasons


def build_import_preview(uploaded_files: Iterable[Any], engine: Any | None = None) -> AttendanceImportPreview:
    """READ -> CLEAN -> STANDARDIZE -> VALIDATE, tanpa menulis database."""
    parsed: list[pd.DataFrame] = []
    names: list[str] = []
    for uploaded in uploaded_files:
        name = str(getattr(uploaded, "name", "upload.xlsx"))
        names.append(name)
        parsed.append(read_attendance_excel(uploaded, name))
    if not parsed:
        raise ValueError("Belum ada file yang dipilih.")
    data = pd.concat(parsed, ignore_index=True)
    missing = sorted(set(KONTRAK_LONG).difference(data.columns))
    if missing:
        raise ValueError(
            "File belum dapat diimport karena kolom wajib berikut tidak ditemukan: "
            + ", ".join(missing)
        )
    data["NIP"] = data["NIP"].astype("string").str.strip()
    data["Tanggal"] = pd.to_datetime(data["Tanggal"], errors="coerce")
    reasons = _reason_series(data)
    invalid = data.loc[reasons.ne("")].copy()
    invalid["Alasan"] = reasons.loc[invalid.index]
    valid = data.loc[reasons.eq("")].copy().reset_index(drop=True)
    # Guard terakhir memakai validator canonical yang juga dipakai saat UPSERT.
    if not valid.empty:
        _, report = prepare_daily_records(valid)
        if report["rejected"]:
            raise ValueError("Data belum lolos validasi ETL presensi existing.")
    dates = pd.to_datetime(valid["Tanggal"], errors="coerce") if not valid.empty else pd.Series(dtype="datetime64[ns]")
    periods = sorted({(int(value.year), int(value.month)) for value in dates.dropna()})
    existing_count = 0
    if engine is not None and not valid.empty:
        keys = [(str(row.NIP), row.Tanggal.date()) for row in valid[["NIP", "Tanggal"]].itertuples(index=False)]
        existing_count = len(find_existing_attendance_keys(engine, keys))
    duplicate_count = int(reasons.str.contains("duplikat NIP", regex=False).sum())
    return AttendanceImportPreview(data, valid, invalid.reset_index(drop=True), names, periods, duplicate_count, existing_count)
