from datetime import date

import pandas as pd
import pytest

from modules.attendance_indicators import lateness_by_weekday, summarize_attendance_indicators
from modules.analytics import attendance_rate, employee_summary
from modules.discipline_rules import calculate_consecutive_unexcused_days
from modules.excel_parser import build_dashboard_dataframe
from services.data_source import postgres_to_canonical


def database_rows(statuses, dates=None):
    return pd.DataFrame({
        "nip": "1", "nama_pegawai": "A", "opd": "O",
        "tanggal": dates or [date(2026, 1, 5 + i) for i in range(len(statuses))],
        "status_presensi": statuses, "jam_masuk": None, "jam_pulang": None,
        "keterlambatan_menit": 0, "sumber_file": "DB",
    })


def test_database_sick_leave_and_unknown_are_never_physical():
    daily = postgres_to_canonical(database_rows(["SAKIT", "IZIN", "", "UNKNOWN"]))
    summary = summarize_attendance_indicators(daily)
    assert summary["physical_attendance_days"] == 0
    assert summary["valid_status_days"] == 2
    assert summary["compliance_percentage"] == 50


def test_aggregate_uses_calendar_and_valid_statuses():
    daily = postgres_to_canonical(database_rows(
        ["HADIR", "CUTI BERSAMA", "UNKNOWN"],
        [date(2026, 2, 13), date(2026, 2, 16), date(2026, 2, 18)],
    ))
    monthly = build_dashboard_dataframe(daily)
    assert monthly["Hari Kerja"].sum() == 2
    assert monthly["Cuti"].sum() == 0
    assert attendance_rate(monthly) == 50
    assert employee_summary(monthly).iloc[0]["Kepatuhan"] == 50


@pytest.mark.parametrize("dates, expected", [
    (["2026-01-05", "2026-01-20"], 1),
    (["2026-01-02", "2026-01-05"], 2),
    (["2026-02-13", "2026-02-18"], 2),
])
def test_streak_breaks_on_missing_required_days_but_skips_holidays(dates, expected):
    daily = postgres_to_canonical(database_rows(["TK"] * len(dates), dates))
    assert calculate_consecutive_unexcused_days(daily, 2026, 2) == expected


def test_lateness_counts_actual_weekday_and_deduplicates():
    daily = postgres_to_canonical(database_rows(
        ["TERLAMBAT"] * 3,
        [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 6)],
    ))
    counts = lateness_by_weekday(daily)
    assert counts["Senin"] == 1
    assert counts["Selasa"] == 1
    assert counts.sum() == 2
