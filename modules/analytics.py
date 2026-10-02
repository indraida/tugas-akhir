"""Analitik dashboard terpusat dan bebas dari dependensi Streamlit."""

from __future__ import annotations

import pandas as pd

from modules.employee_active_status import apply_employee_active_status


MONTH_ORDER = [
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
]

# Seluruh ambang early warning berada di satu tempat agar mudah diuji/diubah.
EARLY_WARNING_RULES = (
    {"min_tk": 0, "max_tk": 0, "status": "Normal", "priority": 0},
    {"min_tk": 1, "max_tk": 2, "status": "Perlu Perhatian", "priority": 1},
    {"min_tk": 3, "max_tk": 5, "status": "Perlu Verifikasi", "priority": 2},
    {"min_tk": 6, "max_tk": None, "status": "Prioritas Tindak Lanjut", "priority": 3},
)
WARNING_STATUSES = [rule["status"] for rule in EARLY_WARNING_RULES]
WARNING_PRIORITY = {
    rule["status"]: rule["priority"]
    for rule in EARLY_WARNING_RULES
}


def ews_period_context(year: int, month: str) -> dict[str, object]:
    """Konteks bulan EWS dan bulan pembanding, termasuk lintas tahun."""
    month_number = MONTH_ORDER.index(str(month)) + 1
    start_date = pd.Timestamp(year=int(year), month=month_number, day=1)
    end_date = start_date + pd.offsets.MonthEnd(0)
    previous_end = start_date - pd.Timedelta(days=1)
    return {
        "year": int(year), "month": str(month), "month_number": month_number,
        "start_date": start_date, "end_date": end_date,
        "previous_year": int(previous_end.year),
        "previous_month": MONTH_ORDER[int(previous_end.month) - 1],
    }


def get_monthly_tk_days(daily_df: pd.DataFrame, nip: object, year: int,
                        month: str | int) -> int | None:
    """TK bulanan dari tanggal wajib presensi unik; None berarti bulan tanpa data."""
    if daily_df.empty or not {"NIP", "Tanggal", "TK"}.issubset(daily_df.columns):
        return None
    month_number = int(month) if str(month).isdigit() else MONTH_ORDER.index(str(month)) + 1
    source = daily_df.copy()
    source["Tanggal"] = pd.to_datetime(source["Tanggal"], errors="coerce")
    source = source[
        source["NIP"].astype(str).eq(str(nip))
        & source["Tanggal"].dt.year.eq(int(year))
        & source["Tanggal"].dt.month.eq(month_number)
    ]
    if source.empty:
        return None
    if {"wajib_presensi", "eligible_tk"}.issubset(source.columns):
        source = source[
            source["wajib_presensi"].fillna(False).astype(bool)
            & source["eligible_tk"].fillna(False).astype(bool)
        ]
    daily = source.groupby(["NIP", "Tanggal"], dropna=False)["TK"].max()
    return int(daily.fillna(False).astype(bool).sum())


def warning_status(tk_days: int) -> str:
    value = max(int(tk_days), 0)
    for rule in EARLY_WARNING_RULES:
        if value >= rule["min_tk"] and (rule["max_tk"] is None or value <= rule["max_tk"]):
            return str(rule["status"])
    return "Normal"


def apply_filters(
    data: pd.DataFrame, *, year=None, month=None, opd=None,
    employee_type=None, warning=None,
) -> pd.DataFrame:
    result = apply_employee_active_status(data)
    if year not in (None, "Semua Tahun") and "Tahun" in result:
        result = result[pd.to_numeric(result["Tahun"], errors="coerce").eq(int(year))]
    if month not in (None, "Semua Bulan"):
        result = result[result["Bulan"].eq(month)]
    if opd not in (None, "Semua OPD"):
        result = result[result["Unit Kerja"].eq(opd)]
    if employee_type not in (None, "Semua Jenis Pegawai"):
        if "Jenis Pegawai" not in result.columns:
            return result.iloc[0:0].copy()
        result = result[result["Jenis Pegawai"].eq(employee_type)]
    if warning not in (None, "Semua Status"):
        employee = employee_summary(result)
        allowed = set(employee.loc[employee["Status Early Warning"].eq(warning), "NIP"])
        result = result[result["NIP"].isin(allowed)]
    return result


def attendance_rate(data: pd.DataFrame) -> float:
    if data.empty:
        return 0.0
    workdays = float(data["Hari Kerja"].sum())
    # Cuti/DL/WFH adalah alasan sah, bukan ketidakpatuhan. Kepatuhan hanya
    # mengurangi hari kerja dengan TK yang eksplisit dari sumber.
    compliant = float(data["Status Sah"].sum()) if "Status Sah" in data else max(workdays - float(data["TK"].sum()), 0.0)
    return compliant / workdays * 100 if workdays else 0.0


