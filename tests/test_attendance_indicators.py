import pandas as pd
from modules.attendance_indicators import prepare_daily_indicators, summarize_attendance_indicators


def rows(statuses, *, late_days=0):
    dates = pd.date_range("2026-03-01", periods=len(statuses), freq="D")
    return pd.DataFrame({
        "NIP": "1", "Tanggal": dates, "Status": statuses,
        "Sumber_Datang": ["MESIN" if value in {"HADIR", "TERLAMBAT"} else value for value in statuses],
        "Sumber_Pulang": ["MESIN" if value in {"HADIR", "TERLAMBAT"} else value for value in statuses],
        "wajib_presensi": True, "TK": [value == "TK" for value in statuses],
        "Terlambat": [index < late_days for index in range(len(statuses))],
    })


def test_standard_example_separates_compliance_physical_and_lateness():
    source = rows(["HADIR"] * 14 + ["CUTI"] * 3 + ["WFH"] * 2 + ["DL"], late_days=8)
    summary = summarize_attendance_indicators(source)
    assert summary["compliance_percentage"] == 100.0
    assert summary["physical_attendance_percentage"] == 70.0
    assert summary["late_events"] == 8


def test_tk_reduces_compliance_and_late_day_is_not_double_counted():
    summary = summarize_attendance_indicators(rows(["HADIR"] * 19 + ["TK"], late_days=1))
    assert summary["compliance_percentage"] == 95.0
    assert summary["physical_attendance_days"] == 19
    assert summary["late_events"] == 1


def test_lateness_is_separate_from_full_compliance():
    summary = summarize_attendance_indicators(rows(["HADIR"] * 20, late_days=15))
    assert summary["compliance_percentage"] == 100.0
    assert summary["late_events"] == 15


def test_collective_leave_is_not_part_of_required_denominator():
    source = rows(["HADIR"] * 20 + ["LIBUR"])
    source.loc[source["Status"].eq("LIBUR"), "wajib_presensi"] = False
    summary = summarize_attendance_indicators(source)
    assert summary["required_days"] == 20
    assert summary["compliance_percentage"] == 100.0


def test_non_required_days_and_duplicate_dates_do_not_contribute():
    source = rows(["HADIR", "CUTI"])
    source.loc[1, "Tanggal"] = source.loc[0, "Tanggal"]
    source.loc[0, "wajib_presensi"] = False
    daily = prepare_daily_indicators(source)
    assert len(daily) == 1
    assert int(daily["Wajib Presensi"].sum()) == 0


def test_mixed_nonphysical_sources_contribute_to_only_one_daily_status():
    source = rows(["DL"])
    source.loc[0, "Status"] = "DL/WFH"
    source.loc[0, "Sumber_Datang"] = "DL"
    source.loc[0, "Sumber_Pulang"] = "WFH"

    daily = prepare_daily_indicators(source)

    assert int(daily[["Hadir Fisik", "Cuti", "WFH", "DL", "TK"]].sum(axis=1).iloc[0]) == 1
    assert bool(daily.loc[0, "DL"])
    assert not bool(daily.loc[0, "WFH"])
    assert bool(daily.loc[0, "Status Sah"])


def test_additional_source_codes_are_valid_physical_attendance():
    source = rows(["SL", "SK", "MR", "PBT"])

    summary = summarize_attendance_indicators(source)

    assert summary["valid_status_days"] == 4
    assert summary["physical_attendance_days"] == 4
    assert summary["compliance_percentage"] == 100.0


def test_cltn_tb_and_mpp_are_valid_nonphysical_leave():
    source = rows(["CLTN", "TB", "MPP"])

    summary = summarize_attendance_indicators(source)

    assert summary["valid_status_days"] == 3
    assert summary["leave_days"] == 3
    assert summary["physical_attendance_days"] == 0
    assert summary["compliance_percentage"] == 100.0
