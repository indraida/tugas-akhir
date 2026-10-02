"""Perhitungan kepatuhan presensi berbasis tanggal wajib presensi."""

from __future__ import annotations

import logging

import pandas as pd

from services.work_calendar import build_work_calendar
from modules.attendance_indicators import prepare_daily_indicators

LOGGER = logging.getLogger(__name__)

def calculate_monthly_attendance_compliance(
    daily_data: pd.DataFrame,
    selected_nip: str,
    year: int,
    month: int,
) -> dict[str, object]:
    """Hitung satu kontribusi maksimal per tanggal terhadap kepatuhan bulanan."""
    calendar = build_work_calendar(int(year), allow_pdf_extraction=False)
    month_calendar = calendar[
        calendar["tanggal"].dt.month.eq(int(month))
    ].copy()
    calendar_workdays = int(month_calendar["is_hari_kerja"].sum())
    required_dates = set(
        month_calendar.loc[month_calendar["wajib_presensi"], "tanggal"]
    )

    required_columns = {"NIP", "Tanggal", "Status"}
    if daily_data.empty or not required_columns.issubset(daily_data.columns):
        return {
            "calendar_workdays": calendar_workdays,
            "required_attendance_days": len(required_dates),
            "valid_status_days": 0,
            "physical_attendance_days": 0,
            "physical_attendance_percentage": None if not required_dates else 0.0,
            "tk_days": 0,
            "compliance_percentage": None if not required_dates else 0.0,
            "duplicate_days": 0,
            "unclassified_days": len(required_dates),
        }

    source = daily_data[daily_data["NIP"].astype(str).eq(str(selected_nip))].copy()
    source["_tanggal"] = pd.to_datetime(source["Tanggal"], errors="coerce").dt.normalize()
    source = source[
        source["_tanggal"].notna()
        & source["_tanggal"].dt.year.eq(int(year))
        & source["_tanggal"].dt.month.eq(int(month))
    ].sort_values("_tanggal", kind="stable")
    duplicate_days = int(source.loc[source.duplicated("_tanggal", keep=False), "_tanggal"].nunique())
    # Loader existing mempertahankan record parser pertama untuk periode duplikat.
    source = source.drop_duplicates("_tanggal", keep="first").set_index("_tanggal")
    required = source[source.index.isin(required_dates)].copy()

    required["wajib_presensi"] = True
    indicators = prepare_daily_indicators(required)
    valid_mask = indicators["Status Sah"].astype(bool)
    tk_mask = indicators["TK"].astype(bool)
    physical_mask = indicators["Hadir Fisik"].astype(bool)
    valid_dates = set(required.index[valid_mask])
    tk_dates = set(required.index[tk_mask])
    unclassified_dates = required_dates.difference(valid_dates).difference(tk_dates)
    valid_status_days = len(valid_dates)
    physical_attendance_days = int(physical_mask.sum())
    required_attendance_days = len(required_dates)
    percentage = (
        valid_status_days / required_attendance_days * 100
        if required_attendance_days else None
    )
    if percentage is not None and percentage > 100:
        LOGGER.error(
            "Kepatuhan >100%% untuk NIP=%s periode=%s-%02d; valid=%s wajib=%s",
            selected_nip, year, month, valid_status_days, required_attendance_days,
        )
        raise ValueError("Persentase kepatuhan melebihi 100%; periksa duplikasi data")
    if duplicate_days or unclassified_dates:
        LOGGER.warning(
            "Validasi kepatuhan NIP=%s periode=%s-%02d duplicate=%s unclassified=%s",
            selected_nip, year, month, duplicate_days, len(unclassified_dates),
        )
    return {
        "calendar_workdays": calendar_workdays,
        "required_attendance_days": required_attendance_days,
        "valid_status_days": valid_status_days,
        "physical_attendance_days": physical_attendance_days,
        "physical_attendance_percentage": physical_attendance_days / required_attendance_days * 100 if required_attendance_days else None,
        "tk_days": len(tk_dates),
        "compliance_percentage": percentage,
        "duplicate_days": duplicate_days,
        "unclassified_days": len(unclassified_dates),
    }