def employee_summary(data: pd.DataFrame) -> pd.DataFrame:
    columns = ["NIP", "Nama Pegawai", "Unit Kerja", "TK", "Terlambat", "Hari Kerja", "Cuti", "WFH", "DL"]
    if data.empty:
        return pd.DataFrame(columns=columns + ["Kepatuhan", "Status Early Warning"])
    result = data.groupby(["NIP", "Nama Pegawai", "Unit Kerja"], as_index=False, dropna=False).agg(
        {"TK": "sum", "Terlambat": "sum", "Hari Kerja": "sum", "Cuti": "sum", "WFH": "sum", "DL": "sum", **({"Status Sah": "sum"} if "Status Sah" in data else {})}
    )
    valid = result["Status Sah"] if "Status Sah" in result else (result["Hari Kerja"] - result["TK"]).clip(lower=0)
    result["Kepatuhan"] = (valid / result["Hari Kerja"].replace(0, pd.NA) * 100).fillna(0)
    result["Status Early Warning"] = result["TK"].map(warning_status)
    result["Prioritas Status"] = result["Status Early Warning"].map(WARNING_PRIORITY).astype(int)
    return result


def employee_warning_summary(
    current_data: pd.DataFrame,
    previous_data: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Ringkasan EWS per NIP dan perubahan berbasis status/TK, tanpa Risk Score."""
    current = employee_summary(current_data)
    if current.empty:
        return current.assign(
            **{"TK Sebelumnya": pd.Series(dtype="Int64"), "Delta TK": pd.Series(dtype="Int64"),
               "Status Sebelumnya": pd.Series(dtype="string"), "Tren": pd.Series(dtype="string"),
               "Warning Baru": pd.Series(dtype=bool)}
        )
    previous = employee_summary(previous_data) if previous_data is not None else pd.DataFrame()
    previous_index = previous.drop_duplicates("NIP").set_index("NIP") if not previous.empty else pd.DataFrame()
    result = current.copy()
    result["TK Sebelumnya"] = result["NIP"].map(previous_index["TK"]) if "TK" in previous_index else pd.NA
    result["Status Sebelumnya"] = result["NIP"].map(previous_index["Status Early Warning"]) if "Status Early Warning" in previous_index else pd.NA
    result["Prioritas Sebelumnya"] = result["Status Sebelumnya"].map(WARNING_PRIORITY)
    result["Delta TK"] = result["TK"] - pd.to_numeric(result["TK Sebelumnya"], errors="coerce")
    comparable = result["TK Sebelumnya"].notna()
    result["Tren"] = "Data pembanding belum tersedia"
    worsened = comparable & (
        result["Prioritas Status"].gt(result["Prioritas Sebelumnya"])
        | (result["Prioritas Status"].eq(result["Prioritas Sebelumnya"]) & result["Delta TK"].gt(0))
    )
    improved = comparable & (
        result["Prioritas Status"].lt(result["Prioritas Sebelumnya"])
        | (result["Prioritas Status"].eq(result["Prioritas Sebelumnya"]) & result["Delta TK"].lt(0))
    )
    stable = comparable & ~worsened & ~improved
    result.loc[worsened, "Tren"] = "Memburuk"
    result.loc[improved, "Tren"] = "Membaik"
    result.loc[stable, "Tren"] = "Stabil"
    result["Warning Baru"] = comparable & result["Status Sebelumnya"].eq("Normal") & result["Status Early Warning"].ne("Normal")
    return result


def dashboard_metrics(data: pd.DataFrame) -> dict[str, object]:
    employees = employee_summary(data)
    warning = employees[employees["Status Early Warning"].ne("Normal")]
    priority = employees[employees["Status Early Warning"].eq("Prioritas Tindak Lanjut")]
    opd = opd_summary(data)
    overall = attendance_rate(data)
    needs_attention = int((opd["Kepatuhan"] < overall).sum()) if not opd.empty else 0
    return {
        "total_employees": int(employees["NIP"].nunique()),
        "attendance_rate": overall,
        "total_tk": int(data["TK"].sum()) if not data.empty else 0,
        "employees_with_tk": int(employees.loc[employees["TK"].gt(0), "NIP"].nunique()),
        "early_warning": int(warning["NIP"].nunique()),
        "priority": int(priority["NIP"].nunique()),
        "opd_attention": needs_attention,
    }


def monthly_trend(data: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if data.empty:
        return pd.DataFrame(columns=["Tahun", "Bulan", "Periode", "Kepatuhan", "TK", "Perubahan"])
    for (year, month), group in data.groupby(["Tahun", "Bulan"], dropna=False):
        rows.append({"Tahun": int(year), "Bulan": month, "Kepatuhan": attendance_rate(group), "TK": int(group["TK"].sum())})
    result = pd.DataFrame(rows)
    result["_month"] = result["Bulan"].map({name: i for i, name in enumerate(MONTH_ORDER)})
    result = result.sort_values(["Tahun", "_month"]).drop(columns="_month")
    result["Periode"] = result["Bulan"].astype(str) + " " + result["Tahun"].astype(str)
    result["Perubahan"] = result["Kepatuhan"].diff()
    result["Perubahan TK (%)"] = result["TK"].pct_change().mul(100).replace([float("inf"), float("-inf")], pd.NA)
    return result.reset_index(drop=True)


def opd_summary(data: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for opd, group in data.groupby("Unit Kerja", dropna=False):
        rows.append({"OPD": opd, "Kepatuhan": attendance_rate(group), "TK": int(group["TK"].sum()), "Pegawai": int(group["NIP"].nunique())})
    return pd.DataFrame(rows).sort_values("Kepatuhan") if rows else pd.DataFrame(columns=["OPD", "Kepatuhan", "TK", "Pegawai"])
