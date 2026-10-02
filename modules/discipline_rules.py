"""Rule-Based Disiplin ASN berdasarkan Pergub Kalbar Nomor 2 Tahun 2024.

Engine ini menghasilkan indikasi awal, bukan penetapan pelanggaran atau
hukuman disiplin. Nilai TK sumber tetap harus diverifikasi oleh pengelola.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


REGULATION = {
    "code": "PERGUB_KALBAR_2_2024",
    "title": (
        "Peraturan Gubernur Kalimantan Barat Nomor 2 Tahun 2024 tentang "
        "Disiplin Pegawai Aparatur Sipil Negara di Lingkungan Pemerintah "
        "Provinsi Kalimantan Barat"
    ),
    "short_title": "Pergub Kalbar No. 2 Tahun 2024",
    "year": 2024,
    "references": (
        "PP Nomor 94 Tahun 2021",
        "PP Nomor 49 Tahun 2018",
        "Peraturan BKN Nomor 6 Tahun 2022",
    ),
}

DISCLAIMER = (
    "Hasil Rule-Based merupakan indikasi awal berdasarkan data presensi dan "
    "ketentuan Pergub Kalbar Nomor 2 Tahun 2024. Penetapan pelanggaran dan "
    "hukuman disiplin dilakukan setelah verifikasi, pemeriksaan, dan keputusan "
    "pejabat yang berwenang sesuai ketentuan peraturan perundang-undangan."
)

COMMON_RULES = (
    {"min_days": 3, "max_days": 3, "level": "Ringan", "indication": "Teguran Lisan", "article": "Pasal 10 ayat (1) huruf c angka 1"},
    {"min_days": 4, "max_days": 6, "level": "Ringan", "indication": "Teguran Tertulis", "article": "Pasal 10 ayat (1) huruf c angka 2"},
    {"min_days": 7, "max_days": 10, "level": "Ringan", "indication": "Pernyataan Tidak Puas Secara Tertulis", "article": "Pasal 10 ayat (1) huruf c angka 3"},
)

DISCIPLINE_RULES = (
    {"employee_type": "PNS", "min_days": 11, "max_days": 13, "level": "Sedang", "indication": "Pemotongan tunjangan kinerja sebesar 25% selama 6 bulan", "article": "Pasal 11 ayat (2)"},
    {"employee_type": "PNS", "min_days": 14, "max_days": 16, "level": "Sedang", "indication": "Pemotongan tunjangan kinerja sebesar 25% selama 9 bulan", "article": "Pasal 11 ayat (3)"},
    {"employee_type": "PNS", "min_days": 17, "max_days": 20, "level": "Sedang", "indication": "Pemotongan tunjangan kinerja sebesar 25% selama 12 bulan", "article": "Pasal 11 ayat (4)"},
    {"employee_type": "PNS", "min_days": 21, "max_days": 24, "level": "Berat", "indication": "Penurunan jabatan setingkat lebih rendah selama 12 bulan", "article": "Pasal 12 ayat (1) huruf f angka 1"},
    {"employee_type": "PNS", "min_days": 25, "max_days": 27, "level": "Berat", "indication": "Pembebasan dari jabatan menjadi Jabatan Pelaksana selama 12 bulan", "article": "Pasal 12 ayat (1) huruf f angka 2"},
    {"employee_type": "PNS", "min_days": 28, "max_days": None, "level": "Berat", "indication": "Pemberhentian dengan hormat tidak atas permintaan sendiri sebagai PNS", "article": "Pasal 12 ayat (1) huruf f angka 3"},
    {"employee_type": "PPPK", "min_days": 11, "max_days": 13, "level": "Sedang", "indication": "Penundaan kenaikan gaji berkala selama 1 tahun", "article": "Pasal 11 ayat (2)"},
    {"employee_type": "PPPK", "min_days": 14, "max_days": 16, "level": "Sedang", "indication": "Penundaan kenaikan gaji berkala selama 2 tahun", "article": "Pasal 11 ayat (3)"},
    {"employee_type": "PPPK", "min_days": 17, "max_days": 20, "level": "Sedang", "indication": "Penundaan kenaikan gaji berkala selama 3 tahun dan tidak diperpanjang perjanjian kerja pada masa hubungan perjanjian kerja berikutnya", "article": "Pasal 11 ayat (4)"},
    {"employee_type": "PPPK", "min_days": 21, "max_days": 24, "level": "Berat", "indication": "Pemutusan hubungan perjanjian kerja dengan hormat tidak atas permintaan sendiri", "article": "Pasal 12 ayat (1) huruf g angka 1"},
)

CONSECUTIVE_RULES = {
    "PNS": {"level": "Berat", "indication": "Pemberhentian dengan hormat tidak atas permintaan sendiri sebagai PNS", "article": "Pasal 12 ayat (1) huruf f angka 4"},
    "PPPK": {"level": "Berat", "indication": "Pemutusan hubungan perjanjian kerja dengan hormat tidak atas permintaan sendiri", "article": "Pasal 12 ayat (1) huruf g angka 2"},
}


def _result(*, status: str, level: str | None, indication: str, article: str | None,
            employee_type: str, annual_days: int, consecutive_days: int | None) -> dict[str, Any]:
    return {
        "rule_status": status,
        "discipline_level": level,
        "indication": indication,
        "article": article,
        "regulation": REGULATION["short_title"],
        "regulation_code": REGULATION["code"],
        "employee_type": employee_type,
        "annual_unexcused_days": annual_days,
        "consecutive_unexcused_days": consecutive_days,
        "consecutive_rule_available": consecutive_days is not None,
        "requires_verification": True,
        "is_final_decision": False,
    }


def evaluate_discipline_rule(employee_type: str | None, annual_unexcused_days: int,
                             consecutive_unexcused_days: int | None = None, *,
                             nip: str | None = None, period_end: object = None,
                             data_granularity: str = "AGGREGATED",
                             consecutive_rule_available: bool | None = None) -> dict[str, Any]:
    """Evaluasi indikatif; rule streak selalu diprioritaskan dari akumulasi."""
    try:
        annual_days = int(annual_unexcused_days)
        consecutive_days = None if consecutive_unexcused_days is None else int(consecutive_unexcused_days)
    except (TypeError, ValueError):
        annual_days, consecutive_days = 0, None
        invalid_data = True
    else:
        invalid_data = annual_days < 0 or (consecutive_days is not None and consecutive_days < 0)
    kind = str(employee_type or "").strip().upper()
    if kind == "CPNS":
        kind = "PNS"
    available = consecutive_days is not None if consecutive_rule_available is None else bool(consecutive_rule_available)
    if not available:
        consecutive_days = None
    context = {"nip": None if nip is None else str(nip), "period_end": period_end,
               "data_granularity": str(data_granularity).upper(),
               "annual_tk_days": annual_days, "consecutive_tk_days": consecutive_days}
    if invalid_data:
        result = _result(status="REVIEW_REQUIRED", level="Perlu Verifikasi",
                         indication="Data rule tidak valid dan perlu diverifikasi.", article=None,
                         employee_type=kind or "UNKNOWN", annual_days=max(annual_days, 0),
                         consecutive_days=None)
        result.update(context)
        return result
    if kind not in {"PNS", "PPPK"}:
        result = _result(
            status="EMPLOYEE_TYPE_REVIEW_REQUIRED", level=None,
            indication="Jenis pegawai perlu diverifikasi sebelum menentukan ketentuan",
            article=None, employee_type=kind or "UNKNOWN",
            annual_days=annual_days, consecutive_days=consecutive_days,
        )
        result["discipline_level"] = "Perlu Verifikasi"
        result.update(context)
        return result
    if consecutive_days is not None and consecutive_days >= 10:
        rule = CONSECUTIVE_RULES[kind]
        result = _result(status="INDICATION", level=rule["level"], indication=rule["indication"],
                         article=rule["article"], employee_type=kind, annual_days=annual_days,
                         consecutive_days=consecutive_days)
        result.update(context)
        return result
    if annual_days <= 2:
        result = _result(
            status="BELUM_MENCAPAI_AMBANG", level="Belum Mencapai Ambang",
            indication="Belum mencapai ambang ketentuan ketidakhadiran",
            article=None, employee_type=kind, annual_days=annual_days, consecutive_days=consecutive_days,
        )
        result.update(context)
        return result
    candidates = list(COMMON_RULES) + [rule for rule in DISCIPLINE_RULES if rule["employee_type"] == kind]
    for rule in candidates:
        if annual_days >= rule["min_days"] and (rule["max_days"] is None or annual_days <= rule["max_days"]):
            result = _result(status="INDICATION", level=rule["level"], indication=rule["indication"],
                             article=rule["article"], employee_type=kind, annual_days=annual_days,
                             consecutive_days=consecutive_days)
            result.update(context)
            return result
    result = _result(
        status="REVIEW_REQUIRED", level=None, indication="Memerlukan verifikasi ketentuan",
        article=None, employee_type=kind, annual_days=annual_days, consecutive_days=consecutive_days,
    )
    result.update(context)
    return result


def calculate_ytd_tk(employee_rows: pd.DataFrame, year: int, active_month: str | int) -> int:
    """Jumlah TK Januari–periode aktif tanpa duplikasi tanggal/bulan."""
    if employee_rows.empty or "TK" not in employee_rows:
        return 0
    month_names = {name: index for index, name in enumerate(
        ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"], 1
    )}
    cutoff = int(active_month) if str(active_month).isdigit() else month_names.get(str(active_month), 0)
    source = employee_rows.copy()
    if "Tanggal" in source:
        source["Tanggal"] = pd.to_datetime(source["Tanggal"], errors="coerce").dt.normalize()
        source = source[source["Tanggal"].dt.year.eq(int(year)) & source["Tanggal"].dt.month.le(cutoff)]
        if {"wajib_presensi", "eligible_tk"}.issubset(source.columns):
            source = source[source["wajib_presensi"].fillna(False).astype(bool) & source["eligible_tk"].fillna(False).astype(bool)]
        else:
            return 0
        keys = [key for key in ["NIP", "Tanggal"] if key in source.columns]
        daily = source.groupby(keys, dropna=False)["TK"].max() if keys else source["TK"]
        return int(daily.fillna(False).astype(bool).sum())
    if not {"Tahun", "Bulan"}.issubset(source.columns):
        return 0
    source = source[pd.to_numeric(source["Tahun"], errors="coerce").eq(int(year))]
    source["_month"] = source["Bulan"].map(month_names).fillna(pd.to_numeric(source["Bulan"], errors="coerce"))
    source = source[source["_month"].between(1, cutoff)]
    keys = [key for key in ["NIP", "Tahun", "_month"] if key in source.columns]
    monthly = source.groupby(keys, dropna=False)["TK"].max() if keys else source["TK"]
    return int(pd.to_numeric(monthly, errors="coerce").fillna(0).sum())


def calculate_consecutive_unexcused_days(daily_rows: pd.DataFrame, year: int,
                                         active_month: str | int) -> int | None:
    """Streak TK pada urutan hari wajib presensi; non-workday tidak memutus streak."""
    if daily_rows.empty or "Tanggal" not in daily_rows or not ({"TK"} <= set(daily_rows.columns) or "Status" in daily_rows):
        return None
    source = daily_rows.copy()
    source["Tanggal"] = pd.to_datetime(source["Tanggal"], errors="coerce").dt.normalize()
    month_names = {name: index for index, name in enumerate(
        ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"], 1
    )}
    cutoff = int(active_month) if str(active_month).isdigit() else month_names.get(str(active_month), 0)
    source = source[source["Tanggal"].dt.year.eq(int(year)) & source["Tanggal"].dt.month.le(cutoff)]
    if "wajib_presensi" not in source or "eligible_tk" not in source:
        return None
    source = source[source["wajib_presensi"].fillna(False).astype(bool) & source["eligible_tk"].fillna(False).astype(bool)]
    if source.empty:
        return 0
    tk_values = source["TK"] if "TK" in source else source["Status"].astype(str).str.upper().eq("TK")
    source["_is_tk"] = tk_values.fillna(False).astype(bool)
    source = source.groupby("Tanggal", as_index=False)["_is_tk"].max().sort_values("Tanggal")
    # Missing required workdays break the streak; weekends and holidays do not.
    from services.work_calendar import build_work_calendar
    calendar = build_work_calendar(int(year), allow_pdf_extraction=False)
    calendar = calendar[calendar["tanggal"].dt.month.le(cutoff)]
    required_dates = calendar.loc[calendar["wajib_presensi"], "tanggal"]
    observed = source.set_index("Tanggal")["_is_tk"]
    values = observed.reindex(required_dates, fill_value=False)
    maximum = streak = 0
    for is_tk in values:
        streak = streak + 1 if is_tk else 0
        maximum = max(maximum, streak)
    return maximum
