import os
import json
import logging
import re
from datetime import date, datetime
from html import escape
from io import StringIO
from pathlib import Path

import altair as alt
import pandas as pd
import requests
import streamlit as st

from modules.excel_parser import build_dashboard_dataframe, load_semua_presensi
from modules.employee_type import add_employee_type_columns, resolve_employee_type
from modules.employee_active_status import apply_employee_active_status, get_employee_inactive_status
from modules.tk_report import (
    MONTH_SHORT,
    generate_tk_excel,
    generate_tk_filename,
    generate_tk_pdf,
    get_report_period_months,
    prepare_tk_report_data,
)
from modules.pp94 import (
    REGULATION_PP94,
    calculate_annual_tk,
    calculate_tk_by_month,
    evaluate_pp94_indicator,
    get_pp94_indication,
)
from modules.monitoring_rules import (
    MONITORING_DISCLAIMER,
    calculate_consecutive_unexcused_days,
    calculate_ytd_tk,
    evaluate_attendance_monitoring_rule,
)
from modules.analytics import (
    MONTH_ORDER as ANALYTICS_MONTHS,
    WARNING_STATUSES,
    apply_filters,
    dashboard_metrics,
    employee_summary,
    employee_warning_summary,
    ews_period_context,
    monthly_trend,
    opd_summary,
    warning_status,
)
from database.auth import authenticate_user, init_auth_schema, seed_default_users
from database.connection import get_engine
from services.data_source import active_source_name, load_daily_data as load_canonical_daily_data
from services.work_calendar import build_work_calendar, load_calendar_overrides, summarize_work_calendar_period
from services.activity_log import APP_TIMEZONE, load_system_activities, log_system_activity
from modules.attendance_compliance import calculate_monthly_attendance_compliance
from modules.attendance_trend import TREND_METRICS, aggregate_attendance_trend
from modules.attendance_indicators import prepare_daily_indicators, summarize_attendance_indicators
from modules.display import format_percentage_exact, safe_display
from modules.user_management import show_user_management_page
from modules.opd_management import show_opd_management_page
from modules.pegawai_management import show_pegawai_management_page
from modules.presensi_data_management import show_presensi_data_page

LOGGER = logging.getLogger(__name__)

st.set_page_config(
    page_title="EWS Kehadiran Pegawai",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

EXTRACTION_TOTAL_PAGES = 500
EXTRACTION_ANOMALY_PAGES = 10


def initialize_session() -> None:
    st.session_state.setdefault("is_logged_in", False)
    st.session_state.setdefault("username", "")
    st.session_state.setdefault("user_fullname", "")
    st.session_state.setdefault("user_role", "")
    st.session_state.setdefault("user_id", None)


def login(username: str, password: str) -> tuple[bool, str]:
    """Autentikasi pengguna menggunakan database PostgreSQL."""
    try:
        engine = get_engine()
        success, user_data, message = authenticate_user(engine, username, password)
        if success and user_data:
            st.session_state.is_logged_in = True
            st.session_state.username = user_data["username"]
            st.session_state.user_fullname = user_data.get("nama_lengkap") or user_data["username"]
            st.session_state.user_role = user_data.get("role", "admin")
            st.session_state.user_id = user_data.get("id")
            try:
                log_system_activity(
                    "auth",
                    "User Login",
                    f"Pengguna '{user_data['username']}' ({user_data.get('role', 'admin')}) berhasil login via PostgreSQL.",
                    metadata={"username": user_data["username"], "role": user_data.get("role")},
                )
            except Exception:
                pass
            return True, message
        return False, message
    except Exception as exc:
        LOGGER.warning("Gagal autentikasi via PostgreSQL: %s", exc)
        return False, f"Gagal terhubung ke database PostgreSQL: {exc}"


def logout() -> None:
    current_user = st.session_state.get("username", "")
    if current_user:
        try:
            log_system_activity(
                "auth",
                "User Logout",
                f"Pengguna '{current_user}' telah logout dari sistem.",
                metadata={"username": current_user},
            )
        except Exception:
            pass
    st.session_state.is_logged_in = False
    st.session_state.username = ""
    st.session_state.user_fullname = ""
    st.session_state.user_role = ""
    st.session_state.user_id = None
    st.rerun()


def get_active_opd_names() -> list[str]:
    """Ambil daftar nama/singkatan OPD aktif dari database PostgreSQL."""
    try:
        engine = get_engine()
        opds = list_opds(engine, aktif_only=True)
        names: list[str] = []
        for o in opds:
            if o.get("nama") and o["nama"] not in names:
                names.append(o["nama"])
            if o.get("singkatan") and o["singkatan"] not in names:
                names.append(o["singkatan"])
        if names:
            return names
    except Exception:
        pass
    return ["BAPPEDA", "BKAD", "BKD", "DISKOMINFO", "INSPEKTORAT", "ROHUKUM", "ROORGANISASI"]


def load_legacy_employee_data() -> pd.DataFrame:
    """Muat master data pegawai langsung dari database PostgreSQL."""
    try:
        engine = get_engine()
        peg_list = list_pegawai(engine, aktif_only=True)
        if peg_list:
            df = pd.DataFrame(peg_list)
            result = pd.DataFrame({
                "NIP": df["nip"].astype(str),
                "Nama Pegawai": df["nama_pegawai"].fillna("-"),
                "Unit Kerja": df["nama_opd"].fillna(df["singkatan_opd"]).fillna("-"),
                "Bulan": "Januari",
                "TK": 0,
                "Cuti": 0,
                "Terlambat": 0,
                "Hari Kerja": 22,
                "Jam Datang": 7.30,
                "Hari Dominan": "Senin",
                "WFH": 0,
                "DL": 0,
                "Jabatan": df.get("jabatan", "Pelaksana"),
                "Pangkat/Golongan": df.get("pangkat_golongan", "-"),
            })
            return result
    except Exception:
        pass
    return pd.DataFrame(columns=[
        "NIP", "Nama Pegawai", "Unit Kerja", "Bulan", "TK", "Cuti", "Terlambat",
        "Hari Kerja", "Jam Datang", "Hari Dominan", "WFH", "DL", "Jabatan", "Pangkat/Golongan"
    ])


MONTH_NAMES = {
    "01": "Januari", "02": "Februari", "03": "Maret", "04": "April",
    "05": "Mei", "06": "Juni", "07": "Juli", "08": "Agustus",
    "09": "September", "10": "Oktober", "11": "November", "12": "Desember",
}

OPD_AKTIF = get_active_opd_names()

class EPresensiAccessError(RuntimeError):
    """Kegagalan akses endpoint yang perlu diketahui pengguna."""


def ambil_presensi_pegawai(bulan, tahun, nip) -> pd.DataFrame:
    """Mengambil rekap harian tanpa cache selama fase debugging integrasi."""
    bulan = str(bulan).zfill(2)
    tahun = str(tahun)
    nip = str(nip).strip()
    url = f"https://epresensi.kalbarprov.go.id/rekapsemua/{bulan}/{tahun}/{nip}"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml,application/json",
    }

    with requests.Session() as session:
        response = session.get(url, headers=headers, timeout=30)

    if response.status_code == 403:
        raise EPresensiAccessError(
            "Endpoint ePresensi mengembalikan status 403. Server mungkin memerlukan "
            "header, cookie sesi browser, atau mekanisme resmi lainnya. Tidak ada "
            "upaya melewati autentikasi atau proteksi server."
        )
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()
    if "application/json" in content_type:
        payload = response.json()
        if isinstance(payload, dict):
            payload = payload.get("data", payload)
        raw = pd.json_normalize(payload) if payload else pd.DataFrame()
    else:
        try:
            tables = pd.read_html(StringIO(response.text))
        except ValueError:
            return pd.DataFrame()
        raw = tables[0] if tables else pd.DataFrame()

    if raw.empty:
        return pd.DataFrame()

    arrival = pd.to_datetime(raw.get("datang.datang"), errors="coerce")
    departure = pd.to_datetime(raw.get("pulang.pulang"), errors="coerce")
    shift_date = pd.to_datetime(raw.get("data_shift.jam_datang"), errors="coerce")
    result = pd.DataFrame(index=raw.index)
    result["NIP"] = raw.get("NIP", str(nip)).astype(str) if "NIP" in raw else str(nip)
    result["Nama"] = raw.get("nama", "-")
    result["OPD"] = raw.get("opd", "-")
    result["Tanggal"] = arrival.fillna(shift_date).dt.date
    result["Jam Masuk"] = arrival
    result["Jam Pulang"] = departure
    result["Status"] = raw.get("datang.sumber", "-")
    result["Keterlambatan"] = raw.get("datang.telat", "00:00")
    result["Hari"] = raw.get("hari", "-")
    return result.reset_index(drop=True)


# Nama fungsi pada integrasi awal tetap tersedia untuk kompatibilitas.
ambil_data_presensi = ambil_presensi_pegawai


def ambil_presensi_10_opd(
    bulan, tahun, df_pegawai: pd.DataFrame
) -> tuple[pd.DataFrame, list[dict]]:
    """Mengambil presensi master pegawai secara berurutan."""
    required = {"NIP", "Nama", "OPD"}
    missing = required.difference(df_pegawai.columns)
    if missing:
        raise ValueError(f"Kolom master pegawai belum lengkap: {', '.join(sorted(missing))}")

    active_opds = get_active_opd_names()
    pegawai_aktif = (
        df_pegawai[df_pegawai["OPD"].isin(active_opds) | df_pegawai["OPD"].fillna("").eq("")]
        .dropna(subset=["NIP"])
        .drop_duplicates("NIP")
    )
    semua_data = []
    gagal = []
    endpoint_urls = []
    for row in pegawai_aktif.itertuples(index=False):
        endpoint_urls.append(
            f"https://epresensi.kalbarprov.go.id/rekapsemua/"
            f"{str(bulan).zfill(2)}/{tahun}/{str(row.NIP)}"
        )
        try:
            presensi = ambil_presensi_pegawai(bulan, tahun, str(row.NIP))
            if presensi.empty:
                raise ValueError("Data presensi kosong")
            presensi = presensi.copy()
            api_nama = str(presensi["Nama"].iloc[0])
            api_opd = str(presensi["OPD"].iloc[0])
            presensi["NIP"] = str(row.NIP)
            presensi["Nama"] = row.Nama if str(row.Nama).strip() else api_nama
            presensi["OPD"] = row.OPD if str(row.OPD).strip() else api_opd
            semua_data.append(presensi)
        except Exception as exc:
            gagal.append({
                "nip": str(row.NIP), "nama": row.Nama,
                "opd": row.OPD, "error": str(exc),
            })

    df_presensi = (
        pd.concat(semua_data, ignore_index=True)
        if semua_data else pd.DataFrame(columns=[
            "NIP", "Nama", "OPD", "Tanggal", "Jam Masuk", "Jam Pulang",
            "Status", "Keterlambatan", "Hari",
        ])
    )
    df_presensi.attrs["endpoint_urls"] = endpoint_urls
    return df_presensi, gagal


def _duration_is_late(value) -> bool:
    if pd.isna(value):
        return False
    parts = str(value).strip().split(":")
    try:
        return any(int(float(part)) > 0 for part in parts)
    except ValueError:
        return False


def _epresensi_to_dashboard(raw: pd.DataFrame, bulan: str, nip: str) -> pd.DataFrame:
    """Menyesuaikan hasil ePresensi dengan kontrak kolom dashboard lama."""
    if raw.empty:
        return pd.DataFrame()

    source = raw.get("Status", pd.Series("", index=raw.index)).fillna("").astype(str).str.upper()
    arrival = pd.to_datetime(raw.get("Jam Masuk"), errors="coerce")
    late = raw.get("Keterlambatan", pd.Series("00:00", index=raw.index)).map(_duration_is_late)
    day_map = {"Sen": "Senin", "Sel": "Selasa", "Rab": "Rabu", "Kam": "Kamis", "Jum": "Jumat"}
    days = raw.get("Hari", pd.Series("", index=raw.index)).map(day_map).fillna("-")
    late_days = days[late]
    dominant_day = late_days.mode().iloc[0] if not late_days.empty else "-"

    is_wfh = source.str.contains("WFH", na=False)
    is_dl = source.str.contains(r"DINAS LUAR|\bDL\b", regex=True, na=False)
    is_leave = source.str.contains(r"CUTI|IZIN|SAKIT", regex=True, na=False)
    is_tk = source.str.contains(r"TANPA KETERANGAN|\bTK\b", regex=True, na=False) | (
        arrival.isna() & ~(is_wfh | is_dl | is_leave)
    )
    actual_arrival = arrival[~(is_wfh | is_dl | is_leave | is_tk)]
    average_arrival = (
        actual_arrival.dt.hour.add(actual_arrival.dt.minute.div(60)).mean()
        if not actual_arrival.empty else 0.0
    )

    first = raw.iloc[0]
    record = {
        "NIP": str(first.get("NIP", nip)),
        "Nama Pegawai": str(first.get("Nama", "-")),
        "Unit Kerja": str(first.get("OPD", "-")),
        "Bulan": MONTH_NAMES[str(bulan).zfill(2)],
        "TK": int(is_tk.sum()),
        "Cuti": int(is_leave.sum()),
        "Terlambat": int(late.sum()),
        "Hari Kerja": int(len(raw)),
        "Jam Datang": round(float(average_arrival), 2),
        "Hari Dominan": dominant_day,
        "WFH": int(is_wfh.sum()),
        "DL": int(is_dl.sum()),
        "Jabatan": "-",
        "Pangkat/Golongan": "-",
    }
    return pd.DataFrame([record])


def _master_pegawai_lama() -> pd.DataFrame:
    try:
        engine = get_engine()
        peg_list = list_pegawai(engine, aktif_only=True)
        if peg_list:
            df = pd.DataFrame(peg_list)
            return pd.DataFrame({
                "NIP": df["nip"].astype(str),
                "Nama": df["nama_pegawai"].fillna("-"),
                "OPD": df["nama_opd"].fillna(df["singkatan_opd"]).fillna("-"),
            }).drop_duplicates("NIP")
    except Exception:
        pass
    return pd.DataFrame(columns=["NIP", "Nama", "OPD"])


def _master_pegawai_aktif() -> pd.DataFrame:
    """Memakai NIP input pengguna tanpa kembali ke master simulasi."""
    input_nips = st.session_state.get("epresensi_nips", "")
    nips = [nip.strip() for nip in input_nips.replace("\n", ",").split(",") if nip.strip()]
    if nips:
        return pd.DataFrame({"NIP": nips, "Nama": "", "OPD": ""}).drop_duplicates("NIP")
    return pd.DataFrame(columns=["NIP", "Nama", "OPD"])


def _excel_source_signature() -> tuple[tuple[str, int, int], ...]:
    """Fingerprint berkas / database agar cache diperbarui ketika data berubah."""
    try:
        if active_source_name() == "PostgreSQL":
            engine = get_engine()
            with engine.connect() as conn:
                from database.repository import presensi_harian
                cnt = conn.execute(select(func.count(presensi_harian.c.id))).scalar() or 0
                max_upd = conn.execute(select(func.max(presensi_harian.c.updated_at))).scalar()
                max_str = str(max_upd) if max_upd else "init"
                return (("postgres", int(cnt), int(hash(max_str))),)
    except Exception:
        pass

    from pathlib import Path

    return tuple(
        (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        for path in sorted(Path("data").rglob("*.xlsx"))
    )


@st.cache_data(show_spinner="Memuat data presensi...")
def _load_excel_daily_data(
    signature: tuple[tuple[str, int, int], ...],
) -> pd.DataFrame:
    """Data harian ter-cleaning; menjadi sumber cache agregat dan detail."""
    # ``signature`` sengaja menjadi argumen cache agar penambahan/ubah file
    # Excel memicu pembacaan ulang tanpa perlu mengubah UI.
    source_signature = signature
    result = load_canonical_daily_data()
    if not result.empty:
        opd_count = int(result.get("Unit Kerja", pd.Series(dtype=str)).dropna().nunique())
        log_system_activity(
            "DATA_LOAD_SUCCESS", "Data presensi berhasil dimuat",
            f"{len(result):,} record • {opd_count} OPD".replace(",", "."),
            {"records": len(result), "opd_count": opd_count}, module="presensi",
            dedupe_key=f"data-load:{source_signature!r}",
        )
    return result


@st.cache_data(show_spinner="Menyusun rekap presensi...")
def _load_excel_dashboard_data(
    signature: tuple[tuple[str, int, int], ...],
) -> pd.DataFrame:
    """Agregasi dashboard dibentuk dari cache harian yang sama."""
    result = build_dashboard_dataframe(_load_excel_daily_data(signature))
    if not result.empty:
        log_system_activity(
            "DATA_PROCESS_SUCCESS", "Data presensi berhasil diproses",
            f"{len(result):,} record siap dianalisis".replace(",", "."),
            {"records": len(result)}, module="presensi",
            dedupe_key=f"data-process:{signature!r}",
        )
    return result


def load_employee_data() -> pd.DataFrame:
    """Sumber utama dashboard: seluruh laporan Excel di folder data/."""
    result = _load_excel_dashboard_data(_excel_source_signature()).copy()
    # Cache dari kontrak DataFrame versi lama harus dibangun ulang. Tanpa cek
    # Jenis Pegawai, cache lama membuat filter Executive masuk ke fallback.
    required_columns = {
        "NIP", "Tahun", "Bulan", "Unit Kerja",
        "Jenis Pegawai", "Jenis Pegawai Source",
    }
    if not required_columns.issubset(result.columns):
        LOGGER.info(
            "Cache dashboard tidak kompatibel; membangun ulang kolom: %s",
            sorted(required_columns.difference(result.columns)),
        )
        _load_excel_dashboard_data.clear()
        result = build_dashboard_dataframe(_load_excel_daily_data(_excel_source_signature()))
    # Guard terakhir menjaga kontrak ketika sumber agregat lain dipakai pada
    # masa transisi. Deteksi tetap dilakukan sekali per NIP oleh helper pusat.
    if "Jenis Pegawai" not in result.columns or result["Jenis Pegawai"].isna().all():
        result = add_employee_type_columns(result)
    return result


def _daily_attendance_scope(*, year=None, month=None, opd=None, employee_type=None,
                            allowed_nips: set[str] | None = None) -> pd.DataFrame:
    """Apply UI scope to canonical daily rows without changing their values."""
    result = apply_employee_active_status(_load_excel_daily_data(_excel_source_signature()))
    if year not in (None, "Semua Tahun"):
        result = result[pd.to_numeric(result["Tahun"], errors="coerce").eq(int(year))]
    if month not in (None, "Semua Bulan"):
        result = result[result["Nama_Bulan"].eq(month)]
    if opd not in (None, "Semua OPD"):
        result = result[result["Unit Kerja"].eq(opd)]
    if employee_type not in (None, "Semua Jenis Pegawai"):
        result = result[result["Jenis Pegawai"].eq(employee_type)]
    if allowed_nips is not None:
        result = result[result["NIP"].astype(str).isin(allowed_nips)]
    return result


def available_months(data: pd.DataFrame) -> list[str]:
    present = set(data["Bulan"].dropna().astype(str))
    return [name for name in MONTH_NAMES.values() if name in present]


def aggregate_risk_by_employee(data: pd.DataFrame) -> pd.DataFrame:
    """Gabungkan rekap bulanan menjadi satu baris EWS per NIP."""
    if data.empty:
        return data.copy()

    aggregations = {
        "TK": "sum", "Cuti": "sum", "Terlambat": "sum", "Hari Kerja": "sum",
        "WFH": "sum", "DL": "sum", "Jam Datang": "mean",
        "Jabatan": "first", "Pangkat/Golongan": "first",
        "Hari Dominan": lambda values: values.replace("-", pd.NA).mode().iloc[0]
        if not values.replace("-", pd.NA).mode().empty else "-",
    }
    group_keys = ["NIP", "Nama Pegawai", "Unit Kerja"]
    if "Tahun" in data.columns:
        group_keys.append("Tahun")
    result = data.groupby(
        group_keys, as_index=False, dropna=False
    ).agg(aggregations)
    result["Bulan"] = "Semua Bulan"
    result["Jam Datang"] = result["Jam Datang"].round(2)
    return result


def get_warning_reason(record: pd.Series) -> str:
    """Satu atau dua alasan ringkas yang aman untuk kartu EWS."""
    reasons = []
    if int(record.get("TK", 0)) > 0:
        reasons.append("TK berulang")
    if int(record.get("Terlambat", 0)) > 0:
        reasons.append("keterlambatan meningkat")
    return " • ".join(reasons[:2]) or "Perlu pemantauan presensi"


def get_followup_recommendation(status: str) -> str:
    mapping = {
        "Kritis": "Evaluasi lebih lanjut dan verifikasi data presensi.",
        "Tinggi": "Konfirmasi data presensi dan tindak lanjut oleh pengelola kepegawaian.",
        "Waspada": "Peringatan awal dan monitoring berkala.",
        "Normal": "Monitoring rutin.",
    }
    return mapping.get(status, "Monitoring rutin.")


def prepare_ews_data(source: pd.DataFrame) -> pd.DataFrame:
    """Legacy - not used by active UI; retained for compatibility."""
    result = aggregate_risk_by_employee(source)
    if result.empty:
        return result
    result["Risk Score"] = result.apply(
        lambda row: calculate_risk_score(row["TK"], row["Terlambat"]), axis=1
    )
    result["Status EWS"] = result["Risk Score"].map(risk_status)
    result["Penyebab Utama"] = result.apply(get_warning_reason, axis=1)
    result["Rekomendasi"] = result["Status EWS"].map(get_followup_recommendation)
    return result


def classify_ews(tk: int) -> tuple[str, str]:
    if tk >= 6:
        return "Risiko Tinggi", "🔴"
    if tk >= 3:
        return "Peringatan Dini", "🟠"
    return "Aman", "🟢"


def sanction_recommendation(tk: int) -> str:
    if tk >= 6:
        return "Rekomendasi pemeriksaan disiplin / sanksi administratif"
    if tk >= 3:
        return "Teguran tertulis dan pembinaan atasan langsung"
    return "Monitoring rutin"


def leave_recommendation(cuti: int) -> str:
    if cuti >= 3:
        return "Verifikasi kelengkapan dokumen dan persetujuan cuti"
    return "Administrasi cuti telah dicatat"


def tardiness_recommendation(tardy_count: int) -> str:
    if tardy_count >= 12:
        return "Teguran tertulis dan evaluasi atasan langsung"
    if tardy_count >= 8:
        return "Pembinaan kedisiplinan kehadiran"
    return "Monitoring rutin"


def discipline_zone(tk: int) -> str | None:
    if 3 <= tk <= 10:
        return "Ambang Monitoring 3–10 Hari"
    if 11 <= tk <= 20:
        return "Ambang Monitoring 11–20 Hari"
    if tk >= 21:
        return "Prioritas Verifikasi ≥21 Hari"
    return None


def calculate_risk_score(tk: int, late_count: int) -> int:
    """Legacy - not used by active UI; retained for compatibility."""
    del late_count
    return min(tk * 8, 100)


def risk_status(score: int) -> str:
    """Legacy - not used by active UI; retained for compatibility."""
    if score >= 75:
        return "Kritis"
    if score >= 50:
        return "Tinggi"
    if score >= 25:
        return "Waspada"
    return "Normal"


def warning_indicators(tk: int, late_count: int) -> str:
    indicators = []
    if tk >= 3:
        indicators.append(f"TK {tk} hari")
    if late_count >= 8:
        indicators.append(f"Terlambat {late_count} kali")
    return ", ".join(indicators) if indicators else "Tidak ada indikator warning"


def risk_recommendation(status: str) -> str:
    recommendations = {
        "Kritis": "Panggilan klarifikasi dan pemeriksaan disiplin segera",
        "Tinggi": "Teguran tertulis serta pembinaan oleh atasan langsung",
        "Waspada": "Konseling kedisiplinan dan monitoring mingguan",
        "Normal": "Monitoring rutin",
    }
    return recommendations[status]


def build_attendance_calendar(record: pd.Series) -> pd.DataFrame:
    workdays = int(record["Hari Kerja"])
    statuses = (
        ["TK"] * int(record["TK"])
        + ["Cuti"] * int(record["Cuti"])
        + ["WFH"] * int(record["WFH"])
        + ["DL"] * int(record["DL"])
    )
    statuses += ["Hadir"] * max(workdays - len(statuses), 0)
    statuses = statuses[:workdays]
    weekdays = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat"]
    calendar_rows = []
    for start in range(0, workdays, len(weekdays)):
        row = {"Minggu": f"Minggu {start // len(weekdays) + 1}"}
        for day_offset, weekday in enumerate(weekdays):
            index = start + day_offset
            row[weekday] = f"{index + 1}: {statuses[index]}" if index < workdays else "-"
        calendar_rows.append(row)
    return pd.DataFrame(calendar_rows)


def inject_dashboard_css() -> None:
    st.markdown(
        """
        <style>
            header[data-testid="stHeader"] { background: #f7f9fc; }
            .stApp { background: #f7f9fc; color: #172033; }
            [data-testid="stSidebar"] { background: #0f2942; border-right: 1px solid rgba(148,163,184,.18); }
            [data-testid="stSidebar"] * { color: #f8fafc !important; }
            [data-testid="stSidebar"] .stSelectbox div[data-baseweb="select"] > div,
            [data-testid="stSidebar"] input { background: #173b5e !important; border-color: rgba(148,163,184,.28) !important; }
            [data-testid="stSidebar"] hr { border-color: rgba(148,163,184,.2); margin: .55rem 0; }
            .sidebar-brand { display:flex; align-items:center; gap:.65rem; padding:.25rem .15rem .7rem; }
            .sidebar-brand .brand-icon { display:grid; place-items:center; width:30px; height:30px; font-size:1.35rem; }
            .sidebar-brand .brand-name { font-size:1.02rem; font-weight:800; letter-spacing:.08em; line-height:1.1; }
            .sidebar-brand .brand-subtitle { color:#94a3b8 !important; font-size:.68rem; margin-top:.2rem; }
            .sidebar-nav-title, [data-testid="stSidebar"] .sidebar-filter-title { color:#94a3b8 !important; text-transform:uppercase; letter-spacing:.1em; font-size:.68rem; font-weight:700; margin:.25rem 0 .35rem; }
            [data-testid="stSidebar"] [data-testid="stRadioGroup"] { gap:2px; width:100%; align-items:flex-start; }
            [data-testid="stSidebar"] [data-testid="stRadioOption"] {
                display:flex; align-items:center; gap:0 !important; width:max-content; max-width:100%; min-height:30px;
                box-sizing:border-box; border-radius:8px; padding:6px 10px; margin:0;
                background:transparent; color:#f8fafc !important;
                transition:background .15s ease, box-shadow .15s ease;
            }
            [data-testid="stSidebar"] [data-testid="stRadioOption"]:hover {
                background:rgba(255,255,255,.08);
            }
            [data-testid="stSidebar"] [data-testid="stRadioOption"][data-selected="true"] {
                background:#2563eb; box-shadow:0 3px 10px rgba(37,99,235,.28); font-weight:600;
            }
            [data-testid="stSidebar"] [data-testid="stRadioOption"][data-selected="true"] * { color:#ffffff !important; }
            [data-testid="stSidebar"] [data-testid="stRadioOption"]:focus-within {
                outline:2px solid #93c5fd; outline-offset:2px;
            }
            [data-testid="stSidebar"] [data-testid="stRadioOption"] > div > div:first-child {
                display:none !important; width:0 !important; min-width:0 !important;
                height:0 !important; margin:0 !important; padding:0 !important;
            }
            [data-testid="stSidebar"] [data-testid="stRadioOption"] > div {
                display:flex; align-items:center; min-width:0; margin:0 !important; line-height:1.2; white-space:normal;
                gap:0 !important; font-size:.78rem;
            }
            [data-testid="stSidebar"] [data-testid="stRadioOption"] p { margin:0 !important; }
            [data-testid="stSidebar"] .stSelectbox, [data-testid="stSidebar"] .stTextInput { margin-bottom:.35rem; }
            [data-testid="stSidebar"] .stSelectbox label, [data-testid="stSidebar"] .stTextInput label { color:#cbd5e1 !important; font-size:.75rem; }
            [data-testid="stSidebar"] .stButton button { background:#2563eb; border:0; border-radius:8px; font-weight:700; }
            .block-container { padding-top: 1.6rem; padding-bottom: 2rem; }
            .dashboard-title { font-size: 2.35rem; line-height: 1.15; font-weight: 800; color: #102a43; text-align: center; letter-spacing: -.02em; margin: .2rem 0 .35rem; }
            .dashboard-subtitle { color: #64748b; text-align: center; font-size: .98rem; margin: 0 0 1.5rem; }
            .section-title { color: #173b61; font-size: 1.1rem; font-weight: 700; margin: 1.3rem 0 .45rem; }
            .ews-card { padding: 1rem 1.15rem; border-radius: 12px; min-height: 116px; border: 1px solid; }
            .ews-card .label { font-size: .87rem; font-weight: 650; opacity: .82; }
            .ews-card .number { font-size: 2rem; font-weight: 800; line-height: 1.25; }
            .ews-card .detail { font-size: .8rem; opacity: .8; }
            .red { background: #fff1f2; border-color: #fecdd3; color: #9f1239; }
            .yellow { background: #fefce8; border-color: #fde68a; color: #854d0e; }
            .orange { background: #fff7ed; border-color: #fed7aa; color: #9a3412; }
            .green { background: #ecfdf5; border-color: #a7f3d0; color: #065f46; }
            .legend { display: flex; gap: 16px; flex-wrap: wrap; color: #475569; font-size: .85rem; padding: .35rem 0 .8rem; }
            .legend span { display: inline-flex; align-items: center; gap: 6px; }
            .legend i { height: 10px; width: 10px; border-radius: 50%; display: inline-block; }
            .note { color: #64748b; font-size: .8rem; margin-top: -.3rem; }
            .stDataFrame { border: 1px solid #e2e8f0; border-radius: 8px; }
            .discipline-funnel { display: flex; flex-direction: column; align-items: center; gap: 7px; padding: .75rem 0; }
            .funnel-level { min-height: 62px; display: flex; align-items: center; justify-content: space-between; gap: .8rem; padding: .7rem 1rem; color: white; border-radius: 8px; box-sizing: border-box; }
            .funnel-level strong, .funnel-level span { color: white; }
            .funnel-level span { font-size: .86rem; text-align: right; }
            .funnel-light { background: #f59e0b; }
            .funnel-medium { background: #f97316; }
            .funnel-heavy { background: #dc2626; }
            .target-kpi-card { background: #111827; color: #f8fafc; border-radius: 10px; padding: 1.15rem 1.25rem; }
            .target-kpi-card .kpi-title { color: #f8fafc; font-size: 1.05rem; font-weight: 700; margin-bottom: .65rem; }
            .target-kpi-card p { color: #f8fafc; font-size: 1rem; margin: .3rem 0; }
            .target-kpi-card .kpi-gap { font-weight: 700; }
            .target-kpi-card .kpi-dot { display: inline-block; width: 14px; height: 14px; border-radius: 50%; margin-left: 6px; vertical-align: -1px; }
            .target-kpi-card .negative { background: #f43f5e; }
            .target-kpi-card .positive { background: #22c55e; }
            .composition-list { display: grid; grid-template-columns: repeat(4, minmax(130px, 1fr)); gap: .65rem; width: 100%; }
            .composition-row { display: flex; flex-direction: column; justify-content: center; min-height: 66px; padding: .65rem .8rem; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; }
            .composition-row .composition-label { color: #475569; font-size: .92rem; }
            .composition-row .composition-value { color: #102a43; font-size: 1rem; font-weight: 700; }
            .composition-bars { margin-top: .8rem; }
            .composition-bar-row { display: grid; grid-template-columns: 110px 1fr 55px; align-items: center; gap: .55rem; margin: .45rem 0; }
            .composition-bar-label, .composition-bar-value { color: #475569; font-size: .82rem; }
            .composition-bar-value { color: #102a43; font-weight: 700; text-align: right; }
            .composition-bar-track { height: 12px; background: #e2e8f0; border-radius: 99px; overflow: hidden; }
            .composition-bar-fill { height: 100%; border-radius: 99px; }
            .individual-kpi-grid { display: grid; grid-template-columns: repeat(5, minmax(130px, 1fr)); gap: .65rem; }
            .individual-kpi-card { min-width: 0; padding: .75rem .8rem; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; }
            .individual-kpi-card .label { color: #64748b; font-size: .78rem; margin-bottom: .3rem; }
            .individual-kpi-card .value { color: #102a43; font-size: 1rem; font-weight: 700; line-height: 1.25; overflow-wrap: anywhere; }
            .page-context-grid { display: grid; grid-template-columns: 1fr; justify-items: center; gap: .6rem; margin: .25rem 0 1.25rem; }
            .page-context-card { width: fit-content; max-width: 100%; box-sizing: border-box; padding: .8rem 1rem; border: 1px solid #dbe4ee; border-radius: 9px; background: #ffffff; color: #64748b; font-size: .92rem; text-align: center; white-space: nowrap; }
            .page-context-card.risk-context-card { color: #102a43; font-size: 1.05rem; font-weight: 750; }
            .hero-subtitle { color:#64748b; text-align:center; font-size:1rem; margin:-.15rem 0 .8rem; }
            .hero-filters { display:flex; justify-content:center; gap:.7rem; flex-wrap:wrap; margin:0 auto 1.15rem; }
            .hero-filter-chip { background:#fff; border:1px solid #dbe4ee; border-radius:8px; padding:.5rem .9rem; color:#334e68; font-size:.84rem; box-shadow:0 2px 7px rgba(15,41,66,.05); }
            .hero-filter-chip strong { color:#102a43; }
            .hero-kpi-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:.8rem; margin-bottom:1.25rem; }
            .hero-kpi-card { display:flex; align-items:center; gap:.65rem; min-width:0; padding:.9rem 1rem; background:#fff; border:1px solid #dbe4ee; border-radius:11px; box-shadow:0 3px 12px rgba(15,41,66,.06); }
            .hero-kpi-icon { font-size:1.35rem; line-height:1; }
            .hero-kpi-value { color:#102a43; font-size:1.42rem; font-weight:800; line-height:1.1; }
            .hero-kpi-label { color:#64748b; font-size:.78rem; margin-top:.2rem; white-space:nowrap; }
            .hero-kpi-card.warning { border-left:4px solid #f59e0b; }
            .hero-kpi-card.critical { border-left:4px solid #ef4444; }
            .trend-target-note { margin-top:.35rem; text-align:center; color:#64748b; font-size:.82rem; }
            .trend-target-note span { color:#f59e0b; margin:0 .35rem; }
            .trend-summary-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:.65rem; margin:.2rem 0 .75rem; }
            .trend-summary-card { background:#fff; border:1px solid #dbe4ee; border-radius:9px; padding:.65rem .8rem; text-align:center; box-shadow:0 2px 8px rgba(15,41,66,.04); }
            .trend-summary-label { color:#64748b; font-size:.74rem; }
            .trend-summary-value { color:#173b5e; font-size:1.12rem; font-weight:800; margin-top:.15rem; }
            .trend-summary-value.warning { color:#c2410c; }
            .trend-summary-value.positive { color:#047857; }
            .trend-summary-value.negative { color:#dc2626; }
            .ews-page-title { display:block !important; visibility:visible !important; color:#173b63 !important; font-size:1.7rem !important; font-weight:800 !important; line-height:1.2; margin:.1rem 0 .15rem; text-align:left; }
            .ews-page-subtitle { color:#64748b; font-size:.92rem; margin:0 0 .7rem; }
            .ews-filter-row { display:flex; justify-content:space-between; align-items:center; gap:.7rem; flex-wrap:wrap; padding:.55rem .8rem; border-top:1px solid #dbe4ee; border-bottom:1px solid #dbe4ee; margin-bottom:1rem; color:#64748b; font-size:.84rem; }
            .ews-filter-chip { background:#fff; border:1px solid #dbe4ee; border-radius:7px; padding:.38rem .75rem; }
            .ews-status-banner { background:#fff7ed; border:1px solid #fed7aa; border-left:4px solid #f97316; border-radius:9px; padding:.75rem 1rem; margin:.25rem 0 1rem; }
            .ews-status-banner .status-label { color:#c2410c; font-size:.78rem; font-weight:800; letter-spacing:.06em; }
            .ews-status-banner .status-value { color:#9a3412; font-size:1.2rem; font-weight:800; margin:.15rem 0; }
            .ews-status-banner .status-detail { color:#7c2d12; font-size:.84rem; }
            .ews-header-risk { background:#ecfdf5; border:1px solid #a7f3d0; border-left:4px solid #10b981; border-radius:9px; padding:.7rem 1rem; margin:.2rem 0 .85rem; }
            .ews-header-risk .risk-label { color:#047857; font-size:.88rem; font-weight:800; }
            .ews-header-risk .risk-count { color:#065f46; font-size:1.12rem; font-weight:800; margin:.15rem 0; }
            .ews-header-risk .risk-change { color:#047857; font-size:.82rem; }
            .ews-risk-summary .ews-card { min-height:100px; background:#fff !important; }
            .ews-risk-summary .green { border-top:3px solid #10b981; }
            .ews-risk-summary .yellow { border-top:3px solid #f59e0b; }
            .ews-risk-summary .orange { border-top:3px solid #f97316; }
            .ews-risk-summary .red { border-top:3px solid #e11d48; }
            .ews-risk-summary .detail { font-size:.78rem; }
            .verified-badge { display:inline-block; padding:.35rem .75rem; border-radius:999px; background:#dbeafe; color:#1d4ed8; font-weight:800; font-size:.8rem; margin:.2rem 0 .55rem; }
            @media (max-width: 700px) { .trend-summary-grid { grid-template-columns:1fr; } }
            @media (max-width: 900px) { .hero-kpi-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } }
            @media (max-width: 900px) { .individual-kpi-grid { grid-template-columns: repeat(2, minmax(130px, 1fr)); } .composition-list { grid-template-columns: repeat(2, minmax(130px, 1fr)); } }
        </style>
        """,
        unsafe_allow_html=True,
    )


def show_login_page() -> None:
    st.markdown(
        """
        <style>
            .stApp { background: linear-gradient(135deg, #0b2545, #1d4e75); }
            header[data-testid="stHeader"] { background: transparent; }
            .login-box { box-sizing: border-box; width: min(92vw, 440px); margin: 9vh auto 0; padding: 2rem 2.3rem 1rem; background: white; border-radius: 16px 16px 0 0; box-shadow: 0 16px 45px rgba(0,0,0,.2); }
            .login-box h1 { color: #102a43; font-size: 1.65rem; margin-bottom: .2rem; }
            .login-box p { color: #64748b; font-size: 0.95rem; margin-bottom: 0.4rem; }
            .db-badge { display: inline-flex; align-items: center; gap: 5px; font-size: 0.75rem; background: #e0f2fe; color: #0369a1; padding: 3px 8px; border-radius: 999px; font-weight: 600; margin-bottom: 0.5rem; }
            div[data-testid="stForm"] { box-sizing: border-box; width: min(92vw, 440px); margin: 0 auto; padding: 0.8rem 2.3rem 1.8rem; border: 0; border-radius: 0 0 16px 16px; background: white; box-shadow: 0 16px 45px rgba(0,0,0,.2); }
            div[data-testid="stForm"] > div { border: 0; padding: 0; }
            .demo-info-card { width: min(92vw, 440px); margin: 1.2rem auto 0; padding: 0.9rem 1.2rem; background: rgba(255, 255, 255, 0.12); border: 1px solid rgba(255, 255, 255, 0.2); border-radius: 12px; color: #f1f5f9; font-size: 0.82rem; backdrop-filter: blur(8px); }
            .demo-info-card strong { color: #38bdf8; }
        </style>
        <div class="login-box">
            <span class="db-badge">🐘 PostgreSQL Database Auth</span>
            <h1>🛡️ EWS Kehadiran</h1>
            <p>Masuk untuk memantau disiplin kehadiran pegawai berbasis database PostgreSQL.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.form("login_form"):
        username = st.text_input("Username", placeholder="Masukkan username")
        password = st.text_input("Kata sandi", type="password", placeholder="Masukkan kata sandi")
        submitted = st.form_submit_button("Masuk ke Sistem", use_container_width=True)
    if submitted:
        success, message = login(username, password)
        if success:
            st.rerun()
        else:
            st.error(message)

    st.markdown(
        """
        <div class="demo-info-card">
            <div style="font-weight: 600; margin-bottom: 4px; color: #e2e8f0;">🔑 Akun Database Tersedia:</div>
            <ul style="margin: 0; padding-left: 1.2rem; line-height: 1.5;">
                <li><strong>admin</strong> / <code>admin123</code> (Administrator EWS)</li>
                <li><strong>operator</strong> / <code>operator123</code> (Operator Presensi)</li>
                <li><strong>pimpinan</strong> / <code>pimpinan123</code> (Pimpinan Eksekutif)</li>
            </ul>
        </div>
        """,
        unsafe_allow_html=True,
    )


def show_dashboard_legacy() -> None:
    inject_dashboard_css()
    data = load_employee_data()
    month_order = ["Januari", "Februari", "Maret"]

    with st.sidebar:
        st.markdown("# 🛡️ EWS Kehadiran")
        st.caption("Sistem peringatan dini disiplin pegawai")
        st.divider()
        st.markdown("#### Filter Data")
        chosen_month = st.selectbox("Periode bulan", month_order)
        violation_options = {
            "Semua jenis": None,
            "Tanpa Keterangan (TK)": "TK",
            "Cuti": "Cuti",
            "Terlambat": "Terlambat",
        }
        chosen_violation = st.selectbox("Jenis ketidakhadiran", violation_options.keys())
        units = ["Semua Unit"] + sorted(data["Unit Kerja"].unique().tolist())
        chosen_unit = st.selectbox("Unit kerja", units)
        search_name = st.text_input("Cari nama atau NIP", placeholder="Ketik untuk mencari")
        st.divider()
        st.markdown("#### Keterangan Status")
        st.markdown("🔴 **Risiko Tinggi**  \\n+TK ≥ 6 hari")
        st.markdown("🟠 **Peringatan Dini**  \\n+TK 3–5 hari")
        st.markdown("🟢 **Aman**  \\n+TK 0–2 hari")
        st.divider()
        st.caption(f"Login sebagai: {st.session_state.username}")
        st.button("Keluar", on_click=logout, use_container_width=True)

    filtered = data[data["Bulan"] == chosen_month].copy()
    violation_column = violation_options[chosen_violation]
    if violation_column:
        filtered = filtered[filtered[violation_column] > 0]
    if chosen_unit != "Semua Unit":
        filtered = filtered[filtered["Unit Kerja"] == chosen_unit]
    if search_name:
        query = search_name.lower()
        filtered = filtered[
            filtered["Nama Pegawai"].str.lower().str.contains(query)
            | filtered["NIP"].str.lower().str.contains(query)
        ]

    filtered[["Status", "Ikon"]] = filtered["TK"].apply(lambda value: pd.Series(classify_ews(value)))
    filtered["Rekomendasi"] = filtered["TK"].apply(sanction_recommendation)

    st.markdown("<h1 class='dashboard-title' style='display:block!important;visibility:visible!important;color:#102a43!important;text-align:center!important;font-size:2.35rem!important;font-weight:800!important;'>Executive Dashboard</h1>", unsafe_allow_html=True)
    st.markdown(f"<p class='dashboard-subtitle'>Monitoring kehadiran pegawai • Periode: {chosen_month}</p>", unsafe_allow_html=True)

    total_employees = filtered["NIP"].nunique()
    total_workdays = int(filtered["Hari Kerja"].sum())
    total_tk = int(filtered["TK"].sum())
    total_leave = int(filtered["Cuti"].sum())
    attendance_rate = (
        max(total_workdays - total_tk - total_leave, 0) / total_workdays * 100
        if total_workdays
        else 0
    )
    total_late = int(filtered["Terlambat"].sum())
    at_risk = int(filtered["Status"].isin(["Risiko Tinggi", "Peringatan Dini"]).sum())

    st.markdown("<div class='section-title'>Ringkasan Eksekutif</div>", unsafe_allow_html=True)
    executive_1, executive_2, executive_3, executive_4, executive_5 = st.columns(5)
    executive_1.metric("Total Pegawai", f"{total_employees}")
    executive_2.metric("Persentase Kehadiran", f"{attendance_rate:.1f}%")
    executive_3.metric("TK", f"{total_tk} hari", help="Total hari tanpa keterangan pada data terfilter.")
    executive_4.metric("Keterlambatan", f"{total_late} kali")
    executive_5.metric("Pegawai Berisiko", f"{at_risk}", help="Pegawai berstatus Risiko Tinggi atau Peringatan Dini.")

    trend_source = data if chosen_unit == "Semua Unit" else data[data["Unit Kerja"] == chosen_unit]
    attendance_trend = trend_source.groupby("Bulan").apply(
        lambda group: max(group["Hari Kerja"].sum() - group["TK"].sum() - group["Cuti"].sum(), 0)
        / group["Hari Kerja"].sum()
        * 100,
        include_groups=False,
    ).reindex(month_order).rename("Persentase Kehadiran")
    opd_ranking = filtered.groupby("Unit Kerja").agg(
        Pegawai=("NIP", "nunique"),
        Hari_Kerja=("Hari Kerja", "sum"),
        TK_TB=("TK", "sum"),
        Cuti=("Cuti", "sum"),
    )
    opd_ranking["Kehadiran"] = (
        (opd_ranking["Hari_Kerja"] - opd_ranking["TK_TB"] - opd_ranking["Cuti"])
        / opd_ranking["Hari_Kerja"]
        * 100
    ).clip(lower=0)
    opd_ranking = opd_ranking.sort_values("Kehadiran", ascending=False).reset_index()
    opd_ranking.index = opd_ranking.index + 1
    opd_ranking.index.name = "Peringkat"

    executive_left, executive_right = st.columns([1.25, 1])
    with executive_left:
        st.caption("Tren Kehadiran — persentase kehadiran bulanan (semakin tinggi semakin baik)")
        st.line_chart(attendance_trend, color="#059669", height=260)
    with executive_right:
        st.caption("Ranking OPD — berdasarkan persentase kehadiran pada periode terfilter")
        st.dataframe(
            opd_ranking[["Unit Kerja", "Pegawai", "Kehadiran", "TK_TB"]],
            use_container_width=True,
            column_config={
                "Kehadiran": st.column_config.NumberColumn("Kehadiran", format="%.1f%%"),
                "TK_TB": st.column_config.NumberColumn("TK", format="%d hari"),
            },
        )

    successful_pages = EXTRACTION_TOTAL_PAGES - EXTRACTION_ANOMALY_PAGES
    success_rate = successful_pages / EXTRACTION_TOTAL_PAGES * 100
    anomaly_rate = EXTRACTION_ANOMALY_PAGES / EXTRACTION_TOTAL_PAGES * 100
    st.info(
        f"**Kualitas ekstraksi data:** Total {EXTRACTION_TOTAL_PAGES} halaman diproses — "
        f"**{success_rate:.0f}% berhasil diurai** ({successful_pages} halaman), "
        f"**{anomaly_rate:.0f}% terdapat anomali format** ({EXTRACTION_ANOMALY_PAGES} halaman)."
    )

    high = int((filtered["Status"] == "Risiko Tinggi").sum())
    warning = int((filtered["Status"] == "Peringatan Dini").sum())
    safe = int((filtered["Status"] == "Aman").sum())
    total_tk = int(filtered["TK"].sum())

    st.markdown("<div class='section-title'>Ringkasan Early Warning System</div>", unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(f"<div class='ews-card red'><div class='label'>🔴 RISIKO TINGGI</div><div class='number'>{high}</div><div class='detail'>Pegawai perlu ditindaklanjuti</div></div>", unsafe_allow_html=True)
    c2.markdown(f"<div class='ews-card orange'><div class='label'>🟠 PERINGATAN DINI</div><div class='number'>{warning}</div><div class='detail'>Perlu pembinaan segera</div></div>", unsafe_allow_html=True)
    c3.markdown(f"<div class='ews-card green'><div class='label'>🟢 STATUS AMAN</div><div class='number'>{safe}</div><div class='detail'>Pegawai sesuai ketentuan</div></div>", unsafe_allow_html=True)
    c4.metric("Total Hari Tanpa Keterangan", f"{total_tk} hari", help="Akumulasi hari tanpa keterangan pada data terfilter.")

    st.markdown("<div class='legend'><span><i style='background:#e11d48'></i>Risiko Tinggi (TK ≥ 6)</span><span><i style='background:#f97316'></i>Peringatan Dini (TK 3–5)</span><span><i style='background:#10b981'></i>Aman (TK 0–2)</span></div>", unsafe_allow_html=True)

    zone_counts = filtered["TK"].apply(discipline_zone).value_counts()
    zone_definitions = [
        ("Ambang Monitoring 3–10 Hari", "3–10 hari", "funnel-light", "100%"),
        ("Ambang Monitoring 11–20 Hari", "11–20 hari", "funnel-medium", "78%"),
        ("Prioritas Verifikasi ≥21 Hari", "≥ 21 hari", "funnel-heavy", "56%"),
    ]
    funnel_levels = "".join(
        f"<div class='funnel-level {style}' style='width:{width}'><strong>{name}</strong><span>{int(zone_counts.get(name, 0))} ASN<br>{range_label}</span></div>"
        for name, range_label, style, width in zone_definitions
    )
    st.markdown("<div class='section-title'>Ambang Referensi Monitoring Presensi</div>", unsafe_allow_html=True)
    st.caption("Pembagian berdasarkan akumulasi hari tanpa keterangan (TK) pada data yang sedang difilter.")
    st.markdown(f"<div class='discipline-funnel'>{funnel_levels}</div>", unsafe_allow_html=True)
    st.caption("Klik salah satu zona untuk melihat daftar pegawai dan tindak lanjutnya.")
    light_tab, medium_tab, heavy_tab = st.tabs(
        ["Ringan (3–10 hari)", "Sedang (11–20 hari)", "Berat (≥ 21 hari)"]
    )
    for tab, zone_name in [
        (light_tab, "Ambang Monitoring 3–10 Hari"),
        (medium_tab, "Ambang Monitoring 11–20 Hari"),
        (heavy_tab, "Prioritas Verifikasi ≥21 Hari"),
    ]:
        with tab:
            zone_table = filtered[filtered["TK"].apply(discipline_zone) == zone_name].copy()
            zone_table["Tindak Lanjut"] = zone_table["TK"].apply(sanction_recommendation)
            zone_table = zone_table.sort_values(["TK", "Nama Pegawai"], ascending=[False, True])[
                ["NIP", "Nama Pegawai", "Unit Kerja", "TK", "Tindak Lanjut"]
            ]
            if zone_table.empty:
                st.info(f"Belum ada pegawai dalam zona {zone_name.lower()}.")
            else:
                st.dataframe(
                    zone_table,
                    hide_index=True,
                    use_container_width=True,
                    column_config={
                        "TK": st.column_config.NumberColumn("Hari Tidak Hadir", format="%d hari"),
                        "Tindak Lanjut": st.column_config.TextColumn("Tindak Lanjut", width="large"),
                    },
                )

    st.markdown("<div class='section-title'>Analisis Perilaku Kehadiran</div>", unsafe_allow_html=True)
    left, right = st.columns(2)
    weekdays = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat"]
    with left:
        st.caption("Day-of-Week Analysis — total keterlambatan per hari")
        day_chart = filtered.groupby("Hari Dominan")["Terlambat"].sum().reindex(weekdays, fill_value=0)
        st.bar_chart(day_chart, color="#2563eb", height=290)
    with right:
        st.caption("Peak Hour Distribution — distribusi jam kedatangan")
        arrival_bins = pd.cut(filtered["Jam Datang"], bins=[6.99, 7.29, 7.44, 7.59, 7.74, 7.89, 8.5], labels=["≤ 07.29", "07.30–07.44", "07.45–07.59", "08.00–08.14", "08.15–08.29", "≥ 08.30"])
        hour_chart = arrival_bins.value_counts().sort_index()
        st.bar_chart(hour_chart, color="#7c3aed", height=290)
    st.markdown("<p class='note'>Analisis ini membantu mengidentifikasi pola keterlambatan dan jam kedatangan yang paling sering terjadi.</p>", unsafe_allow_html=True)

    st.markdown("<div class='section-title'>Tren Kedisiplinan Unit Kerja</div>", unsafe_allow_html=True)
    trend_source = data if chosen_unit == "Semua Unit" else data[data["Unit Kerja"] == chosen_unit]
    trend_chart = (
        trend_source.groupby("Bulan")["TK"]
        .mean()
        .reindex(month_order)
        .rename("Rata-rata TK per Pegawai")
    )
    trend_name = "seluruh unit kerja" if chosen_unit == "Semua Unit" else chosen_unit
    first_value, last_value = trend_chart.iloc[0], trend_chart.iloc[-1]
    if last_value < first_value:
        trend_status = "membaik"
    elif last_value > first_value:
        trend_status = "memburuk"
    else:
        trend_status = "stabil"
    st.caption(
        f"Tren {trend_name}. Nilai rata-rata TK yang menurun menunjukkan kedisiplinan membaik; "
        f"tren saat ini: **{trend_status}**."
    )
    st.line_chart(trend_chart, color="#e11d48", height=300)

    st.markdown("<div class='section-title'>Antrian Tindak Lanjut</div>", unsafe_allow_html=True)
    queue_cols = st.columns(3)
    queue_cols[0].metric("🔴 Belum diproses", warning_total)
    queue_cols[1].metric("🟡 Dalam verifikasi", 0)
    queue_cols[2].metric("🟢 Selesai", 0)
    if st.button("Buka Action Center →", key="open_action_center_from_ews"):
        st.session_state["navigate_to_page"] = "Action Center"
        st.rerun()

    st.markdown("<div class='section-title'>Detail & Tindakan Warning</div>", unsafe_allow_html=True)
    tab_tk, tab_cuti, tab_terlambat = st.tabs(["Tanpa Keterangan", "Cuti", "Keterlambatan"])

    with tab_tk:
        st.caption("Diurutkan berdasarkan akumulasi hari tanpa keterangan (TK) tertinggi.")
        tk_table = filtered[filtered["TK"] > 0].sort_values(["TK", "Nama Pegawai"], ascending=[False, True])[
            ["NIP", "Nama Pegawai", "Unit Kerja", "TK", "Status", "Rekomendasi"]
        ]
        st.dataframe(
            tk_table,
            hide_index=True,
            use_container_width=True,
            column_config={
                "TK": st.column_config.NumberColumn("Hari TK", format="%d hari"),
                "Status": st.column_config.TextColumn("Status EWS", width="medium"),
                "Rekomendasi": st.column_config.TextColumn("Rekomendasi Tindak Lanjut", width="large"),
            },
        )

    with tab_cuti:
        st.caption("Cuti memerlukan verifikasi administrasi dan bukan sanksi otomatis.")
        cuti_table = filtered[filtered["Cuti"] > 0].copy()
        cuti_table["Tindak Lanjut Cuti"] = cuti_table["Cuti"].apply(leave_recommendation)
        cuti_table = cuti_table.sort_values(["Cuti", "Nama Pegawai"], ascending=[False, True])[
            ["NIP", "Nama Pegawai", "Unit Kerja", "Cuti", "Tindak Lanjut Cuti"]
        ]
        st.dataframe(
            cuti_table,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Cuti": st.column_config.NumberColumn("Hari Cuti", format="%d hari"),
                "Tindak Lanjut Cuti": st.column_config.TextColumn("Tindak Lanjut", width="large"),
            },
        )

    with tab_terlambat:
        st.caption("Diurutkan berdasarkan frekuensi keterlambatan tertinggi.")
        tardy_table = filtered[filtered["Terlambat"] > 0].copy()
        tardy_table["Rekomendasi"] = tardy_table["Terlambat"].apply(tardiness_recommendation)
        tardy_table = tardy_table.sort_values(["Terlambat", "Nama Pegawai"], ascending=[False, True])[
            ["NIP", "Nama Pegawai", "Unit Kerja", "Terlambat", "Rekomendasi"]
        ]
        st.dataframe(
            tardy_table,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Terlambat": st.column_config.NumberColumn("Jumlah Terlambat", format="%d kali"),
                "Rekomendasi": st.column_config.TextColumn("Rekomendasi Tindak Lanjut", width="large"),
            },
        )


def show_early_warning_page_legacy() -> None:
    inject_dashboard_css()
    data = load_employee_data()
    month_order = ["Januari", "Februari", "Maret"]

    with st.sidebar:
        st.markdown("# 🛡️ EWS Kehadiran")
        st.caption("Sistem peringatan dini disiplin pegawai")
        st.divider()
        st.markdown("#### Filter Early Warning")
        chosen_month = st.selectbox("Periode bulan", month_order, key="ews_month")
        absence_options = {
            "Semua jenis": None,
            "Tanpa Keterangan (TK)": "TK",
            "Cuti": "Cuti",
            "Terlambat": "Terlambat",
        }
        chosen_absence = st.selectbox("Jenis ketidakhadiran", absence_options.keys(), key="ews_absence")
        units = ["Semua Unit"] + sorted(data["Unit Kerja"].unique().tolist())
        chosen_unit = st.selectbox("Unit kerja", units, key="ews_unit")
        search_name = st.text_input("Cari nama atau NIP", placeholder="Ketik untuk mencari", key="ews_search")
        st.divider()
        st.caption(f"Login sebagai: {st.session_state.username}")
        st.button("Keluar", on_click=logout, use_container_width=True, key="ews_logout")

    filtered = data[data["Bulan"] == chosen_month].copy()
    absence_column = absence_options[chosen_absence]
    if absence_column:
        filtered = filtered[filtered[absence_column] > 0]
    if chosen_unit != "Semua Unit":
        filtered = filtered[filtered["Unit Kerja"] == chosen_unit]
    if search_name:
        query = search_name.lower()
        filtered = filtered[
            filtered["Nama Pegawai"].str.lower().str.contains(query)
            | filtered["NIP"].str.lower().str.contains(query)
        ]

    risk_data = filtered.copy()
    risk_data["Risk Score"] = risk_data.apply(
        lambda row: calculate_risk_score(row["TK"], row["Terlambat"]), axis=1
    )
    risk_data["Status Risiko"] = risk_data["Risk Score"].apply(risk_status)
    risk_data["Indikator Warning"] = risk_data.apply(
        lambda row: warning_indicators(row["TK"], row["Terlambat"]), axis=1
    )
    risk_data["Rekomendasi"] = risk_data["Status Risiko"].apply(risk_recommendation)

    status_order = ["Normal", "Waspada", "Tinggi", "Kritis"]
    status_counts = risk_data["Status Risiko"].value_counts().reindex(status_order, fill_value=0)
    average_risk = risk_data["Risk Score"].mean() if not risk_data.empty else 0

    st.markdown("<h1 class='dashboard-title'>Early Warning System</h1>", unsafe_allow_html=True)
    st.markdown(
        f"<p class='dashboard-subtitle'>Analisis risiko kehadiran pegawai • Periode: {chosen_month}</p>",
        unsafe_allow_html=True,
    )
    risk_metric, normal_metric, alert_metric, high_metric, critical_metric = st.columns(5)
    risk_metric.metric("Risk Score Rata-rata", f"{average_risk:.0f}/100")
    normal_metric.metric("Normal", int(status_counts["Normal"]))
    alert_metric.metric("Waspada", int(status_counts["Waspada"]))
    high_metric.metric("Tinggi", int(status_counts["Tinggi"]))
    critical_metric.metric("Kritis", int(status_counts["Kritis"]))

    st.markdown("<div class='section-title'>Jumlah Pegawai per Kategori Risiko</div>", unsafe_allow_html=True)
    st.bar_chart(status_counts.rename("Jumlah Pegawai"), color="#dc2626", height=240)

    cause_col, action_col = st.columns(2)
    with cause_col:
        st.markdown("<div class='section-title'>Indikator Penyebab Warning</div>", unsafe_allow_html=True)
        indicator_summary = pd.DataFrame(
            {
                "Indikator": ["TK ≥ 3 hari", "Terlambat ≥ 8 kali"],
                "Jumlah Pegawai": [
                    int((risk_data["TK"] >= 3).sum()),
                    int((risk_data["Terlambat"] >= 8).sum()),
                ],
            }
        )
        st.dataframe(indicator_summary, hide_index=True, use_container_width=True)
    with action_col:
        st.markdown("<div class='section-title'>Panduan Tindak Lanjut</div>", unsafe_allow_html=True)
        st.markdown("- **Kritis:** pemeriksaan disiplin segera.\n- **Tinggi:** teguran tertulis dan pembinaan.\n- **Waspada:** konseling serta monitoring mingguan.\n- **Normal:** monitoring rutin.")

    st.markdown("<div class='section-title'>Daftar Pegawai Berisiko</div>", unsafe_allow_html=True)
    st.caption("Pegawai berstatus Waspada, Tinggi, atau Kritis; diurutkan dari Risk Score tertinggi.")
    risk_table = risk_data[risk_data["Status Risiko"] != "Normal"].sort_values(
        ["Risk Score", "Nama Pegawai"], ascending=[False, True]
    )[
        ["NIP", "Nama Pegawai", "Unit Kerja", "Risk Score", "Status Risiko", "Indikator Warning", "Rekomendasi"]
    ]
    if risk_table.empty:
        st.success("Tidak ada pegawai berisiko pada filter yang dipilih.")
    else:
        st.dataframe(
            risk_table,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Risk Score": st.column_config.NumberColumn("Risk Score", format="%d/100"),
                "Status Risiko": st.column_config.TextColumn("Status", width="medium"),
                "Indikator Warning": st.column_config.TextColumn("Penyebab Warning", width="large"),
                "Rekomendasi": st.column_config.TextColumn("Rekomendasi Tindak Lanjut", width="large"),
            },
        )


def show_attendance_analysis_page_legacy() -> None:
    inject_dashboard_css()
    data = load_employee_data()
    month_order = ["Januari", "Februari", "Maret"]

    with st.sidebar:
        st.markdown("# 🛡️ EWS Kehadiran")
        st.caption("Analisis presensi pegawai")
        st.divider()
        st.markdown("#### Filter Analisis")
        chosen_month = st.selectbox("Periode bulan", month_order, key="attendance_month")
        units = ["Semua Unit"] + sorted(data["Unit Kerja"].unique().tolist())
        chosen_unit = st.selectbox("Unit kerja", units, key="attendance_unit")
        search_name = st.text_input("Cari nama atau NIP", placeholder="Ketik untuk mencari", key="attendance_search")
        st.divider()
        st.caption(f"Login sebagai: {st.session_state.username}")
        st.button("Keluar", on_click=logout, use_container_width=True, key="attendance_logout")

    filtered = data[data["Bulan"] == chosen_month].copy()
    if chosen_unit != "Semua Unit":
        filtered = filtered[filtered["Unit Kerja"] == chosen_unit]
    if search_name:
        query = search_name.lower()
        filtered = filtered[
            filtered["Nama Pegawai"].str.lower().str.contains(query)
            | filtered["NIP"].str.lower().str.contains(query)
        ]

    filtered["Hadir"] = (
        filtered["Hari Kerja"] - filtered["TK"] - filtered["Cuti"] - filtered["WFH"] - filtered["DL"]
    ).clip(lower=0)

    st.markdown("<p class='dashboard-title'>Analisis Presensi</p>", unsafe_allow_html=True)
    st.markdown(
        f"<p class='dashboard-subtitle'>Ringkasan presensi pegawai • Periode: {chosen_month}</p>",
        unsafe_allow_html=True,
    )
    attendance_totals = {
        "Hadir": int(filtered["Hadir"].sum()),
        "TK": int(filtered["TK"].sum()),
        "WFH": int(filtered["WFH"].sum()),
        "DL": int(filtered["DL"].sum()),
        "Cuti": int(filtered["Cuti"].sum()),
        "Keterlambatan": int(filtered["Terlambat"].sum()),
    }
    metric_columns = st.columns(6)
    for column, (label, value) in zip(metric_columns, attendance_totals.items()):
        unit_label = "kali" if label == "Keterlambatan" else "hari"
        column.metric(label, f"{value} {unit_label}")

    day_col, peak_col = st.columns(2)
    with day_col:
        st.markdown("<div class='section-title'>Day-of-Week Analysis</div>", unsafe_allow_html=True)
        st.caption("Total TK dan keterlambatan berdasarkan hari yang paling dominan.")
        weekdays = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat"]
        day_analysis = filtered.groupby("Hari Dominan")[["TK", "Terlambat"]].sum().reindex(weekdays, fill_value=0)
        st.bar_chart(day_analysis, height=300)
    with peak_col:
        st.markdown("<div class='section-title'>Peak Hour Analysis</div>", unsafe_allow_html=True)
        st.caption("Distribusi jam kedatangan pegawai pada data terfilter.")
        arrival_bins = pd.cut(
            filtered["Jam Datang"],
            bins=[6.99, 7.29, 7.44, 7.59, 7.74, 7.89, 8.5],
            labels=["≤ 07.29", "07.30–07.44", "07.45–07.59", "08.00–08.14", "08.15–08.29", "≥ 08.30"],
        )
        peak_analysis = arrival_bins.value_counts().sort_index().rename("Jumlah Pegawai")
        st.bar_chart(peak_analysis, color="#7c3aed", height=300)

    st.markdown("<div class='section-title'>Rincian Data Presensi</div>", unsafe_allow_html=True)
    attendance_table = filtered.sort_values("Nama Pegawai")[
        ["NIP", "Nama Pegawai", "Unit Kerja", "Hadir", "TK", "WFH", "DL", "Cuti", "Terlambat"]
    ]
    st.dataframe(
        attendance_table,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Hadir": st.column_config.NumberColumn("Hadir", format="%d hari"),
            "TK": st.column_config.NumberColumn("TK", format="%d hari"),
            "WFH": st.column_config.NumberColumn("WFH", format="%d hari"),
            "DL": st.column_config.NumberColumn("DL", format="%d hari"),
            "Cuti": st.column_config.NumberColumn("Cuti", format="%d hari"),
            "Terlambat": st.column_config.NumberColumn("Keterlambatan", format="%d kali"),
        },
    )


def show_attendance_analysis_page_legacy() -> None:
    inject_dashboard_css()
    data = load_employee_data()
    months = available_months(data)
    weekdays = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat"]
    with st.sidebar:
        st.markdown("# 🛡️ EWS Kehadiran")
        st.caption("Analisis pola presensi")
        st.divider()
        chosen_month = st.selectbox("Periode bulan", ["Semua Bulan"] + months, key="attendance_page_month")
        units = ["Semua Unit"] + sorted(data["Unit Kerja"].unique().tolist())
        chosen_unit = st.selectbox("Unit kerja", units, key="attendance_page_unit")
        search_name = st.text_input("Cari nama atau NIP", key="attendance_page_search")
        st.divider()
        st.caption(f"Login sebagai: {st.session_state.username}")
        st.button("Keluar", on_click=logout, use_container_width=True, key="attendance_page_logout")

    filtered = data.copy()
    if chosen_month != "Semua Bulan":
        filtered = filtered[filtered["Bulan"] == chosen_month]
    if chosen_unit != "Semua Unit":
        filtered = filtered[filtered["Unit Kerja"] == chosen_unit]
    if search_name:
        query = search_name.lower()
        filtered = filtered[filtered["Nama Pegawai"].str.lower().str.contains(query) | filtered["NIP"].str.lower().str.contains(query)]
    filtered = filtered.copy()
    filtered["Hadir"] = (filtered["Hari Kerja"] - filtered["WFH"] - filtered["DL"] - filtered["Cuti"] - filtered["TK"]).clip(lower=0)

    composition = pd.Series({
        "Hadir": int(filtered["Hadir"].sum()), "WFH": int(filtered["WFH"].sum()), "DL": int(filtered["DL"].sum()),
        "Cuti": int(filtered["Cuti"].sum()), "TK": int(filtered["TK"].sum()),
        "Terlambat": int(filtered["Terlambat"].sum()),
    })
    day_analysis = filtered.groupby("Hari Dominan")["Terlambat"].sum().reindex(weekdays, fill_value=0)
    peak_bins = pd.cut(filtered["Jam Datang"], bins=[6.99, 7.30, 8.00, 8.30, 8.5], labels=["07.00–07.30", "07.31–08.00", "08.01–08.30", "≥ 08.31"])
    peak_analysis = peak_bins.value_counts().sort_index()
    peak_period = str(peak_analysis.idxmax()) if not peak_analysis.empty and peak_analysis.max() else "Belum tersedia"
    monthly = filtered.groupby("Bulan").agg(Hari_Kerja=("Hari Kerja", "sum"), TK=("TK", "sum"), Cuti=("Cuti", "sum"))
    monthly["Kehadiran"] = ((monthly["Hari_Kerja"] - monthly["TK"] - monthly["Cuti"]) / monthly["Hari_Kerja"] * 100).clip(lower=0)
    monthly = monthly.reindex(months)
    weekly = filtered.reset_index(drop=True).assign(Minggu=lambda frame: frame.index // 5 + 1).groupby("Minggu")["Terlambat"].sum().rename("Keterlambatan")
    heatmap = filtered.pivot_table(index="Bulan", columns="Hari Dominan", values="Terlambat", aggfunc="sum", fill_value=0).reindex(index=months, columns=weekdays, fill_value=0)

    st.markdown("<h1 class='dashboard-title'>Analisis Presensi</h1>", unsafe_allow_html=True)
    st.markdown(f"<div class='page-context-grid'><div class='page-context-card risk-context-card'>Komposisi dan pola presensi</div><div class='page-context-card'>Periode: {chosen_month}</div></div>", unsafe_allow_html=True)
    st.markdown("<div class='section-title'>Komposisi Presensi</div>", unsafe_allow_html=True)
    composition_rows = "".join(
        f"<div class='composition-row'><span class='composition-label'>{label}</span><span class='composition-value'>{value} {'kali' if label == 'Terlambat' else 'hari'}</span></div>"
        for label, value in composition.items()
    )
    st.markdown(f"<div class='composition-list'>{composition_rows}</div>", unsafe_allow_html=True)
    composition_colors = {
        "Hadir": "#2563eb", "WFH": "#7c3aed", "DL": "#0891b2", "Cuti": "#16a34a",
        "TK": "#f59e0b", "Terlambat": "#dc2626",
    }
    maximum_composition = max(int(composition.max()), 1)
    composition_bar_rows = "".join(
        f"<div class='composition-bar-row'><span class='composition-bar-label'>{label}</span>"
        f"<div class='composition-bar-track'><div class='composition-bar-fill' style='width:{value / maximum_composition * 100:.1f}%;background:{composition_colors[label]}'></div></div>"
        f"<span class='composition-bar-value'>{int(value)}</span></div>"
        for label, value in composition.items()
    )
    st.markdown(f"<div class='composition-bars'>{composition_bar_rows}</div>", unsafe_allow_html=True)

    st.markdown("<div class='section-title'>Trend Analysis</div>", unsafe_allow_html=True)
    daily_col, weekly_col, monthly_col = st.columns(3)
    with daily_col:
        st.caption("Tren harian")
        st.bar_chart(day_analysis, color="#f97316", height=220)
    with weekly_col:
        st.caption("Tren mingguan")
        st.line_chart(weekly, color="#7c3aed", height=220)
    with monthly_col:
        st.caption("Tren bulanan")
        st.line_chart(monthly["Kehadiran"], color="#059669", height=220)

    st.markdown("<div class='section-title'>Calendar Heatmap</div>", unsafe_allow_html=True)
    st.dataframe(
        heatmap,
        use_container_width=True,
        column_config={weekday: st.column_config.NumberColumn(weekday, format="%d") for weekday in weekdays},
    )
    day_col, peak_col = st.columns(2)
    with day_col:
        st.markdown("<div class='section-title'>Day-of-Week Analysis</div>", unsafe_allow_html=True)
        day_table = day_analysis.rename("Keterlambatan").reset_index().rename(columns={"Hari Dominan": "Hari"})
        day_table["Ringkasan"] = day_table.apply(lambda row: f"{row['Hari']} → {int(row['Keterlambatan'])} keterlambatan", axis=1)
        st.dataframe(day_table, hide_index=True, use_container_width=True)
    with peak_col:
        st.markdown("<div class='section-title'>Peak Hour Analysis</div>", unsafe_allow_html=True)
        st.bar_chart(peak_analysis.rename("Jumlah Pegawai"), color="#dc2626", height=220)
        st.info(f"Periode keterlambatan tertinggi: **{peak_period}**")
    total_late = int(day_analysis.sum())
    monday_share = int(day_analysis.get("Senin", 0)) / total_late * 100 if total_late else 0
    st.markdown("<div class='section-title'>Pattern Analysis</div>", unsafe_allow_html=True)
    st.warning(f"⚠️ **{monday_share:.0f}%** kejadian keterlambatan terjadi pada hari Senin.")


def show_attendance_analysis_page_legacy_v3() -> None:
    inject_dashboard_css()
    data = load_employee_data()
    months = available_months(data)
    weekdays = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat"]
    years = sorted(pd.to_numeric(data["Tahun"], errors="coerce").dropna().astype(int).unique().tolist())
    units = sorted(data["Unit Kerja"].dropna().astype(str).unique().tolist())
    employee_types = [item for item in ["PNS", "PPPK", "Belum Diketahui"] if item in set(data["Jenis Pegawai"])]
    st.markdown("<div class='attendance-analysis-page'>", unsafe_allow_html=True)
    st.markdown("<h1 class='attendance-page-title'>Analisis Presensi</h1><div class='attendance-page-subtitle'>Analisis pola kehadiran, keterlambatan, dan ketidakhadiran pegawai.</div>", unsafe_allow_html=True)
    filter_year, filter_month, filter_opd, filter_type, filter_reset = st.columns([1, 1, 2, 1.4, .7])
    with filter_year:
        chosen_year = st.selectbox("Tahun", ["Semua Tahun"] + years, key="attendance_filter_year")
    with filter_month:
        chosen_month = st.selectbox("Bulan", ["Semua Bulan"] + months, key="attendance_filter_month")
    with filter_opd:
        chosen_unit = st.selectbox("OPD", ["Semua OPD"] + units, key="attendance_filter_opd")
    with filter_type:
        chosen_employee_type = st.selectbox("Jenis Pegawai", ["Semua Jenis Pegawai"] + employee_types, key="attendance_filter_employee_type")
    with filter_reset:
        st.write("")
        if st.button("Reset", key="attendance_filter_reset", use_container_width=True):
            for key in ["attendance_filter_year", "attendance_filter_month", "attendance_filter_opd", "attendance_filter_employee_type", "attendance_trend_metric", "attendance_trend_granularity"]:
                st.session_state.pop(key, None)
            st.rerun()
    filtered = apply_filters(data, year=chosen_year, month=chosen_month, opd=chosen_unit, employee_type=chosen_employee_type)
    daily_filtered = _load_excel_daily_data(_excel_source_signature()).copy()
    if chosen_year != "Semua Tahun":
        daily_filtered = daily_filtered[daily_filtered["Tahun"].eq(int(chosen_year))]
    if chosen_employee_type != "Semua Jenis Pegawai":
        daily_filtered = daily_filtered[daily_filtered["Jenis Pegawai"].eq(chosen_employee_type)]
    tk_daily_base = daily_filtered.copy()
    if chosen_unit != "Semua OPD":
        daily_filtered = daily_filtered[daily_filtered["Unit Kerja"].eq(chosen_unit)]
    if chosen_month != "Semua Bulan":
        daily_filtered = daily_filtered[daily_filtered["Nama_Bulan"].eq(chosen_month)]
    if filtered.empty:
        st.info("Tidak terdapat data presensi pada kombinasi filter yang dipilih.")
        st.markdown("</div>", unsafe_allow_html=True)
        return
    filtered = filtered.copy()
    filtered["Hadir"] = (filtered["Hari Kerja"] - filtered["WFH"] - filtered["DL"] - filtered["Cuti"] - filtered["TK"]).clip(lower=0)
    total_work = max(int(filtered["Hari Kerja"].sum()), 1)
    comp = filtered[["Hadir", "WFH", "DL", "Cuti", "TK"]].sum().astype(int)
    late_total = int(filtered["Terlambat"].sum())
    visible_months = [month for month in months if month in set(filtered["Bulan"].astype(str))]
    visible_years = sorted(pd.to_numeric(filtered["Tahun"], errors="coerce").dropna().astype(int).unique().tolist())
    if len(visible_months) > 1:
        period_label = f"{visible_months[0]}–{visible_months[-1]}"
    else:
        period_label = visible_months[0] if visible_months else "Periode aktif"
    if len(visible_years) == 1:
        period_label += f" {visible_years[0]}"
    elif visible_years:
        period_label += f" {visible_years[0]}–{visible_years[-1]}"
    opd_label = "Semua OPD" if chosen_unit == "Semua OPD" else chosen_unit
    st.markdown(f"<div class='attendance-meta-strip'>Periode aktif: <strong>{escape(period_label)}</strong> • <strong>{escape(opd_label.title())}</strong> • <strong>{filtered['NIP'].nunique():,}</strong> Pegawai</div>".replace(",", "."), unsafe_allow_html=True)
    st.markdown("""
        <style>
        .attendance-analysis-page{max-width:1480px;margin:0 auto}.attendance-page-title{font-size:29px;font-weight:700;color:#173b63;margin:0 0 4px}.attendance-page-subtitle{font-size:14px;color:#64748b;margin-bottom:14px}
        .attendance-meta-strip{font-size:12px;color:#64748b;background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:9px 12px;margin:4px 0 20px}.attendance-section-title{font-size:17px;font-weight:700;color:#173b63;margin:24px 0 10px}
        .attendance-kpi-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.attendance-kpi-card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:14px 16px;box-shadow:0 3px 12px rgba(15,23,42,.035);transition:transform .18s ease,box-shadow .18s ease}.attendance-kpi-card:hover{transform:translateY(-1px);box-shadow:0 5px 16px rgba(15,23,42,.05)}
        .attendance-kpi-value{font-size:30px;font-weight:700;color:#173b63;line-height:1.1}.attendance-kpi-label{font-size:12px;color:#64748b;margin-top:5px}.attendance-secondary{font-size:12px;color:#64748b;margin-top:9px}
        @media(max-width:700px){.attendance-kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.attendance-page-title{font-size:26px}}
        </style>
    """, unsafe_allow_html=True)
    st.markdown("<div class='section-title'>Ringkasan Presensi</div>", unsafe_allow_html=True)
    indicator_summary = summarize_attendance_indicators(daily_filtered)
    attendance_rate_value = float(indicator_summary["physical_attendance_percentage"])
    compliance_rate_value = float(indicator_summary["compliance_percentage"])
    kpi_html = "".join(
        f"<div class='attendance-kpi-card'><div class='attendance-kpi-value'>{value}</div><div class='attendance-kpi-label'>{label}</div></div>"
        for label, value in [
            ("Kepatuhan Presensi", f"{compliance_rate_value:.1f}%".replace(".", ",")),
            ("Kehadiran Fisik", f"{attendance_rate_value:.1f}%".replace(".", ",")),
            ("Keterlambatan", f"{late_total:,}".replace(",", ".")),
            ("TK", f"{int(comp['TK']):,}".replace(",", ".")),
            ("Cuti", f"{int(comp['Cuti']):,}".replace(",", ".")),
        ]
    )
    st.markdown(f"<div class='attendance-kpi-grid'>{kpi_html}</div><div class='attendance-secondary'>WFH {int(comp['WFH']):,} • DL {int(comp['DL']):,}</div>".replace(",", "."), unsafe_allow_html=True)
    st.caption("Kepatuhan Presensi menghitung status sah pada hari wajib presensi. Kehadiran Fisik hanya menghitung hari benar-benar hadir; keterlambatan tetap dihitung terpisah.")
    st.markdown("<div class='section-title'>Komposisi Presensi</div>", unsafe_allow_html=True)
    composition = comp.rename_axis("Status").reset_index(name="Jumlah")
    composition["Persentase"] = composition["Jumlah"] / max(int(composition["Jumlah"].sum()), 1) * 100
    composition["Label"] = composition["Persentase"].map(lambda value: f"{value:.1f}%".replace(".", ","))
    composition_colors = alt.Scale(domain=["Hadir", "Cuti", "DL", "WFH", "TK"], range=["#10b981", "#8b5cf6", "#2563eb", "#06b6d4", "#ef4444"])
    composition_chart = alt.Chart(composition).mark_bar(cornerRadiusEnd=5).encode(
        y=alt.Y("Status:N", sort=["Hadir", "Cuti", "DL", "WFH", "TK"], title=None),
        x=alt.X("Persentase:Q", title="Persentase", scale=alt.Scale(domain=[0, max(float(composition['Persentase'].max()) * 1.12, 1)])),
        color=alt.Color("Status:N", scale=composition_colors, legend=None),
        tooltip=["Status:N", alt.Tooltip("Jumlah:Q", format=","), alt.Tooltip("Persentase:Q", format=".1f")],
    )
    composition_labels = alt.Chart(composition).mark_text(align="left", dx=5, color="#475569").encode(
        y=alt.Y("Status:N", sort=["Hadir", "Cuti", "DL", "WFH", "TK"]), x="Persentase:Q", text="Label:N"
    )
    st.altair_chart((composition_chart + composition_labels).properties(height=210), use_container_width=True)
    st.markdown("<div class='section-title'>Tren Presensi</div>", unsafe_allow_html=True)
    trend_metric_col, trend_granularity_col = st.columns([1, 2])
    with trend_metric_col:
        trend_metric = st.selectbox(
            "Metric", ["Kepatuhan Presensi", "Kehadiran Fisik", "TK", "Keterlambatan", "Cuti", "WFH", "DL"],
            key="attendance_trend_metric",
            help="Kepatuhan Presensi menghitung status sah (Hadir, Cuti, WFH, DL, SL, SK, MR, atau PBT). Kehadiran Fisik hanya menghitung hari benar-benar hadir. Keterlambatan dihitung terpisah.",
        )
    with trend_granularity_col:
        trend_granularity = st.radio("Granularitas", ["Harian", "Mingguan", "Bulanan"], horizontal=True, index=2, key="attendance_trend_granularity")
    trend_source = daily_filtered.copy()
    trend_source["Tanggal"] = pd.to_datetime(trend_source["Tanggal"], errors="coerce")
    trend_source = trend_source[trend_source["Tanggal"].notna()].copy()
    source_codes = (trend_source["Sumber_Datang"].fillna("") + "/" + trend_source["Sumber_Pulang"].fillna("")).str.upper()
    trend_source["Cuti"] = source_codes.str.contains("CUTI", regex=False)
    trend_source["WFH"] = source_codes.str.contains(r"\bWFH\b|\bWFA\b", regex=True)
    trend_source["DL"] = source_codes.str.contains(r"\bDL\b", regex=True)
    trend_source["Hari Kerja"] = trend_source["Status"].ne("LIBUR")
    trend_data = aggregate_attendance_trend(trend_source, trend_granularity)
    trend_color = {"Kepatuhan Presensi": "#10b981", "Kehadiran Fisik": "#2563eb", "Keterlambatan": "#f59e0b", "TK": "#ef4444", "Cuti": "#8b5cf6", "WFH": "#06b6d4", "DL": "#64748b"}[trend_metric]
    percentage_metric = trend_metric in {"Kepatuhan Presensi", "Kehadiran Fisik"}
    trend_chart = alt.Chart(trend_data).mark_line(point=True, strokeWidth=3, color=trend_color).encode(
        x=alt.X("PeriodLabel:N", title=None, sort=trend_data["PeriodLabel"].tolist(), axis=alt.Axis(labelAngle=0)),
        y=alt.Y(f"{trend_metric}:Q", title=f"{trend_metric}{' (%)' if percentage_metric else ''}", scale=alt.Scale(domain=[0, 100]) if percentage_metric else alt.Undefined),
        tooltip=[alt.Tooltip("PeriodLabel:N", title="Periode"), alt.Tooltip(f"{trend_metric}:Q", format=".1f" if percentage_metric else ",", title=trend_metric)],
    ).properties(height=250)
    st.altair_chart(trend_chart, use_container_width=True)
    trend_insight = ""
    if len(trend_data) > 1:
        trend_change = float(trend_data.iloc[-1][trend_metric] - trend_data.iloc[-2][trend_metric])
        if trend_change:
            favorable = trend_change > 0 if percentage_metric else trend_change < 0
            trend_insight = f"{'💡' if favorable else '⚠️'} {trend_metric} {'meningkat' if trend_change > 0 else 'menurun'} {abs(trend_change):.1f}{' poin' if percentage_metric else ' kejadian'} dibanding periode sebelumnya."
            st.caption(trend_insight)

    late_daily = daily_filtered[daily_filtered["Terlambat"].fillna(False).astype(bool)].copy()
    day_analysis = late_daily.groupby("Hari").size().reindex(weekdays, fill_value=0)
    day_chart = day_analysis.rename("Keterlambatan").reset_index()
    peak_labels = ["07.31–07.45", "07.46–08.00", "08.01–08.30", "> 08.30"]
    arrival_minutes = pd.to_numeric(late_daily["Jam_Masuk"].astype(str).str.split(":").str[0], errors="coerce") * 60 + pd.to_numeric(late_daily["Jam_Masuk"].astype(str).str.split(":").str[1], errors="coerce")
    late_daily["Rentang Waktu"] = pd.cut(arrival_minutes, bins=[450, 465, 480, 510, 1440], labels=peak_labels, include_lowest=False)
    peak = late_daily["Rentang Waktu"].value_counts().reindex(peak_labels, fill_value=0)
    st.markdown("<div class='section-title'>Pola Waktu Presensi</div>", unsafe_allow_html=True)
    day_col, peak_col = st.columns(2)
    with day_col:
        st.markdown("<div class='section-title'>Hari Rawan Keterlambatan</div>", unsafe_allow_html=True); st.bar_chart(day_chart, x="Hari", y="Keterlambatan", color="#f97316", height=240)
        if day_analysis.max() > 0: st.caption(f"{day_analysis.idxmax()} merupakan hari dengan keterlambatan tertinggi, yaitu {int(day_analysis.max())} kejadian.")
    with peak_col:
        st.markdown("<div class='section-title'>Distribusi Waktu Keterlambatan</div>", unsafe_allow_html=True)
        peak_chart = peak.rename_axis("Rentang Waktu").reset_index(name="Keterlambatan")
        st.altair_chart(alt.Chart(peak_chart).mark_bar(color="#2563eb").encode(y=alt.Y("Rentang Waktu:N", sort=peak_labels, title=None), x=alt.X("Keterlambatan:Q", title="Keterlambatan"), tooltip=["Rentang Waktu", "Keterlambatan"]).properties(height=240), use_container_width=True)
        if len(peak) and peak.max() > 0: st.caption(f"Periode tertinggi: {peak.idxmax()} WIB dengan {int(peak.max())} kejadian.")
    heatmap = (
        late_daily.groupby(["Nama_Bulan", "Hari"], observed=False)
        .size().rename("Keterlambatan").reset_index()
    )
    heatmap["Nama_Bulan"] = pd.Categorical(heatmap["Nama_Bulan"], categories=months, ordered=True)
    st.markdown("<div class='section-title'>Heatmap Pola Keterlambatan</div>", unsafe_allow_html=True)
    heatmap_chart = alt.Chart(heatmap).mark_rect(cornerRadius=3).encode(
        x=alt.X("Hari:N", sort=weekdays, title=None),
        y=alt.Y("Nama_Bulan:N", sort=months, title=None),
        color=alt.Color("Keterlambatan:Q", scale=alt.Scale(scheme="blues"), legend=None),
        tooltip=[alt.Tooltip("Nama_Bulan:N", title="Bulan"), alt.Tooltip("Hari:N"), alt.Tooltip("Keterlambatan:Q", format=",", title="Keterlambatan")],
    ).properties(height=max(150, len(visible_months) * 34))
    st.altair_chart(heatmap_chart, use_container_width=True)
    st.caption("Semakin gelap warna, semakin tinggi jumlah kejadian keterlambatan.")

    st.markdown("<div class='section-title'>🚫 Ringkasan Ketidakhadiran (TK)</div>", unsafe_allow_html=True)
    st.caption("Analisis tren, pegawai, dan OPD dengan ketidakhadiran TK pada periode aktif.")
    active_month = chosen_month if chosen_month in months else (visible_months[-1] if visible_months else months[0])
    active_month_number = next((int(number) for number, name in MONTH_NAMES.items() if name == active_month), 1)
    default_period = ["TW I", "TW II", "TW III", "TW IV"][(active_month_number - 1) // 3]
    period_choice = default_period
    period_keys = {"TW I": "TRIWULAN_I", "TW II": "TRIWULAN_II", "TW III": "TRIWULAN_III", "TW IV": "TRIWULAN_IV"}
    period_type = period_keys[period_choice]
    selected_report_month_numbers = get_report_period_months(period_type)
    selected_report_months = [MONTH_NAMES[f"{index:02d}"] for index in selected_report_month_numbers]
    # Laporan TK selalu mencakup seluruh OPD; filter OPD halaman hanya untuk
    # analisis umum di atas section laporan.
    report_base = apply_filters(data, year=chosen_year, employee_type=chosen_employee_type)
    tk_report_filtered = report_base[report_base["Bulan"].astype(str).isin(selected_report_months)].copy()
    tk_daily_report = tk_daily_base[tk_daily_base["Nama_Bulan"].astype(str).isin(selected_report_months)].copy()
    tk_employee = (
        tk_report_filtered.groupby(["NIP", "Nama Pegawai", "Unit Kerja"], as_index=False, dropna=False)["TK"]
        .sum()
    )
    tk_employee = tk_employee[tk_employee["TK"].gt(0)].copy()
    report_data = prepare_tk_report_data(report_base, chosen_employee_type, period_type)
    total_tk_report = int(report_data["grand_total_tk"])
    total_tk_employees = int(report_data["total_employees"])
    total_tk_opds = int(report_data["total_opd"])
    short_months = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "Mei", 6: "Jun", 7: "Jul", 8: "Agt", 9: "Sep", 10: "Okt", 11: "Nov", 12: "Des"}
    report_range = f"{short_months[selected_report_month_numbers[0]]}–{short_months[selected_report_month_numbers[-1]]} {report_data['year']}"
    st.markdown(f"""
        <style>
        .tk-period-selector{{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:13px 16px;margin:8px 0 12px;box-shadow:0 3px 12px rgba(15,23,42,.035)}}
        .tk-report-meta{{font-size:13px;color:#475569}}.tk-report-meta strong{{color:#173b63}}
        @media(max-width:640px){{.tk-period-selector{{padding:12px}}}}
        </style>
        <div class='tk-period-selector'><div class='tk-report-meta'><strong>{escape(report_data['period_label'])}</strong> • {escape(report_range)}<br>{total_tk_opds} OPD tercakup • {total_tk_employees} Pegawai dengan TK • {total_tk_report} Hari TK</div></div>
    """, unsafe_allow_html=True)

    if st.button("Siapkan Laporan Ketidakhadiran →", key="attendance_go_tk_report"):
        st.session_state["navigate_to_page"] = "Laporan Ketidakhadiran"
        st.rerun()
    if total_tk_report == 0:
        st.markdown(
            f"<style>.tk-report-empty{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:13px;color:#475569;font-size:13px}}</style><div class='tk-report-empty'>✅ Tidak terdapat data TK pada {escape(report_data['period_label'].title())}.</div>",
            unsafe_allow_html=True,
        )
    else:
        highest_employee_tk = int(tk_employee["TK"].max())
        tk_kpis = "".join(
            f"<div class='tk-report-kpi'><div class='tk-report-kpi-value'>{value}</div><div class='tk-report-kpi-label'>{label}</div></div>"
            for label, value in [
                ("Hari TK", total_tk_report),
                ("Pegawai", total_tk_employees),
                ("OPD", total_tk_opds),
                ("TK Tertinggi", f"{highest_employee_tk} Hari"),
            ]
        )
        tk_trend = (
            tk_report_filtered.groupby("Bulan", as_index=False)
            .agg(Hari_TK=("TK", "sum"), Pegawai=("NIP", lambda values: values[tk_report_filtered.loc[values.index, "TK"].gt(0)].nunique()))
        )
        tk_trend["Bulan"] = pd.Categorical(tk_trend["Bulan"], categories=months, ordered=True)
        tk_trend = tk_trend.sort_values("Bulan")
        tk_trend_chart = alt.Chart(tk_trend).mark_bar(color="#ef4444").encode(
            x=alt.X("Bulan:N", sort=months, title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("Hari_TK:Q", title="Hari TK"),
            tooltip=[
                alt.Tooltip("Bulan:N", title="Periode"),
                alt.Tooltip("Hari_TK:Q", title="Hari TK"),
                alt.Tooltip("Pegawai:Q", title="Pegawai"),
            ],
        ).properties(height=220)

        tk_ranking_limit = st.selectbox(
            "Tampilkan", [5, 10], format_func=lambda value: f"Top {value}",
            key="tk_report_ranking_limit",
        )
        top_employees = tk_employee.sort_values(
            ["TK", "Nama Pegawai"], ascending=[False, True], kind="stable"
        ).head(tk_ranking_limit)
        employee_rows = "".join(
            f"<div class='tk-report-rank-row'><span class='tk-report-rank-no'>{rank:02d}</span><div class='tk-report-rank-main'><strong>{escape(str(row['Nama Pegawai']))}</strong><small>{escape(str(row['Unit Kerja']))}</small></div><span class='tk-report-rank-value'>{int(row['TK'])} hari</span></div>"
            for rank, (_, row) in enumerate(top_employees.iterrows(), start=1)
        )
        tk_opd = (
            tk_employee.groupby("Unit Kerja", as_index=False)
            .agg(Hari_TK=("TK", "sum"), Pegawai=("NIP", "nunique"))
            .sort_values(["Hari_TK", "Unit Kerja"], ascending=[False, True], kind="stable")
        )
        top_opds = tk_opd.head(tk_ranking_limit)
        opd_rows = "".join(
            f"<div class='tk-report-rank-row'><span class='tk-report-rank-no'>{rank:02d}</span><div class='tk-report-rank-main'><strong>{escape(str(row['Unit Kerja']).title())}</strong><small>{int(row['Hari_TK'])} hari TK • {int(row['Pegawai'])} pegawai</small></div></div>"
            for rank, (_, row) in enumerate(top_opds.iterrows(), start=1)
        )
        peak_month = tk_trend.loc[tk_trend["Hari_TK"].idxmax()]
        peak_opd = top_opds.iloc[0]
        detail_tk = tk_daily_report[tk_daily_report["TK"].fillna(False).astype(bool)].copy()
        detail_tk["Tanggal"] = pd.to_datetime(detail_tk["Tanggal"], errors="coerce")
        invalid_detail_dates = int(detail_tk["Tanggal"].isna().sum())
        detail_tk = detail_tk[detail_tk["Tanggal"].notna()].copy()
        duplicate_detail_count = int(detail_tk.duplicated(["NIP", "Tanggal"]).sum())
        detail_tk = (
            detail_tk.sort_values("Tanggal", ascending=False, kind="stable")
            .drop_duplicates(["NIP", "Tanggal"], keep="first")
        )
        if invalid_detail_dates or duplicate_detail_count:
            LOGGER.warning(
                "Detail TK: tanggal invalid=%s, duplicate NIP+Tanggal=%s",
                invalid_detail_dates, duplicate_detail_count,
            )
        detail_event_count = len(detail_tk)
        if detail_event_count != total_tk_report:
            LOGGER.error(
                "Total TK tidak konsisten: rekap=%s, detail harian=%s. Detail tanggal tidak dirender.",
                total_tk_report, detail_event_count,
            )
            detail_section_html = (
                "<div class='tk-report-ranking-title' style='margin-top:16px'>Rekap Ketidakhadiran Pegawai</div>"
                "<div class='tk-report-detail'>Data detail harian belum konsisten dengan rekap aktif. Rekap pegawai tetap ditampilkan pada ranking di atas.</div>"
            )
        elif detail_tk.empty:
            detail_section_html = (
                "<div class='tk-report-ranking-title' style='margin-top:16px'>Detail Ketidakhadiran</div>"
                "<div class='tk-report-detail'>Tidak terdapat ketidakhadiran dengan status TK pada filter yang dipilih.</div>"
            )
        else:
            month_names_id = {1: "Januari", 2: "Februari", 3: "Maret", 4: "April", 5: "Mei", 6: "Juni", 7: "Juli", 8: "Agustus", 9: "September", 10: "Oktober", 11: "November", 12: "Desember"}
            detail_rows = "".join(
                f"<tr><td>{row['Tanggal'].day:02d} {month_names_id[row['Tanggal'].month]} {row['Tanggal'].year}</td><td>{escape(str(row['Hari']))}</td><td>{escape(str(row['Nama']))}</td><td>{escape(str(row['NIP']))}</td><td>{escape(str(row['Unit Kerja']).title())}</td><td><span class='tk-report-status'>TK</span></td><td>-</td></tr>"
                for _, row in detail_tk.iterrows()
            )
            detail_section_html = (
                "<div class='tk-report-ranking-title' style='margin-top:16px'>Detail Ketidakhadiran</div>"
                "<div class='tk-report-table-wrap'><table class='tk-report-table'><thead><tr><th>Tanggal</th><th>Hari</th><th>Nama Pegawai</th><th>NIP</th><th>OPD</th><th>Status</th><th>Keterangan</th></tr></thead>"
                f"<tbody>{detail_rows}</tbody></table></div>"
            )
        st.markdown(f"""
            <style>
            .tk-report-section{{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:18px;margin-top:8px}}
            .tk-report-kpis{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}}
            .tk-report-kpi{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:11px 13px}}
            .tk-report-kpi-value{{font-size:27px;font-weight:700;color:#102a43;line-height:1.1}} .tk-report-kpi-label{{font-size:12px;color:#64748b;margin-top:4px}}
            .tk-report-rankings{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:16px}} .tk-report-ranking{{border:1px solid #e2e8f0;border-radius:12px;padding:14px}}
            .tk-report-ranking-title{{font-size:14px;font-weight:700;color:#173b63;margin-bottom:7px}} .tk-report-rank-row{{display:flex;align-items:flex-start;gap:8px;padding:9px 0;border-top:1px solid #f1f5f9}}
            .tk-report-rank-no{{font-size:11px;font-weight:700;color:#64748b}} .tk-report-rank-main{{min-width:0;flex:1}} .tk-report-rank-main strong{{display:block;font-size:12px;color:#173b63}} .tk-report-rank-main small{{display:block;font-size:11px;color:#64748b;margin-top:2px}}
            .tk-report-rank-value{{font-size:12px;font-weight:700;color:#173b63;white-space:nowrap}} .tk-report-detail{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:11px 12px;color:#64748b;font-size:12px;margin-top:14px}}
            .tk-report-table-wrap{{overflow-x:auto;border:1px solid #e2e8f0;border-radius:10px;margin-top:8px}} .tk-report-table{{width:100%;border-collapse:collapse;min-width:820px;font-size:11px}}
            .tk-report-table th{{background:#f8fafc;color:#475569;text-align:left;font-weight:600;padding:9px;border-bottom:1px solid #e2e8f0}} .tk-report-table td{{background:#fff;color:#334155;padding:9px;border-bottom:1px solid #f1f5f9;white-space:nowrap}} .tk-report-table tbody tr:hover td{{background:#f8fafc}}
            .tk-report-status{{display:inline-block;background:#fee2e2;color:#991b1b;border-radius:999px;padding:3px 8px;font-weight:700}}
            .tk-report-insight{{font-size:12px;color:#475569;line-height:1.6;margin-top:14px}} .tk-report-note{{font-size:11px;color:#64748b;border-top:1px solid #f1f5f9;padding-top:10px;margin-top:12px}}
            .tk-report-empty{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:13px;color:#475569;font-size:13px}}
            @media(max-width:640px){{.tk-report-kpis{{grid-template-columns:repeat(2,minmax(0,1fr))}}.tk-report-rankings{{grid-template-columns:1fr}}.tk-report-section{{padding:14px}}}}
            </style>
            <section class='tk-report-section'>
                <div class='tk-report-kpis'>{tk_kpis}</div>
            </section>
        """, unsafe_allow_html=True)
        st.markdown("<div class='tk-report-ranking-title' style='margin-top:14px'>Tren Ketidakhadiran TK</div>", unsafe_allow_html=True)
        st.altair_chart(tk_trend_chart, use_container_width=True)
        st.markdown(f"""
            <section class='tk-report-section'>
                <div class='tk-report-rankings'>
                    <div class='tk-report-ranking'><div class='tk-report-ranking-title'>Pegawai dengan TK Tertinggi</div>{employee_rows}</div>
                    <div class='tk-report-ranking'><div class='tk-report-ranking-title'>OPD dengan Ketidakhadiran TK</div>{opd_rows}</div>
                </div>
{detail_section_html}
                <div class='tk-report-insight'><strong>💡 Insight Ketidakhadiran</strong><br>• {escape(str(peak_month['Bulan']))} memiliki jumlah TK tertinggi sebanyak {int(peak_month['Hari_TK'])} hari.<br>• {total_tk_employees} pegawai tercatat memiliki minimal satu TK.<br>• {escape(str(peak_opd['Unit Kerja']).title())} memiliki jumlah TK tertinggi pada periode aktif.</div>
                <div class='tk-report-note'>Status TK pada laporan ini merupakan data presensi dan tidak secara otomatis menunjukkan pelanggaran disiplin. Verifikasi alasan ketidakhadiran tetap diperlukan sebelum dilakukan tindak lanjut.</div>
            </section>
        """, unsafe_allow_html=True)

    st.markdown("<div class='section-title'>Pegawai dengan Pola Menonjol</div>", unsafe_allow_html=True)
    standout = (
        filtered.groupby(["NIP", "Nama Pegawai", "Unit Kerja"], as_index=False)
        .agg(TK=("TK", "sum"), Terlambat=("Terlambat", "sum"))
        .sort_values(["TK", "Terlambat", "Nama Pegawai"], ascending=[False, False, True], kind="stable")
        .head(5)
    )
    standout_rows = "".join(
        f"<div class='attendance-standout-row'><span>{rank:02d}</span><div><strong>{escape(str(row['Nama Pegawai']).title())}</strong><small>{escape(str(row['Unit Kerja']).title())}</small><em>TK {int(row['TK'])} hari • Terlambat {int(row['Terlambat'])} kali</em></div></div>"
        for rank, (_, row) in enumerate(standout.iterrows(), start=1)
    )
    st.markdown(f"""
        <style>
        .attendance-standout{{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:8px 16px;box-shadow:0 3px 12px rgba(15,23,42,.035)}}
        .attendance-standout-row{{display:grid;grid-template-columns:28px 1fr;gap:8px;padding:11px 0;border-bottom:1px solid #f1f5f9}}.attendance-standout-row:last-child{{border-bottom:0}}
        .attendance-standout-row>span{{font-size:11px;font-weight:700;color:#64748b}}.attendance-standout-row strong{{display:block;font-size:13px;color:#173b63}}.attendance-standout-row small{{display:block;font-size:11px;color:#64748b;margin-top:2px}}.attendance-standout-row em{{display:block;font-size:11px;font-style:normal;color:#475569;margin-top:5px}}
        </style><div class='attendance-standout'>{standout_rows}</div>
    """, unsafe_allow_html=True)

    main_insights = []
    if trend_insight:
        main_insights.append(trend_insight)
    if int(day_analysis.sum()) > 0:
        main_insights.append(f"📅 {day_analysis.idxmax()} merupakan hari keterlambatan tertinggi dengan {int(day_analysis.max())} kejadian.")
    if len(peak) and int(peak.max()) > 0:
        main_insights.append(f"⏰ Keterlambatan paling banyak terjadi pada rentang {peak.idxmax()} sebanyak {int(peak.max())} kejadian.")
    if total_tk_report > 0 and len(main_insights) < 3:
        main_insights.append(f"🚫 {peak_month['Bulan']} memiliki jumlah TK tertinggi pada periode aktif.")
    st.markdown("<div class='section-title'>Insight Utama</div>", unsafe_allow_html=True)
    for insight in main_insights[:3]:
        st.markdown(f"<div style='font-size:13px;color:#475569;margin:7px 0'>{escape(str(insight))}</div>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)


def show_attendance_analysis_page() -> None:
    """Analisis pola dan tren dari satu scope data harian canonical."""
    inject_dashboard_css()
    data = load_employee_data()
    if data.empty:
        st.info("Tidak terdapat data presensi pada parameter yang dipilih.")
        return

    months = available_months(data)
    weekdays = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat"]
    years = sorted(pd.to_numeric(data["Tahun"], errors="coerce").dropna().astype(int).unique().tolist())
    units = sorted(data["Unit Kerja"].dropna().astype(str).unique().tolist())
    present_types = set(data.get("Jenis Pegawai", pd.Series(dtype=str)).dropna().astype(str))
    employee_types = [item for item in ["PNS", "PPPK", "Belum Diketahui"] if item in present_types]

    st.markdown("""
    <style>
      .attendance-analysis-page{max-width:1480px;margin:0 auto;color:#0f172a}.attendance-page-title{font-size:29px;font-weight:700;color:#173b63;margin:0 0 4px}.attendance-page-subtitle{font-size:14px;color:#64748b;margin-bottom:14px}
      .attendance-meta-strip{font-size:12px;color:#64748b;background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:9px 12px;margin:4px 0 20px}.attendance-section-title{font-size:18px;font-weight:700;color:#173b63;margin:25px 0 4px}.attendance-section-note{font-size:12px;color:#64748b;margin:0 0 11px}
      .attendance-kpi-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.attendance-kpi-card,.attendance-insight{background:#fff;border:1px solid #e2e8f0;border-radius:14px;box-shadow:0 3px 12px rgba(15,23,42,.035)}.attendance-kpi-card{padding:16px 18px}.attendance-kpi-value{font-size:30px;font-weight:750;line-height:1.1;color:#173b63}.attendance-kpi-label{font-size:12px;color:#64748b;margin-top:6px}.attendance-insight{font-size:13px;color:#475569;padding:11px 14px;margin:7px 0}
      @media(max-width:700px){.attendance-kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.attendance-page-title{font-size:26px}}
    </style><div class='attendance-analysis-page'></div>
    """, unsafe_allow_html=True)
    st.markdown("<h1 class='attendance-page-title'>Analisis Presensi</h1><div class='attendance-page-subtitle'>Analisis pola, tren, dan karakteristik presensi pegawai.</div>", unsafe_allow_html=True)

    filter_year, filter_month, filter_opd, filter_type, filter_reset = st.columns([1, 1, 2, 1.4, .7])
    with filter_year:
        chosen_year = st.selectbox("Tahun", ["Semua Tahun"] + years, key="attendance_filter_year")
    with filter_month:
        chosen_month = st.selectbox("Bulan", ["Semua Bulan"] + months, key="attendance_filter_month")
    with filter_opd:
        chosen_unit = st.selectbox("OPD", ["Semua OPD"] + units, key="attendance_filter_opd")
    with filter_type:
        chosen_employee_type = st.selectbox("Jenis Pegawai", ["Semua Jenis Pegawai"] + employee_types, key="attendance_filter_employee_type")
    with filter_reset:
        st.write("")
        if st.button("Reset", key="attendance_filter_reset", use_container_width=True):
            for key in ["attendance_filter_year", "attendance_filter_month", "attendance_filter_opd", "attendance_filter_employee_type", "attendance_trend_metric", "attendance_trend_granularity"]:
                st.session_state.pop(key, None)
            st.rerun()

    filtered = apply_filters(data, year=chosen_year, month=chosen_month, opd=chosen_unit, employee_type=chosen_employee_type)
    daily_filtered = _daily_attendance_scope(year=chosen_year, month=chosen_month, opd=chosen_unit, employee_type=chosen_employee_type)
    if filtered.empty or daily_filtered.empty:
        st.info("Tidak terdapat data presensi pada parameter yang dipilih.")
        return

    indicators = prepare_daily_indicators(daily_filtered)
    summary = summarize_attendance_indicators(daily_filtered)
    period_months = [month for month in months if month in set(filtered["Bulan"].astype(str))]
    period_years = sorted(pd.to_numeric(filtered["Tahun"], errors="coerce").dropna().astype(int).unique().tolist())
    month_text = period_months[0] if len(period_months) == 1 else f"{period_months[0]}–{period_months[-1]}" if period_months else "Periode aktif"
    year_text = str(period_years[0]) if len(period_years) == 1 else f"{period_years[0]}–{period_years[-1]}" if period_years else ""
    opd_text = "Semua OPD" if chosen_unit == "Semua OPD" else chosen_unit.title()
    calendar_scope = data.copy()
    if chosen_year != "Semua Tahun":
        calendar_scope = calendar_scope[pd.to_numeric(calendar_scope["Tahun"], errors="coerce").eq(int(chosen_year))]
    if chosen_month != "Semua Bulan":
        calendar_scope = calendar_scope[calendar_scope["Bulan"].eq(chosen_month)]
    month_numbers = {name: index for index, name in enumerate(ANALYTICS_MONTHS, start=1)}
    calendar_periods = (
        calendar_scope[["Tahun", "Bulan"]].drop_duplicates()
        .assign(
            _tahun=lambda frame: pd.to_numeric(frame["Tahun"], errors="coerce"),
            _bulan=lambda frame: frame["Bulan"].map(month_numbers),
        )
        .dropna(subset=["_tahun", "_bulan"])
    )
    calendar_metrics = summarize_work_calendar_period(
        calendar_periods[["_tahun", "_bulan"]].itertuples(index=False, name=None)
    )
    st.markdown(
        f"<div class='attendance-meta-strip'>Periode aktif: <strong>{escape((month_text + ' ' + year_text).strip())}</strong> • "
        f"<strong>{escape(opd_text)}</strong> • <strong>{filtered['NIP'].nunique():,}</strong> pegawai • "
        f"<strong>{calendar_metrics['workdays']:,}</strong> Hari Kerja • "
        f"<strong>{calendar_metrics['required_days']:,}</strong> Hari Wajib Presensi</div>".replace(",", "."),
        unsafe_allow_html=True,
    )

    def id_number(value: int) -> str:
        return f"{int(value):,}".replace(",", ".")

    compliance = float(summary["compliance_percentage"])
    physical = float(summary["physical_attendance_percentage"])
    if not (0 <= compliance <= 100 and 0 <= physical <= 100):
        st.error("Persentase presensi di luar rentang 0–100. Periksa duplikasi atau klasifikasi status sumber.")
        return
    total_tk = int(filtered["TK"].sum())
    late_total = int(summary["late_events"])
    kpis = [
        (f"{compliance:.1f}%".replace(".", ","), "Kepatuhan Presensi", "Kepatuhan Presensi menunjukkan persentase hari wajib presensi yang memiliki status sah, yaitu Hadir, Cuti, WFH, DL, SL, SK, MR, atau PBT."),
        (f"{physical:.1f}%".replace(".", ","), "Kehadiran Fisik", "Persentase hari wajib presensi ketika pegawai benar-benar hadir."),
        (f"{id_number(total_tk)} hari", "TK", "Total hari Tanpa Keterangan dari source TK existing pada periode aktif."),
        (f"{id_number(late_total)} kali", "Keterlambatan", "Indikator disiplin waktu yang dihitung terpisah dari Kepatuhan Presensi."),
    ]
    st.markdown("<div class='attendance-section-title'>Ringkasan Presensi</div><div class='attendance-section-note'>Empat indikator utama untuk parameter aktif.</div><div class='attendance-kpi-grid'>" + "".join(f"<div class='attendance-kpi-card' title='{escape(help_text)}'><div class='attendance-kpi-value'>{value}</div><div class='attendance-kpi-label'>{label}</div></div>" for value, label, help_text in kpis) + "</div>", unsafe_allow_html=True)

    composition_values = {
        "Hadir": int(indicators["Hadir Fisik"].sum()), "Cuti": int(indicators["Cuti"].sum()),
        "WFH": int(indicators["WFH"].sum()), "DL": int(indicators["DL"].sum()), "TK": total_tk,
    }
    composition = pd.DataFrame({"Status": list(composition_values), "Jumlah": list(composition_values.values())})
    composition_order = list(composition_values)
    composition_colors = alt.Scale(domain=composition_order, range=["#10b981", "#8b5cf6", "#06b6d4", "#2563eb", "#94a3b8", "#ef4444"])
    composition_chart = alt.Chart(composition).mark_bar(cornerRadiusEnd=5).encode(
        y=alt.Y("Status:N", sort=composition_order, title=None), x=alt.X("Jumlah:Q", title="Jumlah hari-pegawai"),
        color=alt.Color("Status:N", scale=composition_colors, legend=None),
        tooltip=["Status:N", alt.Tooltip("Jumlah:Q", format=",", title="Hari-pegawai")],
    ).properties(height=190)
    st.markdown("<div class='attendance-section-title'>Komposisi Presensi</div><div class='attendance-section-note'>Satuan hari-pegawai; setiap status dihitung satu kali per NIP dan tanggal. Keterlambatan tetap indikator terpisah.</div>", unsafe_allow_html=True)
    st.altair_chart(composition_chart, use_container_width=True)

    st.markdown("<div class='attendance-section-title'>Tren Presensi</div><div class='attendance-section-note'>Seluruh titik tren mengikuti filter periode aktif yang sama dengan KPI.</div>", unsafe_allow_html=True)
    metric_col, granularity_col = st.columns([1, 2])
    available_metric_labels = list(TREND_METRICS)
    with metric_col:
        trend_metric_label = st.selectbox("Metric", available_metric_labels, key="attendance_trend_metric")
    with granularity_col:
        trend_granularity = st.radio("Granularitas", ["Harian", "Mingguan", "Bulanan"], horizontal=True, index=2, key="attendance_trend_granularity")
    metric_key = TREND_METRICS[trend_metric_label]
    trend_data = aggregate_attendance_trend(daily_filtered, trend_granularity)
    percentage_metric = metric_key in {"compliance_percentage", "physical_attendance_percentage"}
    metric_unit = "%" if percentage_metric else "hari" if metric_key != "late_events" else "kali"
    colors = {"compliance_percentage": "#10b981", "physical_attendance_percentage": "#2563eb", "late_events": "#f59e0b", "tk_days": "#ef4444", "leave_days": "#8b5cf6", "wfh_days": "#06b6d4", "official_duty_days": "#64748b", "sick_or_permitted_days": "#94a3b8"}
    mark = alt.Chart(trend_data).mark_line(point=True, strokeWidth=3, color=colors[metric_key])
    trend_chart = mark.encode(
        x=alt.X("PeriodLabel:N", title=None, sort=trend_data["PeriodLabel"].tolist(), axis=alt.Axis(labelAngle=-25 if len(trend_data) > 12 else 0)),
        y=alt.Y(f"{metric_key}:Q", title=f"{trend_metric_label} ({metric_unit})", scale=alt.Scale(domain=[0, 100]) if percentage_metric else alt.Undefined),
        tooltip=[alt.Tooltip("PeriodLabel:N", title="Periode"), alt.Tooltip(f"{metric_key}:Q", format=".1f" if percentage_metric else ",", title=f"{trend_metric_label} ({metric_unit})")],
    ).properties(height=250)
    st.altair_chart(trend_chart, use_container_width=True)
    trend_insight = None
    if len(trend_data) >= 2:
        change = float(trend_data.iloc[-1][metric_key] - trend_data.iloc[-2][metric_key])
        if change != 0:
            amount = f"{abs(change):.1f}".replace(".", ",") if percentage_metric else id_number(round(abs(change)))
            trend_insight = f"{trend_metric_label} {'meningkat' if change > 0 else 'menurun'} {amount} {'poin' if percentage_metric else metric_unit} dibanding periode sebelumnya."
            st.caption(trend_insight)

    late_daily = indicators[indicators["Terlambat"].fillna(False).astype(bool)].copy()
    day_analysis = late_daily.groupby("Hari").size().reindex(weekdays, fill_value=0)
    day_chart = day_analysis.rename("Keterlambatan").reset_index()
    arrival_parts = late_daily.get("Jam_Masuk", pd.Series("", index=late_daily.index)).astype(str).str.split(":")
    arrival_minutes = pd.to_numeric(arrival_parts.str[0], errors="coerce") * 60 + pd.to_numeric(arrival_parts.str[1], errors="coerce")
    peak_labels = ["07.31–07.45", "07.46–08.00", "08.01–08.30", "> 08.30"]
    late_daily["Rentang Waktu"] = pd.cut(arrival_minutes, bins=[450, 465, 480, 510, 1440], labels=peak_labels, include_lowest=False)
    peak = late_daily["Rentang Waktu"].value_counts().reindex(peak_labels, fill_value=0)
    st.markdown("<div class='attendance-section-title'>Pola Waktu</div><div class='attendance-section-note'>Distribusi keterlambatan menurut hari kerja dan jam datang; ini pola mingguan, bukan tren waktu.</div>", unsafe_allow_html=True)
    day_col, time_col = st.columns(2)
    with day_col:
        st.markdown("**Hari Rawan**")
        st.altair_chart(alt.Chart(day_chart).mark_bar(color="#f59e0b").encode(x=alt.X("Hari:N", sort=weekdays, title=None), y=alt.Y("Keterlambatan:Q", title="Kejadian (kali)"), tooltip=["Hari:N", alt.Tooltip("Keterlambatan:Q", format=",")]).properties(height=220), use_container_width=True)
    with time_col:
        st.markdown("**Waktu Rawan**")
        peak_chart = peak.rename_axis("Rentang Waktu").reset_index(name="Keterlambatan")
        st.altair_chart(alt.Chart(peak_chart).mark_bar(color="#2563eb").encode(y=alt.Y("Rentang Waktu:N", sort=peak_labels, title=None), x=alt.X("Keterlambatan:Q", title="Kejadian (kali)"), tooltip=["Rentang Waktu:N", alt.Tooltip("Keterlambatan:Q", format=",")]).properties(height=220), use_container_width=True)

    heatmap = late_daily.groupby(["Nama_Bulan", "Hari"], observed=False).size().rename("Keterlambatan").reset_index()
    heatmap["Nama_Bulan"] = pd.Categorical(heatmap["Nama_Bulan"], categories=period_months, ordered=True)
    heatmap = heatmap[heatmap["Nama_Bulan"].notna()]
    st.markdown("<div class='attendance-section-title'>Heatmap Presensi</div><div class='attendance-section-note'>Intensitas kejadian keterlambatan menurut bulan dan hari kerja.</div>", unsafe_allow_html=True)
    st.altair_chart(alt.Chart(heatmap).mark_rect(cornerRadius=3).encode(x=alt.X("Hari:N", sort=weekdays, title=None), y=alt.Y("Nama_Bulan:N", sort=period_months, title=None), color=alt.Color("Keterlambatan:Q", scale=alt.Scale(scheme="blues"), legend=None), tooltip=[alt.Tooltip("Nama_Bulan:N", title="Bulan"), "Hari:N", alt.Tooltip("Keterlambatan:Q", format=",", title="Kejadian")]).properties(height=max(150, len(period_months) * 34)), use_container_width=True)

    standout = indicators.groupby(["NIP", "Nama", "Unit Kerja"], as_index=False, dropna=False).agg(Wajib=("Wajib Presensi", "sum"), Hadir_Fisik=("Hadir Fisik", "sum"), TK=("TK", "sum"), Terlambat=("Terlambat", "sum"))
    standout["Kehadiran Fisik"] = (standout["Hadir_Fisik"] / standout["Wajib"].replace(0, pd.NA) * 100).fillna(0)
    standout = standout.sort_values(["TK", "Terlambat", "Kehadiran Fisik", "Nama"], ascending=[False, False, True, True], kind="stable").head(7)
    display_standout = standout.rename(columns={"Nama": "Nama Pegawai"})[["Nama Pegawai", "NIP", "Unit Kerja", "TK", "Terlambat", "Kehadiran Fisik"]].copy()
    display_standout["TK"] = display_standout["TK"].map(lambda value: f"{int(value)} hari")
    display_standout["Terlambat"] = display_standout["Terlambat"].map(lambda value: f"{int(value)} kali")
    display_standout["Kehadiran Fisik"] = display_standout["Kehadiran Fisik"].map(lambda value: f"{value:.1f}%".replace(".", ","))
    st.markdown("<div class='attendance-section-title'>Pegawai dengan Pola Menonjol</div><div class='attendance-section-note'>Outlier analitis berdasarkan TK, keterlambatan, dan Kehadiran Fisik; bukan penilaian risiko.</div>", unsafe_allow_html=True)
    st.dataframe(display_standout, hide_index=True, use_container_width=True)

    monthly = aggregate_attendance_trend(daily_filtered, "Bulanan")
    insights = []
    if trend_insight:
        insights.append(trend_insight)
    if int(day_analysis.sum()) > 0:
        insights.append(f"Keterlambatan paling banyak terjadi pada {day_analysis.idxmax()}: {id_number(day_analysis.max())} kali.")
    if not monthly.empty and int(monthly["tk_days"].max()) > 0:
        peak_month = monthly.loc[monthly["tk_days"].idxmax()]
        insights.append(f"TK tertinggi terjadi pada {peak_month['PeriodLabel']}: {id_number(peak_month['tk_days'])} hari.")
    if insights:
        st.markdown("<div class='attendance-section-title'>Insight</div>" + "".join(f"<div class='attendance-insight'>• {escape(item)}</div>" for item in insights[:3]), unsafe_allow_html=True)
    if st.button("Buka Laporan Ketidakhadiran →", key="attendance_go_tk_report"):
        st.session_state["navigate_to_page"] = "Laporan Ketidakhadiran"
        st.rerun()


def show_opd_analysis_page_legacy() -> None:
    inject_dashboard_css()
    data = load_employee_data()
    month_order = ["Januari", "Februari", "Maret"]

    with st.sidebar:
        st.markdown("# 🛡️ EWS Kehadiran")
        st.caption("Analisis presensi per OPD")
        st.divider()
        st.markdown("#### Filter Analisis OPD")
        chosen_month = st.selectbox("Periode bulan", month_order, key="opd_month")
        st.divider()
        st.caption(f"Login sebagai: {st.session_state.username}")
        st.button("Keluar", on_click=logout, use_container_width=True, key="opd_logout")

    monthly_data = data[data["Bulan"] == chosen_month].copy()
    opd_summary = monthly_data.groupby("Unit Kerja").agg(
        Pegawai=("NIP", "nunique"),
        Hari_Kerja=("Hari Kerja", "sum"),
        TK_TB=("TK", "sum"),
        Cuti=("Cuti", "sum"),
        Keterlambatan=("Terlambat", "sum"),
    )
    opd_summary["Persentase Kehadiran"] = (
        (opd_summary["Hari_Kerja"] - opd_summary["TK_TB"] - opd_summary["Cuti"])
        / opd_summary["Hari_Kerja"]
        * 100
    ).clip(lower=0)
    opd_summary = opd_summary.sort_values("Persentase Kehadiran", ascending=False)
    opd_ranking = opd_summary.reset_index()
    opd_ranking.index = opd_ranking.index + 1
    opd_ranking.index.name = "Peringkat"

    opd_trend_source = data.groupby(["Bulan", "Unit Kerja"]).agg(
        Hari_Kerja=("Hari Kerja", "sum"),
        TK_TB=("TK", "sum"),
        Cuti=("Cuti", "sum"),
    )
    opd_trend_source["Persentase Kehadiran"] = (
        (opd_trend_source["Hari_Kerja"] - opd_trend_source["TK_TB"] - opd_trend_source["Cuti"])
        / opd_trend_source["Hari_Kerja"]
        * 100
    ).clip(lower=0)
    opd_trend = opd_trend_source["Persentase Kehadiran"].unstack("Unit Kerja").reindex(month_order)

    st.markdown("<p class='dashboard-title'>Analisis OPD</p>", unsafe_allow_html=True)
    st.markdown(
        f"<div class='page-context-grid'><div class='page-context-card risk-context-card'>Perbandingan presensi antar OPD</div><div class='page-context-card'>Periode: {chosen_month}</div></div>",
        unsafe_allow_html=True,
    )
    overview_1, overview_2, overview_3 = st.columns(3)
    overview_1.metric("OPD Terbaik", opd_ranking.iloc[0]["Unit Kerja"], f"{opd_ranking.iloc[0]['Persentase Kehadiran']:.1f}% kehadiran")
    overview_2.metric("Rata-rata Kehadiran OPD", f"{opd_ranking['Persentase Kehadiran'].mean():.1f}%")
    overview_3.metric("Total TK", f"{int(opd_ranking['TK_TB'].sum())} hari")

    st.markdown("<div class='section-title'>Ranking OPD</div>", unsafe_allow_html=True)
    st.dataframe(
        opd_ranking[["Unit Kerja", "Pegawai", "Persentase Kehadiran", "TK_TB", "Keterlambatan"]],
        use_container_width=True,
        column_config={
            "Persentase Kehadiran": st.column_config.NumberColumn("Kehadiran", format="%.1f%%"),
            "TK_TB": st.column_config.NumberColumn("TK", format="%d hari"),
            "Keterlambatan": st.column_config.NumberColumn("Keterlambatan", format="%d kali"),
        },
    )

    attendance_col, absence_col, late_col = st.columns(3)
    with attendance_col:
        st.markdown("<div class='section-title'>Persentase Kehadiran per OPD</div>", unsafe_allow_html=True)
        st.bar_chart(opd_summary["Persentase Kehadiran"], color="#059669", height=270)
    with absence_col:
        st.markdown("<div class='section-title'>TK per OPD</div>", unsafe_allow_html=True)
        st.bar_chart(opd_summary["TK_TB"], color="#dc2626", height=270)
    with late_col:
        st.markdown("<div class='section-title'>Keterlambatan per OPD</div>", unsafe_allow_html=True)
        st.bar_chart(opd_summary["Keterlambatan"], color="#f97316", height=270)

    st.markdown("<div class='section-title'>Tren Presensi OPD</div>", unsafe_allow_html=True)
    st.caption("Perbandingan persentase kehadiran setiap OPD dari Januari hingga Maret.")
    st.line_chart(opd_trend, height=320)


def show_opd_analysis_page_legacy_v3() -> None:
    inject_dashboard_css()
    data = apply_employee_active_status(load_employee_data())
    months = available_months(data)
    with st.sidebar:
        st.markdown("# 🛡️ EWS Kehadiran")
        st.caption("Kinerja dan risiko OPD")
        st.divider()
        chosen_month = st.selectbox("Periode bulan", ["Semua Bulan"] + months, key="opd_page_month")
        st.divider()
        st.caption(f"Login sebagai: {st.session_state.username}")
        st.button("Keluar", on_click=logout, use_container_width=True, key="opd_page_logout")

    filtered = data if chosen_month == "Semua Bulan" else data[data["Bulan"] == chosen_month]
    filtered = filtered.copy()
    filtered["Risk Score"] = filtered.apply(lambda row: calculate_risk_score(row["TK"], row["Terlambat"]), axis=1)
    filtered["Status"] = filtered["Risk Score"].apply(risk_status)
    filtered["Penyebab"] = filtered.apply(lambda row: warning_indicators(row["TK"], row["Terlambat"]), axis=1)
    status_icons = {"Normal": "🟢", "Waspada": "🟡", "Tinggi": "🟠", "Kritis": "🔴"}

    opd = filtered.groupby("Unit Kerja").agg(
        Pegawai=("NIP", "nunique"),
        Hari_Kerja=("Hari Kerja", "sum"),
        TK_TB=("TK", "sum"),
        Keterlambatan=("Terlambat", "sum"),
        Risk_Score=("Risk Score", "mean"),
    )
    opd["Kehadiran"] = ((opd["Hari_Kerja"] - opd["TK_TB"] - filtered.groupby("Unit Kerja")["Cuti"].sum()) / opd["Hari_Kerja"] * 100).clip(lower=0)
    opd["Pegawai Berisiko"] = filtered.groupby("Unit Kerja")["Status"].apply(lambda values: (values != "Normal").sum())
    opd = opd.sort_values("Risk_Score", ascending=False).reset_index()
    opd["Risk Score"] = opd["Risk_Score"].round(0).astype(int)
    opd["Status"] = opd["Risk Score"].apply(risk_status)
    opd["Status"] = opd["Status"].map(lambda value: f"{status_icons[value]} {value}")
    opd["Gap"] = opd["Kehadiran"] - 95

    st.markdown("<h1 class='dashboard-title'>Analisis OPD</h1>", unsafe_allow_html=True)
    st.markdown(f"<div class='page-context-grid'><div class='page-context-card risk-context-card'>Analisis kinerja OPD</div><div class='page-context-card'>Periode: {chosen_month}</div></div>", unsafe_allow_html=True)
    st.markdown("<div class='section-title'>OPD dengan Risiko Tertinggi</div>", unsafe_allow_html=True)
    st.dataframe(
        opd[["Unit Kerja", "Risk Score", "Status", "Pegawai Berisiko"]],
        hide_index=True,
        use_container_width=True,
        column_config={"Risk Score": st.column_config.NumberColumn("Risk Score", format="%d"), "Status": st.column_config.TextColumn("Status", width="medium")},
    )

    st.markdown("<div class='section-title'>Perbandingan Kinerja OPD</div>", unsafe_allow_html=True)
    st.dataframe(
        opd[["Unit Kerja", "Kehadiran", "TK_TB", "Keterlambatan", "Risk Score"]],
        hide_index=True,
        use_container_width=True,
        column_config={
            "Kehadiran": st.column_config.NumberColumn("Kehadiran", format="%.1f%%"),
            "TK_TB": st.column_config.NumberColumn("TK", format="%d hari"),
            "Keterlambatan": st.column_config.NumberColumn("Keterlambatan", format="%d kali"),
            "Risk Score": st.column_config.NumberColumn("Risk Score", format="%d"),
        },
    )

    st.markdown("<div class='section-title'>Target vs Realisasi OPD</div>", unsafe_allow_html=True)
    target_table = opd[["Unit Kerja", "Kehadiran", "Gap"]].rename(columns={"Kehadiran": "Realisasi"})
    target_table.insert(1, "Target", 95.0)
    st.dataframe(
        target_table,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Target": st.column_config.NumberColumn("Target", format="%.1f%%"),
            "Realisasi": st.column_config.NumberColumn("Realisasi", format="%.1f%%"),
            "Gap": st.column_config.NumberColumn("Gap", format="%+.1f%%"),
        },
    )

    st.markdown("<div class='section-title'>Pegawai Berisiko per OPD</div>", unsafe_allow_html=True)
    chosen_opd = st.selectbox("Pilih OPD untuk melihat pegawai berisiko", opd["Unit Kerja"].tolist(), key="opd_drilldown")
    drilldown = filtered[(filtered["Unit Kerja"] == chosen_opd) & (filtered["Status"] != "Normal")].sort_values("Risk Score", ascending=False)
    if drilldown.empty:
        st.success("Tidak ada pegawai berisiko pada OPD ini.")
    else:
        st.dataframe(
            drilldown[["Nama Pegawai", "NIP", "Risk Score", "TK", "Terlambat", "Penyebab", "Status"]],
            hide_index=True,
            use_container_width=True,
            column_config={
                "Risk Score": st.column_config.NumberColumn("Risk Score", format="%d"),
            "TK": st.column_config.NumberColumn("TK", format="%d hari"),
                "Terlambat": st.column_config.NumberColumn("Terlambat", format="%d kali"),
                "Penyebab": st.column_config.TextColumn("Penyebab", width="large"),
            },
        )


def show_opd_analysis_page_legacy_v4() -> None:
    inject_dashboard_css()
    data = apply_employee_active_status(load_employee_data())
    months = available_months(data)
    threshold = 85.0
    with st.sidebar:
        chosen_month = st.selectbox("Periode bulan", ["Semua Bulan"] + months, key="opd_page_month_v2")
        units = ["Semua Unit"] + sorted(data["Unit Kerja"].unique().tolist())
        chosen_unit = st.selectbox("OPD", units, key="opd_page_unit_v2")
    filtered = data.copy()
    if chosen_month != "Semua Bulan": filtered = filtered[filtered["Bulan"] == chosen_month]
    if chosen_unit != "Semua Unit": filtered = filtered[filtered["Unit Kerja"] == chosen_unit]
    st.markdown("<h1 class='ews-page-title'>Analisis OPD</h1>", unsafe_allow_html=True)
    st.markdown("<div class='ews-page-subtitle'>Perbandingan dan evaluasi presensi antar perangkat daerah berdasarkan periode yang dipilih.</div>", unsafe_allow_html=True)
    st.markdown(f"<div class='ews-filter-row'><span>Periode: <strong>{chosen_month}</strong></span><span class='ews-filter-chip'>OPD: <strong>{'Semua OPD' if chosen_unit == 'Semua Unit' else chosen_unit}</strong></span></div>", unsafe_allow_html=True)
    if filtered.empty:
        st.info("Tidak terdapat data presensi OPD pada periode yang dipilih."); return
    filtered = filtered.copy()
    opd = filtered.groupby("Unit Kerja").agg(Pegawai=("NIP", "nunique"), Hari_Kerja=("Hari Kerja", "sum"), TK=("TK", "sum"), Terlambat=("Terlambat", "sum"), Cuti=("Cuti", "sum"), WFH=("WFH", "sum"), DL=("DL", "sum")).reset_index()
    opd["Kepatuhan"] = ((opd["Hari_Kerja"] - opd["TK"]).clip(lower=0) / opd["Hari_Kerja"].replace(0, 1) * 100).clip(lower=0)
    st.markdown("<div class='section-title'>Ringkasan OPD</div>", unsafe_allow_html=True)
    best = opd.loc[opd["Kepatuhan"].idxmax()]; attention = int((opd["Kepatuhan"] < threshold).sum())
    summary_cols = st.columns(4)
    summary_cols[0].metric("Jumlah OPD", len(opd)); summary_cols[1].metric("Rata-rata Kepatuhan", f"{opd['Kepatuhan'].mean():.1f}%"); summary_cols[2].metric("Kepatuhan Tertinggi", f"{best['Kepatuhan']:.1f}%", best["Unit Kerja"]); summary_cols[3].metric("OPD Perlu Perhatian", attention)
    st.markdown("<div class='section-title'>Perbandingan OPD</div>", unsafe_allow_html=True)
    ranking_type = st.selectbox("Urutkan berdasarkan", ["Kepatuhan Tertinggi", "Kepatuhan Terendah", "TK Tertinggi", "Keterlambatan Tertinggi"], key="opd_rank_type_v2")
    limit_label = st.selectbox("Tampilkan", ["Top 10", "Top 5", "Semua OPD"], key="opd_rank_limit_v2")
    metric = "Kepatuhan" if "Kepatuhan" in ranking_type else "TK" if ranking_type == "TK Tertinggi" else "Terlambat"
    ranked = opd.sort_values(metric, ascending=(ranking_type == "Kepatuhan Terendah"))
    if limit_label != "Semua OPD": ranked = ranked.head(int(limit_label.split()[1]))
    chart_data = ranked.sort_values(metric, ascending=True).set_index("Unit Kerja")[[metric]]
    st.bar_chart(chart_data, color="#2563eb", height=300)
    table = ranked.reset_index(drop=True); table.insert(0, "No", range(1, len(table) + 1)); table = table[["No", "Unit Kerja", "Pegawai", "Kepatuhan", "TK", "Terlambat"]].copy(); table["Kepatuhan"] = table["Kepatuhan"].map(lambda v: f"{v:.1f}%"); table["TK"] = table["TK"].map(lambda v: f"{int(v)} hari"); table["Terlambat"] = table["Terlambat"].map(lambda v: f"{int(v)} kali")
    st.dataframe(table, hide_index=True, use_container_width=True, column_config={"No": st.column_config.NumberColumn(width="small"), "Unit Kerja": st.column_config.TextColumn(width="large"), "Pegawai": st.column_config.NumberColumn(width="small"), "Kepatuhan": st.column_config.TextColumn(width="medium"), "TK": st.column_config.TextColumn(width="small"), "Terlambat": st.column_config.TextColumn(width="medium")})
    attention_df = opd[opd["Kepatuhan"] < threshold].copy()
    st.markdown("<div class='section-title'>OPD yang Memerlukan Perhatian</div>", unsafe_allow_html=True)
    if attention_df.empty: st.success("Tidak ada OPD di bawah ambang perhatian.")
    else:
        attention_df["Indikasi"] = attention_df.apply(lambda row: "Kepatuhan rendah + keterlambatan tinggi" if row["Kepatuhan"] < threshold and row["Terlambat"] >= opd["Terlambat"].median() else "Kepatuhan rendah", axis=1)
        st.dataframe(attention_df[["Unit Kerja", "Kepatuhan", "TK", "Terlambat", "Indikasi"]], hide_index=True, use_container_width=True)
    st.markdown("<div class='section-title'>Tren Presensi OPD</div>", unsafe_allow_html=True)
    trend_mode = st.selectbox("Tampilan Tren", ["Pilih Satu OPD", "Top 5 Kepatuhan", "Bottom 5 Kepatuhan"], key="opd_trend_mode_v2")
    if trend_mode == "Pilih Satu OPD": selected_units = [st.selectbox("Pilih OPD", sorted(opd["Unit Kerja"].tolist()), key="opd_trend_unit_v2")]
    else:
        base = opd.sort_values("Kepatuhan", ascending=trend_mode.startswith("Bottom")).head(5); selected_units = base["Unit Kerja"].tolist()
    trend_source = data[data["Unit Kerja"].isin(selected_units)].copy(); trend_source["Kepatuhan"] = (trend_source["Hari Kerja"] - trend_source["TK"]).clip(lower=0) / trend_source["Hari Kerja"].replace(0, 1) * 100
    trend = trend_source.groupby(["Bulan", "Unit Kerja"])["Kepatuhan"].mean().unstack("Unit Kerja").reindex(months)
    trend_chart = trend.reset_index(); trend_chart["Bulan"] = pd.Categorical(trend_chart["Bulan"], categories=months, ordered=True); trend_chart = trend_chart.sort_values("Bulan"); st.line_chart(trend_chart, x="Bulan", y=selected_units, height=280)
    st.markdown("<div class='section-title'>Detail OPD</div>", unsafe_allow_html=True)
    detail_unit = st.selectbox("Pilih OPD", sorted(opd["Unit Kerja"].tolist()), key="opd_detail_unit_v2"); detail = opd[opd["Unit Kerja"] == detail_unit].iloc[0]
    detail_cols = st.columns(5)
    for col, label, value in zip(detail_cols, ["Jumlah Pegawai", "Kepatuhan", "TK", "Terlambat", "Cuti"], [int(detail["Pegawai"]), f"{detail['Kepatuhan']:.1f}%", f"{int(detail['TK'])} hari", f"{int(detail['Terlambat'])} kali", f"{int(detail['Cuti'])} hari"]): col.metric(label, value)
    st.caption(f"WFH: {int(detail['WFH'])} hari • DL: {int(detail['DL'])} hari")
    detail_source = data[data["Unit Kerja"] == detail_unit].copy(); detail_source["Kepatuhan"] = (detail_source["Hari Kerja"] - detail_source["TK"]).clip(lower=0) / detail_source["Hari Kerja"].replace(0, 1) * 100
    detail_trend = detail_source.groupby("Bulan")["Kepatuhan"].mean().reindex(months).dropna().rename("Kepatuhan").reset_index(); detail_trend["Bulan"] = pd.Categorical(detail_trend["Bulan"], categories=months, ordered=True); detail_trend = detail_trend.sort_values("Bulan"); st.line_chart(detail_trend, x="Bulan", y="Kepatuhan", height=220)
    st.markdown("<div class='section-title'>Insight OPD</div>", unsafe_allow_html=True)
    lowest = opd.loc[opd["Kepatuhan"].idxmin()]; highest_late = opd.loc[opd["Terlambat"].idxmax()]
    total_opd_late = int(opd["Terlambat"].sum())
    highest_late_share = (int(highest_late["Terlambat"]) / total_opd_late * 100) if total_opd_late else 0
    st.markdown(f"🏆 Kepatuhan tertinggi: **{best['Unit Kerja']}** ({best['Kepatuhan']:.1f}%).")
    st.markdown(f"⚠️ Kepatuhan terendah: **{lowest['Unit Kerja']}** ({lowest['Kepatuhan']:.1f}%).")
    st.markdown(f"🕘 Keterlambatan tertinggi: **{highest_late['Unit Kerja']}** ({int(highest_late['Terlambat'])} kali / {highest_late_share:.1f}% dari total keterlambatan OPD).")


def show_opd_analysis_page() -> None:
    """Perbandingan lintas-OPD; analisis time-series dimiliki Analisis Presensi."""
    inject_dashboard_css()
    source = load_employee_data()
    if source.empty:
        st.info("Tidak terdapat data presensi OPD pada parameter yang dipilih.")
        return

    years = sorted(pd.to_numeric(source["Tahun"], errors="coerce").dropna().astype(int).unique().tolist())
    months = available_months(source)
    present_types = set(source.get("Jenis Pegawai", pd.Series(dtype=str)).dropna().astype(str))
    employee_types = [item for item in ["PNS", "PPPK", "Belum Diketahui"] if item in present_types]

    st.markdown("<h1 style='font-size:29px;font-weight:700;color:#173b63;margin:0 0 4px'>Analisis OPD</h1><div style='font-size:14px;color:#64748b;margin-bottom:16px'>Perbandingan posisi dan karakteristik presensi antar perangkat daerah.</div>", unsafe_allow_html=True)
    filter_year, filter_month, filter_type = st.columns([1, 1, 1.4])
    with filter_year:
        chosen_year = st.selectbox("Tahun", ["Semua Tahun"] + years, key="opd_filter_year")
    with filter_month:
        chosen_month = st.selectbox("Periode", ["Semua Bulan"] + months, key="opd_filter_month")
    with filter_type:
        chosen_type = st.selectbox("Jenis Pegawai", ["Semua Jenis Pegawai"] + employee_types, key="opd_filter_employee_type")

    calendar_scope = source.copy()
    if chosen_year != "Semua Tahun":
        calendar_scope = calendar_scope[pd.to_numeric(calendar_scope["Tahun"], errors="coerce").eq(int(chosen_year))]
    if chosen_month != "Semua Bulan":
        calendar_scope = calendar_scope[calendar_scope["Bulan"].eq(chosen_month)]
    month_numbers = {name: index for index, name in enumerate(ANALYTICS_MONTHS, start=1)}
    calendar_periods = (
        calendar_scope[["Tahun", "Bulan"]].drop_duplicates()
        .assign(
            _tahun=lambda frame: pd.to_numeric(frame["Tahun"], errors="coerce"),
            _bulan=lambda frame: frame["Bulan"].map(month_numbers),
        )
        .dropna(subset=["_tahun", "_bulan"])
    )
    period_pairs = list(calendar_periods[["_tahun", "_bulan"]].itertuples(index=False, name=None))
    try:
        period_years = sorted({int(year) for year, _ in period_pairs})
        calendars_available = bool(period_years) and all(
            bool(build_work_calendar(year, allow_pdf_extraction=False).attrs.get("official_available"))
            for year in period_years
        )
        calendar_required_days = (
            summarize_work_calendar_period(period_pairs)["required_days"]
            if calendars_available else None
        )
    except (OSError, ValueError):
        LOGGER.exception("Master kalender untuk tabel Analisis OPD tidak tersedia atau tidak valid")
        calendar_required_days = None

    daily = _daily_attendance_scope(year=chosen_year, month=chosen_month, employee_type=chosen_type)
    if daily.empty:
        st.info("Tidak terdapat data presensi OPD pada parameter yang dipilih.")
        return
    indicators = prepare_daily_indicators(daily)
    opd = indicators.groupby("Unit Kerja", as_index=False, dropna=False).agg(
        Pegawai=("NIP", "nunique"), Wajib=("Wajib Presensi", "sum"),
        Sah=("Status Sah", "sum"), Hadir_Fisik=("Hadir Fisik", "sum"),
        TK=("TK", "sum"), Terlambat=("Terlambat", "sum"),
        Cuti=("Cuti", "sum"), WFH=("WFH", "sum"), DL=("DL", "sum"),
    )
    employees_with_tk = indicators[indicators["TK"]].groupby("Unit Kerja")["NIP"].nunique()
    opd["Pegawai dengan TK"] = opd["Unit Kerja"].map(employees_with_tk).fillna(0).astype(int)
    opd["Kepatuhan Presensi"] = opd["Sah"] / opd["Wajib"].replace(0, pd.NA) * 100
    opd["Kehadiran Fisik"] = opd["Hadir_Fisik"] / opd["Wajib"].replace(0, pd.NA) * 100
    opd[["Kepatuhan Presensi", "Kehadiran Fisik"]] = opd[["Kepatuhan Presensi", "Kehadiran Fisik"]].fillna(0)
    opd["Hari Wajib Presensi Kalender"] = calendar_required_days

    metric_map = {
        "Total TK": "TK", "Pegawai dengan TK": "Pegawai dengan TK",
        "Kepatuhan Presensi": "Kepatuhan Presensi", "Kehadiran Fisik": "Kehadiran Fisik",
        "Keterlambatan": "Terlambat",
    }
    control_metric, control_limit = st.columns([1.5, 1])
    with control_metric:
        metric_label = st.selectbox("Metric Perbandingan", list(metric_map), key="opd_comparison_metric")
    with control_limit:
        limit_label = st.selectbox("Tampilkan", ["Top 5", "Top 10", "Semua OPD"], key="opd_rank_limit")
    metric_key = metric_map[metric_label]
    ascending = metric_key in {"Kepatuhan Presensi", "Kehadiran Fisik"}
    ranked = opd.sort_values([metric_key, "Unit Kerja"], ascending=[ascending, True], kind="stable")
    if limit_label != "Semua OPD":
        ranked = ranked.head(int(limit_label.split()[1]))

    def format_id(value) -> str:
        return f"{int(value):,}".replace(",", ".")

    total_tk = int(indicators["TK"].sum())
    total_employees_with_tk = int(indicators.loc[indicators["TK"], "NIP"].nunique())
    impacted_opds = int((opd["TK"] > 0).sum())
    highest_tk = int(opd["TK"].max()) if not opd.empty else 0
    kpis = [
        (f"{format_id(total_tk)} hari", "Total TK"),
        (f"{format_id(total_employees_with_tk)} pegawai", "Pegawai dengan TK"),
        (f"{len(ranked)} OPD", "OPD Ditampilkan"),
        (f"{format_id(highest_tk)} hari", "TK OPD Tertinggi"),
    ]
    st.markdown("""
    <style>
      .opd-kpi-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:10px 0 24px}.opd-kpi-card,.opd-detail-card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;box-shadow:0 3px 12px rgba(15,23,42,.035)}.opd-kpi-card{padding:16px 18px}.opd-kpi-value{font-size:29px;font-weight:750;color:#173b63}.opd-kpi-label{font-size:12px;color:#64748b;margin-top:6px}.opd-section-title{font-size:18px;font-weight:700;color:#173b63;margin:24px 0 4px}.opd-section-note{font-size:12px;color:#64748b;margin-bottom:10px}.opd-detail-card{padding:15px 18px;margin:10px 0}
      @media(max-width:700px){.opd-kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
    </style>
    """, unsafe_allow_html=True)
    st.markdown("<div class='opd-section-title'>Ringkasan OPD</div><div class='opd-section-note'>Ringkasan lintas-OPD pada parameter aktif.</div><div class='opd-kpi-grid'>" + "".join(f"<div class='opd-kpi-card'><div class='opd-kpi-value'>{value}</div><div class='opd-kpi-label'>{label}</div></div>" for value, label in kpis) + "</div>", unsafe_allow_html=True)

    chart_data = ranked.sort_values(metric_key, ascending=not ascending)
    unit = "%" if metric_key in {"Kepatuhan Presensi", "Kehadiran Fisik"} else "pegawai" if metric_key == "Pegawai dengan TK" else "kali" if metric_key == "Terlambat" else "hari"
    comparison_chart = alt.Chart(chart_data).mark_bar(cornerRadiusEnd=5, color="#2563eb").encode(
        y=alt.Y("Unit Kerja:N", sort=chart_data["Unit Kerja"].tolist(), title=None),
        x=alt.X(f"{metric_key}:Q", title=f"{metric_label} ({unit})"),
        tooltip=[alt.Tooltip("Unit Kerja:N", title="OPD"), alt.Tooltip(f"{metric_key}:Q", format=".1f" if unit == "%" else ",", title=f"{metric_label} ({unit})")],
    ).properties(height=max(210, len(ranked) * 38))
    st.markdown("<div class='opd-section-title'>Perbandingan OPD</div><div class='opd-section-note'>Ranking horizontal antar-OPD; Top 5 dibatasi tepat maksimal lima OPD unik.</div>", unsafe_allow_html=True)
    st.altair_chart(comparison_chart, use_container_width=True)

    table = ranked[["Unit Kerja", "Pegawai", "Hari Wajib Presensi Kalender", "Pegawai dengan TK", "Kepatuhan Presensi", "Kehadiran Fisik", "TK", "Terlambat"]].copy()
    table["Kepatuhan Presensi"] = table["Kepatuhan Presensi"].map(format_percentage_exact)
    table["Kehadiran Fisik"] = table["Kehadiran Fisik"].map(lambda value: f"{value:.1f}%".replace(".", ","))
    table["TK"] = table["TK"].map(lambda value: f"{int(value)} hari")
    table["Terlambat"] = table["Terlambat"].map(lambda value: f"{int(value)} kali")
    st.dataframe(
        table,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Hari Wajib Presensi Kalender": st.column_config.NumberColumn(format="%d", width="medium"),
        },
    )
    if calendar_required_days is None:
        st.warning("Master Kalender Kerja untuk periode aktif belum tersedia atau belum valid.")

    st.markdown("<div class='opd-section-title'>Detail OPD Terpilih</div><div class='opd-section-note'>Komposisi dan pegawai menonjol pada satu OPD, tanpa grafik tren waktu.</div>", unsafe_allow_html=True)
    selected_opd = st.selectbox("Pilih OPD", sorted(opd["Unit Kerja"].astype(str).tolist()), key="opd_detail_selected")
    detail = opd[opd["Unit Kerja"].astype(str).eq(selected_opd)].iloc[0]
    detail_kpis = [
        ("Jumlah Pegawai", f"{int(detail['Pegawai'])} pegawai"),
        ("Kepatuhan Presensi", format_percentage_exact(detail["Kepatuhan Presensi"])),
        ("Kehadiran Fisik", f"{detail['Kehadiran Fisik']:.1f}%".replace(".", ",")),
        ("TK", f"{int(detail['TK'])} hari"),
        ("Keterlambatan", f"{int(detail['Terlambat'])} kali"),
    ]
    st.markdown(f"<div class='opd-detail-card'><strong style='color:#173b63'>{escape(selected_opd.title())}</strong><div style='display:flex;flex-wrap:wrap;gap:10px 24px;margin-top:10px;font-size:12px;color:#64748b'>" + "".join(f"<span>{label}: <strong style='color:#0f172a'>{value}</strong></span>" for label, value in detail_kpis) + "</div></div>", unsafe_allow_html=True)

    detail_daily = indicators[indicators["Unit Kerja"].astype(str).eq(selected_opd)].copy()
    composition_values = {
        "Hadir": int(detail_daily["Hadir Fisik"].sum()), "Cuti": int(detail_daily["Cuti"].sum()),
        "WFH": int(detail_daily["WFH"].sum()), "DL": int(detail_daily["DL"].sum()), "TK": int(detail_daily["TK"].sum()),
    }
    composition = pd.DataFrame({"Status": list(composition_values), "Jumlah": list(composition_values.values())})
    composition_chart = alt.Chart(composition).mark_bar(cornerRadiusEnd=4, color="#2563eb").encode(
        y=alt.Y("Status:N", sort=list(composition_values), title=None), x=alt.X("Jumlah:Q", title="Jumlah hari"),
        tooltip=["Status:N", alt.Tooltip("Jumlah:Q", format=",", title="Hari")],
    ).properties(height=190)
    st.markdown("**Komposisi Presensi**")
    st.altair_chart(composition_chart, use_container_width=True)

    employees = detail_daily.groupby(["NIP", "Nama"], as_index=False, dropna=False).agg(TK=("TK", "sum"), Terlambat=("Terlambat", "sum"))
    employee_left, employee_right = st.columns(2)
    with employee_left:
        st.markdown("**Pegawai dengan TK Tertinggi**")
        tk_employees = employees[employees["TK"] > 0].sort_values(["TK", "Nama"], ascending=[False, True]).head(5)
        st.dataframe(tk_employees.rename(columns={"Nama": "Nama Pegawai"}), hide_index=True, use_container_width=True)
    with employee_right:
        st.markdown("**Pegawai Terlambat Terbanyak**")
        late_employees = employees[employees["Terlambat"] > 0].sort_values(["Terlambat", "Nama"], ascending=[False, True]).head(5)
        st.dataframe(late_employees.rename(columns={"Nama": "Nama Pegawai"}), hide_index=True, use_container_width=True)


def show_employee_detail_page_legacy() -> None:
    inject_dashboard_css()
    data = load_employee_data()
    month_order = ["Januari", "Februari", "Maret"]
    employees = data.drop_duplicates("NIP").sort_values("Nama Pegawai")
    employee_options = {
        f"{row['Nama Pegawai']} — {row['NIP']}": row["NIP"]
        for _, row in employees.iterrows()
    }

    with st.sidebar:
        st.markdown("# 🛡️ EWS Kehadiran")
        st.caption("Detail presensi pegawai")
        st.divider()
        selected_employee_label = st.selectbox("Pilih pegawai", employee_options.keys(), key="detail_employee")
        selected_nip = employee_options[selected_employee_label]
        employee_history = data[data["NIP"] == selected_nip].copy()
        available_months = [month for month in month_order if month in employee_history["Bulan"].values]
        selected_month = st.selectbox("Periode detail", available_months, key="detail_month")
        st.divider()
        st.caption(f"Login sebagai: {st.session_state.username}")
        st.button("Keluar", on_click=logout, use_container_width=True, key="detail_logout")

    employee_history["Bulan"] = pd.Categorical(employee_history["Bulan"], categories=month_order, ordered=True)
    employee_history = employee_history.sort_values("Bulan")
    selected_record = employee_history[employee_history["Bulan"] == selected_month].iloc[0]
    employee_history["Hadir"] = (
        employee_history["Hari Kerja"]
        - employee_history["TK"]
        - employee_history["Cuti"]
        - employee_history["WFH"]
        - employee_history["DL"]
    ).clip(lower=0)
    employee_history["Risk Score"] = employee_history.apply(
        lambda row: calculate_risk_score(row["TK"], row["Terlambat"]), axis=1
    )
    employee_history["Status Risiko"] = employee_history["Risk Score"].apply(risk_status)
    employee_history["Indikator Warning"] = employee_history.apply(
        lambda row: warning_indicators(row["TK"], row["Terlambat"]), axis=1
    )
    employee_history["Rekomendasi"] = employee_history["Status Risiko"].apply(risk_recommendation)

    score = calculate_risk_score(selected_record["TK"], selected_record["Terlambat"])
    status = risk_status(score)
    recommendation = risk_recommendation(status)
    attendance_calendar = build_attendance_calendar(selected_record)

    st.markdown("<p class='dashboard-title'>Detail Pegawai</p>", unsafe_allow_html=True)
    st.markdown(
        f"<p class='dashboard-subtitle'>Profil dan riwayat presensi • Periode: {selected_month}</p>",
        unsafe_allow_html=True,
    )

    st.markdown("<div class='section-title'>Identitas Pegawai</div>", unsafe_allow_html=True)
    identity_1, identity_2, identity_3, identity_4 = st.columns(4)
    identity_1.metric("NIP", selected_record["NIP"])
    identity_2.metric("Nama Pegawai", selected_record["Nama Pegawai"])
    identity_3.metric("Unit Kerja", selected_record["Unit Kerja"])
    identity_4.metric("Hari Kerja", f"{int(selected_record['Hari Kerja'])} hari")

    risk_col, recommendation_col = st.columns([1, 2])
    with risk_col:
        st.markdown("<div class='section-title'>Risk Score</div>", unsafe_allow_html=True)
        st.metric("Status Risiko", f"{score}/100", status)
        st.progress(score / 100)
    with recommendation_col:
        st.markdown("<div class='section-title'>Rekomendasi Tindak Lanjut</div>", unsafe_allow_html=True)
        st.info(f"**Status {status}:** {recommendation}")

    st.markdown("<div class='section-title'>Kalender Presensi</div>", unsafe_allow_html=True)
    st.caption("Status per hari kerja pada periode terpilih. Legenda: Hadir, TK, WFH, DL, dan Cuti.")
    st.dataframe(attendance_calendar, hide_index=True, use_container_width=True)

    history_col, late_history_col = st.columns(2)
    with history_col:
        st.markdown("<div class='section-title'>Riwayat Kehadiran</div>", unsafe_allow_html=True)
        st.dataframe(
            employee_history[["Bulan", "Hadir", "TK", "WFH", "DL", "Cuti"]],
            hide_index=True,
            use_container_width=True,
            column_config={
                "Hadir": st.column_config.NumberColumn("Hadir", format="%d hari"),
                "TK": st.column_config.NumberColumn("TK", format="%d hari"),
                "WFH": st.column_config.NumberColumn("WFH", format="%d hari"),
                "DL": st.column_config.NumberColumn("DL", format="%d hari"),
                "Cuti": st.column_config.NumberColumn("Cuti", format="%d hari"),
            },
        )
    with late_history_col:
        st.markdown("<div class='section-title'>Riwayat Keterlambatan</div>", unsafe_allow_html=True)
        st.dataframe(
            employee_history[["Bulan", "Terlambat", "Jam Datang", "Hari Dominan"]],
            hide_index=True,
            use_container_width=True,
            column_config={
                "Terlambat": st.column_config.NumberColumn("Keterlambatan", format="%d kali"),
                "Jam Datang": st.column_config.NumberColumn("Rata-rata Jam Datang", format="%.2f"),
            },
        )

    st.markdown("<div class='section-title'>Riwayat Warning</div>", unsafe_allow_html=True)
    st.dataframe(
        employee_history[["Bulan", "Risk Score", "Status Risiko", "Indikator Warning", "Rekomendasi"]],
        hide_index=True,
        use_container_width=True,
        column_config={
            "Risk Score": st.column_config.NumberColumn("Risk Score", format="%d/100"),
            "Status Risiko": st.column_config.TextColumn("Status", width="medium"),
            "Indikator Warning": st.column_config.TextColumn("Penyebab Warning", width="large"),
            "Rekomendasi": st.column_config.TextColumn("Rekomendasi", width="large"),
        },
    )


def show_employee_detail_page() -> None:
    inject_dashboard_css()
    data = load_employee_data()
    months = ["Januari", "Februari", "Maret"]
    employees = data.drop_duplicates("NIP").sort_values("Nama Pegawai")
    options = {f"{row['Nama Pegawai']} — {row['NIP']}": row["NIP"] for _, row in employees.iterrows()}

    with st.sidebar:
        st.markdown("# 🛡️ EWS Kehadiran")
        st.caption("Profil dan riwayat individu")
        st.divider()
        selected_label = st.selectbox("Pilih pegawai", options.keys(), key="employee_page_select")
        selected_nip = options[selected_label]
        history = data[data["NIP"] == selected_nip].copy()
        selected_month = st.selectbox("Periode", months, key="employee_page_month")
        st.divider()
        st.caption(f"Login sebagai: {st.session_state.username}")
        st.button("Keluar", on_click=logout, use_container_width=True, key="employee_page_logout")

    history["Bulan"] = pd.Categorical(history["Bulan"], categories=months, ordered=True)
    history = history.sort_values("Bulan")
    selected_rows = history[history["Bulan"] == selected_month]
    has_period_data = not selected_rows.empty
    if has_period_data:
        record = selected_rows.iloc[0]
    else:
        record = history.iloc[0].copy()
        record["Bulan"] = selected_month
        for field in ["TK", "Cuti", "Terlambat", "Hari Kerja", "WFH", "DL"]:
            record[field] = 0
    history["Hadir"] = (history["Hari Kerja"] - history["TK"] - history["WFH"] - history["DL"] - history["Cuti"]).clip(lower=0)
    history["Kehadiran"] = (history["Hadir"] / history["Hari Kerja"] * 100).round(1)
    history["Risk Score"] = history.apply(lambda row: calculate_risk_score(row["TK"], row["Terlambat"]), axis=1)
    history["Status"] = history["Risk Score"].apply(risk_status)
    history["Status Ikon"] = history["Status"].map({"Normal": "🟢 NORMAL", "Waspada": "🟡 WASPADA", "Tinggi": "🟠 TINGGI", "Kritis": "🔴 KRITIS"})
    score = calculate_risk_score(record["TK"], record["Terlambat"])
    status = risk_status(score)
    attendance = (max(record["Hari Kerja"] - record["TK"] - record["Cuti"], 0) / record["Hari Kerja"] * 100) if record["Hari Kerja"] else 0
    calendar = build_attendance_calendar(record)
    pattern_days = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat"]
    pattern_summary = history.groupby("Hari Dominan")["TK"].sum().reindex(pattern_days, fill_value=0)
    peak_pattern_day = pattern_summary.idxmax() if pattern_summary.sum() else "Belum tersedia"
    peak_pattern_count = int(pattern_summary.max()) if pattern_summary.sum() else 0

    st.markdown("<h1 class='dashboard-title'>Detail Pegawai</h1>", unsafe_allow_html=True)
    st.markdown(f"<div class='page-context-grid'><div class='page-context-card risk-context-card'>Profil individu dan pola presensi</div><div class='page-context-card'>Periode: {selected_month}</div></div>", unsafe_allow_html=True)
    if not has_period_data:
        st.info(f"Belum ada data presensi {record['Nama Pegawai']} untuk periode {selected_month}.")
    st.markdown("<div class='section-title'>Identitas Pegawai</div>", unsafe_allow_html=True)
    identity_table = pd.DataFrame(
        {
            "Informasi": ["Nama", "NIP", "Jabatan", "Pangkat/Golongan", "OPD"],
            "Data": [
                record["Nama Pegawai"],
                record["NIP"],
                record["Jabatan"],
                record["Pangkat/Golongan"],
                record["Unit Kerja"],
            ],
        }
    )
    st.dataframe(identity_table, hide_index=True, use_container_width=True)

    st.markdown("<div class='section-title'>KPI Individu</div>", unsafe_allow_html=True)
    kpi_values = {
        "Kehadiran": f"{attendance:.1f}%",
        "TK": f"{int(record['TK'])} hari",
        "Terlambat": f"{int(record['Terlambat'])} kali",
        "Risk Score": f"{score}/100",
        "Status": {"Normal": "🟢 NORMAL", "Waspada": "🟡 WASPADA", "Tinggi": "🟠 TINGGI", "Kritis": "🔴 KRITIS"}[status],
    }
    kpi_cards = "".join(
        f"<div class='individual-kpi-card'><div class='label'>{label}</div><div class='value'>{value}</div></div>"
        for label, value in kpi_values.items()
    )
    st.markdown(f"<div class='individual-kpi-grid'>{kpi_cards}</div>", unsafe_allow_html=True)

    st.markdown("<div class='section-title'>Calendar Heatmap</div>", unsafe_allow_html=True)
    st.caption("Status presensi individu berdasarkan urutan hari kerja pada periode terpilih.")
    st.dataframe(calendar, hide_index=True, use_container_width=True)

    st.markdown("<div class='section-title'>Riwayat Presensi</div>", unsafe_allow_html=True)
    st.dataframe(
        history[["Bulan", "Hadir", "TK", "Terlambat", "WFH", "DL", "Cuti"]],
        hide_index=True,
        use_container_width=True,
        column_config={
            "Kehadiran": st.column_config.NumberColumn("Kehadiran", format="%.1f%%"),
            "Hadir": st.column_config.NumberColumn("Hadir", format="%d hari"),
            "TK": st.column_config.NumberColumn("TK", format="%d hari"),
            "Terlambat": st.column_config.NumberColumn("Terlambat", format="%d kali"),
            "WFH": st.column_config.NumberColumn("WFH", format="%d hari"),
            "DL": st.column_config.NumberColumn("DL", format="%d hari"),
            "Cuti": st.column_config.NumberColumn("Cuti", format="%d hari"),
        },
    )

    st.markdown("<div class='section-title'>Pattern</div>", unsafe_allow_html=True)
    st.dataframe(
        pattern_summary.rename("Kejadian TK").reset_index().rename(columns={"Hari Dominan": "Hari"}),
        hide_index=True,
        use_container_width=True,
    )
    st.warning(f"⚠️ Pola TK tertinggi terjadi pada **{peak_pattern_day}** ({peak_pattern_count} kejadian) dalam riwayat yang tersedia.")
    st.markdown("<div class='section-title'>Riwayat Warning</div>", unsafe_allow_html=True)
    warning_history = history[history["Status"] != "Normal"][["Bulan", "Risk Score", "Status Ikon"]]
    if warning_history.empty:
        st.success("Pegawai belum masuk kategori Waspada, Tinggi, atau Kritis.")
    else:
        st.dataframe(warning_history, hide_index=True, use_container_width=True)


def _employee_attendance_rate(source: pd.DataFrame) -> float:
    """Memakai formula kehadiran yang sama dengan halaman analisis lain."""
    workdays = float(source["Hari Kerja"].sum())
    if not workdays:
        return 0.0
    return max((workdays - float(source["TK"].sum()) - float(source["Cuti"].sum())) / workdays * 100, 0.0)


def prepare_employee_daily_attendance(source: pd.DataFrame, selected_nip: str) -> pd.DataFrame:
    """Siapkan satu baris per NIP + tanggal memakai field hasil parser existing."""
    required = {"NIP", "Tanggal", "Hari", "Status"}
    if source.empty or not required.issubset(source.columns):
        return pd.DataFrame()
    daily = source[source["NIP"].astype(str) == str(selected_nip)].copy()
    daily["_tanggal"] = pd.to_datetime(daily["Tanggal"], errors="coerce")
    daily = daily[daily["_tanggal"].notna()].copy()
    if daily.empty:
        return pd.DataFrame()

    # Loader existing sudah membentuk satu sel presensi per tanggal. Jika file
    # periode yang sama terduplikasi, pertahankan record hasil parser pertama.
    daily = daily.sort_values("_tanggal", kind="stable").drop_duplicates(
        ["NIP", "_tanggal"], keep="first"
    )
    source_status = daily["Status"].fillna("").astype(str).str.upper()
    source_codes = (
        daily.get("Sumber_Datang", pd.Series("", index=daily.index)).fillna("").astype(str)
        + "/"
        + daily.get("Sumber_Pulang", pd.Series("", index=daily.index)).fillna("").astype(str)
    ).str.upper()
    status = pd.Series("Hadir", index=daily.index, dtype="object")
    status.loc[source_status.eq("LIBUR")] = "Libur"
    for valid_code in ["SL", "SK", "MR", "PBT"]:
        status.loc[source_codes.str.contains(fr"\b{valid_code}\b", regex=True)] = valid_code
    status.loc[source_codes.str.contains(r"\bWFH\b|\bWFA\b", regex=True)] = "WFH"
    status.loc[source_codes.str.contains(r"\bDL\b", regex=True)] = "DL"
    status.loc[source_codes.str.contains("CUTI", regex=False)] = "Cuti"
    status.loc[daily.get("TK", pd.Series(False, index=daily.index)).fillna(False).astype(bool)] = "TK"
    late_mask = daily.get("Terlambat", pd.Series(False, index=daily.index)).fillna(False).astype(bool)
    status.loc[late_mask & status.eq("Hadir")] = "Terlambat"

    month_names = {1: "Januari", 2: "Februari", 3: "Maret", 4: "April", 5: "Mei", 6: "Juni", 7: "Juli", 8: "Agustus", 9: "September", 10: "Oktober", 11: "November", 12: "Desember"}
    result = pd.DataFrame(index=daily.index)
    result["_tanggal"] = daily["_tanggal"]
    result["Periode"] = daily["_tanggal"].map(lambda value: f"{month_names[value.month]} {value.year}")
    result["Tanggal"] = daily["_tanggal"].map(lambda value: f"{value.day:02d} {month_names[value.month]} {value.year}")
    result["Hari"] = daily["Hari"].fillna("-").replace("", "-")
    day_labels = {"HARI_KERJA": "Hari Kerja", "AKHIR_PEKAN": "Akhir Pekan", "LIBUR_NASIONAL": "Libur Nasional", "CUTI_BERSAMA": "Cuti Bersama"}
    result["Jenis Hari"] = daily.get("jenis_hari", pd.Series("HARI_KERJA", index=daily.index)).map(day_labels).fillna("Hari Kerja")
    result["Hari Kerja Kalender"] = daily.get("is_hari_kerja", pd.Series(True, index=daily.index)).fillna(False).astype(bool)
    result["Wajib Presensi"] = daily.get("wajib_presensi", pd.Series(True, index=daily.index)).fillna(False).astype(bool)
    result["Jam Masuk"] = daily.get("Jam_Masuk", pd.Series("-", index=daily.index)).fillna("-").replace("", "-")
    result["Jam Pulang"] = daily.get("Jam_Pulang", pd.Series("-", index=daily.index)).fillna("-").replace("", "-")
    result["Status"] = status
    late_minutes = pd.to_numeric(daily.get("Menit_Terlambat", pd.Series(0, index=daily.index)), errors="coerce").fillna(0).astype(int)
    result["Terlambat"] = late_minutes.map(lambda value: f"{value // 60} jam {value % 60} menit" if value >= 60 and value % 60 else f"{value // 60} jam" if value >= 60 else f"{value} menit" if value else "-")
    return result.sort_values("_tanggal", ascending=False).reset_index(drop=True)


def show_employee_detail_page() -> None:
    """Profil presensi individu; tidak memuat workflow verifikasi EWS."""
    inject_dashboard_css()
    data = load_employee_data()
    months = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
    months = [month for month in months if month in data.get("Bulan", pd.Series(dtype=str)).values]

    st.markdown("<h1 style='display:block!important;visibility:visible!important;opacity:1!important;font-size:32px!important;font-weight:800!important;color:#102a43!important;line-height:1.2!important;text-align:left!important;margin:0 0 .3rem!important'>Detail Pegawai</h1>", unsafe_allow_html=True)
    st.markdown("<div style='color:#64748b;margin:.3rem 0 1rem'>Profil individu, ringkasan, dan riwayat presensi pegawai.</div>", unsafe_allow_html=True)

    if data.empty or not {"NIP", "Nama Pegawai", "Unit Kerja"}.issubset(data.columns):
        st.info("Tidak terdapat data pegawai pada filter yang dipilih.")
        return

    filter_opd, filter_employee, filter_period = st.columns([1, 2, 1])
    units = sorted(data["Unit Kerja"].dropna().astype(str).unique().tolist())
    with filter_opd:
        selected_unit = st.selectbox(
            "OPD", units, index=None, placeholder="Pilih OPD",
            key="employee_detail_opd",
        )
    if "_employee_detail_previous_opd" not in st.session_state:
        st.session_state["_employee_detail_previous_opd"] = selected_unit
    elif st.session_state["_employee_detail_previous_opd"] != selected_unit:
        st.session_state["_employee_detail_previous_opd"] = selected_unit
        st.session_state.pop("employee_detail_employee", None)
        st.session_state.pop("employee_detail_period", None)
        st.session_state.pop("_employee_detail_previous_employee", None)
        st.rerun()

    employee_source = data[data["Unit Kerja"] == selected_unit] if selected_unit is not None else data.iloc[0:0]
    employees = employee_source.drop_duplicates("NIP").sort_values("Nama Pegawai")
    options = {f"{row['Nama Pegawai']} — {row['NIP']}": row["NIP"] for _, row in employees.iterrows()}
    with filter_employee:
        selected_label = st.selectbox(
            "Pegawai", list(options), index=None,
            placeholder="Pilih Pegawai" if selected_unit is not None else "Pilih OPD terlebih dahulu",
            disabled=selected_unit is None,
            key="employee_detail_employee",
        )
    if "_employee_detail_previous_employee" not in st.session_state:
        st.session_state["_employee_detail_previous_employee"] = selected_label
    elif st.session_state["_employee_detail_previous_employee"] != selected_label:
        st.session_state["_employee_detail_previous_employee"] = selected_label
        st.session_state.pop("employee_detail_period", None)
        st.rerun()

    selected_nip = options.get(selected_label)
    history = data[data["NIP"] == selected_nip].copy() if selected_nip is not None else data.iloc[0:0].copy()
    month_rank = {month: index for index, month in enumerate(ANALYTICS_MONTHS, start=1)}
    period_options: list[str] = []
    period_scope: dict[str, tuple[int, str]] = {}
    if selected_nip is not None:
        history["_tahun"] = pd.to_numeric(history["Tahun"], errors="coerce").astype("Int64")
        period_pairs = (
            history.loc[history["_tahun"].notna(), ["_tahun", "Bulan"]]
            .drop_duplicates()
            .assign(_bulan=lambda frame: frame["Bulan"].map(month_rank))
            .sort_values(["_tahun", "_bulan"])
        )
        period_options = [f"{row['Bulan']} {int(row['_tahun'])}" for _, row in period_pairs.iterrows()]
        period_scope = {
            f"{row['Bulan']} {int(row['_tahun'])}": (int(row['_tahun']), str(row["Bulan"]))
            for _, row in period_pairs.iterrows()
        }
    with filter_period:
        selected_period = st.selectbox(
            "Periode", period_options, index=None, placeholder="Pilih Periode",
            disabled=selected_nip is None,
            key="employee_detail_period",
        )

    if selected_unit is None or selected_nip is None or selected_period is None:
        st.info("Pilih OPD, pegawai, dan periode untuk menampilkan detail presensi.")
        return

    selected_year, selected_month = period_scope[selected_period]
    history["Bulan"] = pd.Categorical(history["Bulan"], categories=months, ordered=True)
    history = history.sort_values(["_tahun", "Bulan"])
    visible = history[history["_tahun"].eq(selected_year) & history["Bulan"].eq(selected_month)].copy()
    if visible.empty:
        name = str(history.iloc[0]["Nama Pegawai"])
        st.info(f"Tidak terdapat data presensi {name} pada periode yang dipilih.")
        return

    record = visible.iloc[-1]
    job = safe_display(record.get("Jabatan"), "")
    unit = safe_display(record.get("Unit Kerja"), "")
    existing_employee_type = str(record.get("Jenis Pegawai", "")).strip().upper()
    existing_employee_type = existing_employee_type if existing_employee_type in {"PNS", "PPPK", "CPNS"} else None
    if existing_employee_type:
        employee_type = existing_employee_type
        employee_type_source = str(record.get("Jenis Pegawai Source", "NIP_DETECTION"))
    else:
        employee_type, employee_type_source = resolve_employee_type(selected_nip)
    normalized_selected_nip = str(selected_nip).strip()
    masked_selected_nip = (
        f"{normalized_selected_nip[:3]}{'*' * max(len(normalized_selected_nip) - 6, 0)}{normalized_selected_nip[-3:]}"
        if len(normalized_selected_nip) > 6 else "***"
    )
    LOGGER.debug(
        "Detail Pegawai NIP=%s jenis=%s source=%s",
        masked_selected_nip, employee_type, employee_type_source,
    )
    employee_type_label = employee_type if employee_type in {"PNS", "PPPK", "CPNS"} else "Belum Diketahui"
    employee_type_class = {"PNS": "pns", "PPPK": "pppk"}.get(employee_type, "unknown")
    st.markdown("<div class='section-title'>Profil Pegawai</div>", unsafe_allow_html=True)
    details = [f"<div style='font-size:22px;font-weight:750;color:#102a43'>{escape(safe_display(record.get('Nama Pegawai'), '-'))}</div>", f"<div style='color:#64748b;margin-top:.25rem'>NIP: {escape(safe_display(record.get('NIP'), '-'))}</div>"]
    if job and job != "-":
        details.append(f"<div style='margin-top:.65rem'><strong>Jabatan</strong><br>{job}</div>")
    if unit and unit != "-":
        details.append(f"<div style='margin-top:.45rem'><strong>Unit Kerja</strong><br>{unit}</div>")
    details.append(f"<div class='employee-type-badge {employee_type_class}'>{employee_type_label}</div>")
    inactive_status = get_employee_inactive_status(selected_nip)
    if inactive_status:
        effective_text = inactive_status["effective_date"].strftime("%d-%m-%Y")
        details.append(
            "<div style='margin-top:12px;padding-top:10px;border-top:1px solid #e2e8f0;"
            "font-size:12px;color:#475569'><strong>Status Presensi:</strong> Nonaktif"
            f"<br><strong>Efektif:</strong> {effective_text}</div>"
        )
    st.markdown(f"""
        <style>
        .employee-profile-card{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:18px}}
        .employee-type-badge{{display:inline-block;margin-top:12px;padding:4px 10px;border-radius:999px;font-size:12px;font-weight:700}}
        .employee-type-badge.pns{{background:#eff6ff;color:#1d4ed8}}
        .employee-type-badge.pppk{{background:#ecfeff;color:#0e7490}}
        .employee-type-badge.unknown{{background:#f1f5f9;color:#64748b}}
        </style>
        <div class='employee-profile-card'>{''.join(details)}</div>
    """, unsafe_allow_html=True)

    raw_daily = _load_excel_daily_data(_excel_source_signature())
    selected_daily = raw_daily[
        raw_daily["NIP"].astype(str).eq(str(selected_nip))
        & pd.to_numeric(raw_daily["Tahun"], errors="coerce").eq(selected_year)
        & raw_daily["Nama_Bulan"].eq(selected_month)
    ]
    selected_indicators = summarize_attendance_indicators(selected_daily)
    compliance_rate = float(selected_indicators["compliance_percentage"])
    physical_rate = float(selected_indicators["physical_attendance_percentage"])
    tk = int(visible["TK"].sum())
    late = int(visible["Terlambat"].sum())
    leave = int(visible["Cuti"].sum())
    wfh = int(visible.get("WFH", pd.Series(0, index=visible.index)).sum())
    dl = int(visible.get("DL", pd.Series(0, index=visible.index)).sum())
    ytd_history = history[
        history["_tahun"].eq(selected_year)
        & history["Bulan"].map(month_rank).le(month_rank[selected_month])
    ]
    annual_late = int(pd.to_numeric(ytd_history["Terlambat"], errors="coerce").fillna(0).sum())
    employee_daily_ytd = raw_daily[
        raw_daily["NIP"].astype(str).eq(str(selected_nip))
    ]
    annual_tk = calculate_ytd_tk(employee_daily_ytd, selected_year, selected_month)
    consecutive_tk = calculate_consecutive_unexcused_days(
        employee_daily_ytd, selected_year, selected_month,
    )
    monitoring = evaluate_attendance_monitoring_rule(
        annual_tk, consecutive_tk, consecutive_tk is not None,
        nip=str(selected_nip), employee_type=employee_type,
        period_end=selected_period, data_granularity="DAILY",
    )
    st.markdown("<div class='section-title'>Ringkasan Presensi</div>", unsafe_allow_html=True)
    for column, label, value in zip(st.columns(5), ["Kepatuhan Presensi", "Kehadiran Fisik", "TK", "Keterlambatan", "Cuti"], [f"{compliance_rate:.1f}%", f"{physical_rate:.1f}%", f"{tk} hari", f"{late} kali", f"{leave} hari"]):
        column.metric(label, value)
    if "WFH" in visible or "DL" in visible:
        st.caption(f"WFH: {wfh} hari • DL: {dl} hari")

    monthly_tk = visible.groupby("Bulan", observed=True)["TK"].sum().reindex([selected_month], fill_value=0)
    monthly_tk = monthly_tk[monthly_tk.gt(0)]
    ews_status = warning_status(tk)
    st.markdown("<div class='section-title'>Riwayat Ketidakhadiran Tanpa Keterangan</div>", unsafe_allow_html=True)
    st.write(f"**Total TK:** {tk} hari")
    if monthly_tk.empty:
        st.info("Tidak terdapat TK pada periode analisis.")
    else:
        st.dataframe(monthly_tk.rename("Jumlah TK").reset_index(), hide_index=True, use_container_width=True)

    st.markdown("<div class='section-title'>Dasar Early Warning</div>", unsafe_allow_html=True)
    periods = selected_period if not monthly_tk.empty else "Tidak ada"
    st.write(f"**Status:** {ews_status}")
    st.write(f"**Indikator:** Akumulasi ketidakhadiran tanpa keterangan pada data presensi: {tk} hari.")
    st.write(f"**Periode terjadinya:** {periods}")
    st.caption("Status early warning merupakan indikator awal dan tidak menyatakan pegawai telah terbukti melakukan pelanggaran disiplin.")

    st.markdown("<div class='section-title'>Monitoring Rule-Based Presensi</div>", unsafe_allow_html=True)
    consecutive_label = f"{consecutive_tk} hari kerja" if consecutive_tk is not None else "Data belum mendukung pemeriksaan ketidakhadiran 10 hari kerja terus-menerus"
    article_html = f"<br>{escape(str(monitoring['reference_article']))}" if monitoring["reference_article"] else ""
    st.markdown(
        f"<div style='background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:18px 20px'>"
        f"<strong>TK Tahun Berjalan</strong><br>{annual_tk} hari (Januari–{escape(selected_month)} {selected_year})<br><br>"
        f"<strong>Keterlambatan</strong><br>{annual_late} kali <span style='color:#64748b'>(indikator tambahan)</span><br><br>"
        f"<strong>Jenis Pegawai</strong><br>{escape(employee_type_label)}<br><br>"
        f"<strong>Ambang Referensi</strong><br>{escape(str(monitoring['reference_band']))}<br><br>"
        f"<strong>Status Monitoring</strong><br>{escape(str(monitoring['reference_status']))}<br><br>"
        f"<strong>Catatan Monitoring</strong><br>{escape(str(monitoring['monitoring_note']))}<br><br>"
        f"<strong>Dasar Monitoring</strong><br>{escape(str(monitoring['regulation']))}"
        f"{article_html}<br><br>"
        f"<strong>TK terus-menerus</strong><br>{escape(consecutive_label)}</div>",
        unsafe_allow_html=True,
    )
    with st.expander("Lihat Dasar Penilaian Rule-Based Presensi"):
        st.write(f"Jenis Pegawai: {employee_type_label}")
        st.write(f"Akumulasi TK tahun berjalan: {annual_tk} hari")
        st.write(f"TK terus-menerus: {consecutive_label}")
        st.write(f"Ambang Referensi: {monitoring['reference_band']}")
        st.write(f"Status Monitoring: {monitoring['reference_status']}")
        st.write(f"Referensi: {monitoring['reference_article'] or 'Monitoring awal'}")
        st.write(f"Dasar Monitoring: {monitoring['regulation']}")
        st.caption(MONITORING_DISCLAIMER)
    if employee_type == "PPPK":
        st.caption("Jenis pegawai terdeteksi sebagai PPPK; engine menerapkan konfigurasi Pergub khusus PPPK dan tidak menggunakan ketentuan PNS.")
    elif employee_type not in {"PNS", "CPNS"}:
        st.warning("Jenis pegawai perlu diverifikasi sebelum ketentuan spesifik dapat ditampilkan.")

    if ews_status != "Normal":
        st.markdown("<div class='section-title'>Status EWS</div>", unsafe_allow_html=True)
        st.markdown(f"<div style='background:#fff;border:1px solid #fde68a;border-radius:12px;padding:16px'><strong>{escape(ews_status)}</strong><br><span style='color:#64748b'>TK tercatat: {tk} hari</span><br><span style='color:#64748b'>Indikator tambahan: terlambat {late} kali</span></div>", unsafe_allow_html=True)
        if st.button("Lihat Detail Warning", key="employee_detail_warning"):
            st.session_state["navigate_to_page"] = "Early Warning System"
            st.rerun()

    trend = visible.copy()
    trend["Kehadiran Fisik"] = physical_rate
    st.markdown("<div class='section-title'>Tren Kehadiran Fisik</div>", unsafe_allow_html=True)
    if len(trend) == 1:
        st.caption(f"Kehadiran Fisik {trend.iloc[0]['Bulan']}: {trend.iloc[0]['Kehadiran Fisik']:.1f}%")
    else:
        chart = alt.Chart(trend).mark_line(point=True, color="#2563eb", strokeWidth=3).encode(
            x=alt.X("Bulan:N", sort=months, title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("Kehadiran Fisik:Q", title="Kehadiran Fisik (%)", scale=alt.Scale(domain=[0, 100])),
            tooltip=[alt.Tooltip("Bulan:N", title="Periode"), alt.Tooltip("Kehadiran Fisik:Q", format=".1f", title="Kehadiran Fisik (%)")],
        ).properties(height=240)
        st.altair_chart(chart, use_container_width=True)
    if len(trend) > 1:
        late_chart = alt.Chart(trend).mark_bar(color="#f97316").encode(
            x=alt.X("Bulan:N", sort=months, title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("Terlambat:Q", title="Keterlambatan (kali)"),
            tooltip=["Bulan:N", "Terlambat:Q"],
        ).properties(height=190)
        st.markdown("<div class='section-title'>Tren Keterlambatan</div>", unsafe_allow_html=True)
        st.altair_chart(late_chart, use_container_width=True)

    st.markdown("<div class='section-title'>📅 Presensi Harian</div>", unsafe_allow_html=True)
    st.caption(f"Riwayat presensi pegawai pada {selected_period}.")
    employee_daily = prepare_employee_daily_attendance(raw_daily, selected_nip)
    if employee_daily.empty:
        st.info("Data presensi harian belum tersedia pada sumber data periode ini.")
    else:
        # Key periode harian lama tidak lagi menjadi sumber filter.
        st.session_state.pop(f"employee_daily_period_{selected_nip}", None)
        period_daily = employee_daily[employee_daily["Periode"] == selected_period].copy()
        supported_statuses = [
            item for item in ["Hadir", "Terlambat", "TK", "Cuti", "DL", "WFH", "SL", "SK", "MR", "PBT"]
            if item in set(period_daily["Status"])
        ]
        daily_status = st.selectbox(
            "Status", ["Semua"] + supported_statuses,
            key=f"employee_daily_status_{selected_nip}",
        )

        workday_count = int(period_daily["Hari Kerja Kalender"].sum())
        present_count = int(period_daily["Status"].isin(["Hadir", "Terlambat"]).sum())
        late_count = int(period_daily["Status"].eq("Terlambat").sum())
        tk_count = int(period_daily["Status"].eq("TK").sum())
        daily_kpis = "".join(
            f"<div class='employee-daily-kpi'><div class='employee-daily-kpi-value'>{value}</div><div class='employee-daily-kpi-label'>{label}</div></div>"
            for label, value in [("Hari Kerja", workday_count), ("Hadir", present_count), ("Terlambat", late_count), ("TK", tk_count)]
        )
        secondary = " • ".join(
            f"{label} {int(period_daily['Status'].eq(label).sum())} hari"
            for label in ["Cuti", "DL", "WFH"] if label in set(period_daily["Status"])
        )
        filtered_daily = period_daily if daily_status == "Semua" else period_daily[period_daily["Status"] == daily_status]
        status_classes = {"Hadir": "hadir", "Terlambat": "terlambat", "TK": "tk", "Cuti": "cuti", "DL": "dl", "WFH": "wfh", "SL": "sah", "SK": "sah", "MR": "sah", "PBT": "sah", "Libur": "libur"}
        rows_html = "".join(
            "<tr>"
            f"<td>{escape(str(row['Tanggal']))}</td><td>{escape(str(row['Hari']))}</td>"
            f"<td>{escape(str(row['Jenis Hari']))}</td><td>{escape(str(row['Jam Masuk']))}</td><td>{escape(str(row['Jam Pulang']))}</td>"
            f"<td><span class='employee-daily-status {status_classes.get(str(row['Status']), 'libur')}'>{escape(str(row['Status']))}</span></td>"
            f"<td>{escape(str(row['Terlambat']))}</td></tr>"
            for _, row in filtered_daily.iterrows()
        )
        table_html = (
            f"<div class='employee-daily-table-wrap'><table class='employee-daily-table'><thead><tr><th>Tanggal</th><th>Hari</th><th>Jenis Hari</th><th>Jam Masuk</th><th>Jam Pulang</th><th>Status</th><th>Terlambat</th></tr></thead><tbody>{rows_html}</tbody></table></div>"
            if not filtered_daily.empty
            else "<div class='employee-daily-empty'>Tidak terdapat data presensi harian pegawai pada periode ini.</div>"
        )
        secondary_html = f"<div class='employee-daily-secondary'>{secondary}</div>" if secondary else "<div style='height:10px'></div>"
        dominant_day_html = ""
        if late_count:
            dominant_day = escape(str(period_daily.loc[period_daily["Status"].eq("Terlambat"), "Hari"].mode().iloc[0]))
            dominant_day_html = f"📅 Hari keterlambatan dominan: {dominant_day}.<br>"
        st.markdown(f"""
            <style>
            .employee-daily-attendance{{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:18px;margin-top:10px}}
            .employee-daily-kpis{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}}
            .employee-daily-kpi{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:10px 12px}}
            .employee-daily-kpi-value{{font-size:18px;font-weight:700;color:#102a43}} .employee-daily-kpi-label{{font-size:12px;color:#64748b;margin-top:2px}}
            .employee-daily-secondary{{font-size:12px;color:#64748b;margin:10px 0 14px}}
            .employee-daily-table-wrap{{overflow-x:auto;border:1px solid #e2e8f0;border-radius:10px}}
            .employee-daily-table{{width:100%;border-collapse:collapse;min-width:660px;font-size:12px}}
            .employee-daily-table th{{background:#f8fafc;font-weight:600;color:#475569;text-align:left;padding:10px;border-bottom:1px solid #e2e8f0}}
            .employee-daily-table td{{background:#fff;color:#334155;padding:10px;border-bottom:1px solid #f1f5f9;white-space:nowrap}}
            .employee-daily-table tbody tr:hover td{{background:#f8fafc}}
            .employee-daily-status{{display:inline-block;border-radius:999px;padding:3px 8px;font-weight:600}}
            .employee-daily-status.hadir{{background:#dcfce7;color:#166534}} .employee-daily-status.terlambat{{background:#fef3c7;color:#92400e}}
            .employee-daily-status.tk{{background:#fee2e2;color:#991b1b}} .employee-daily-status.cuti{{background:#f3e8ff;color:#6b21a8}}
            .employee-daily-status.dl{{background:#dbeafe;color:#1d4ed8}} .employee-daily-status.wfh{{background:#cffafe;color:#155e75}}
            .employee-daily-status.sah{{background:#eef2ff;color:#4338ca}}
            .employee-daily-status.libur{{background:#f1f5f9;color:#64748b}}
            .employee-daily-insights{{font-size:12px;color:#475569;line-height:1.55;margin-top:14px}}
            .employee-daily-empty{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px;color:#475569;font-size:13px}}
            @media(max-width:640px){{.employee-daily-kpis{{grid-template-columns:repeat(2,minmax(0,1fr))}}.employee-daily-attendance{{padding:14px}}}}
            </style>
            <section class='employee-daily-attendance'>
                <div class='employee-daily-kpis'>{daily_kpis}</div>
                {secondary_html}
                {table_html}
                <div class='employee-daily-insights'><strong>💡 Ringkasan Presensi</strong><br>⚠️ Pegawai mengalami {late_count} kejadian keterlambatan pada {selected_period}.<br>{dominant_day_html}🔴 TK tercatat: {tk_count} hari.</div>
            </section>
        """, unsafe_allow_html=True)

    pattern_days = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat"]
    day_counts = visible.groupby("Hari Dominan")["Terlambat"].sum().reindex(pattern_days, fill_value=0)
    st.markdown("<div class='section-title'>Pola Presensi</div>", unsafe_allow_html=True)
    if int(day_counts.sum()) == 0:
        st.info("Tidak terdapat kejadian keterlambatan pada periode yang dipilih.")
    else:
        peak_day = day_counts.idxmax()
        pattern_col, day_chart_col = st.columns([1, 2])
        with pattern_col:
            st.markdown("**Hari Keterlambatan Dominan**")
            st.markdown(f"<div style='font-size:20px;font-weight:700;color:#102a43'>{peak_day}</div><div style='color:#64748b'>{int(day_counts.max())} keterlambatan</div>", unsafe_allow_html=True)
        with day_chart_col:
            day_frame = day_counts.rename("Keterlambatan").reset_index().rename(columns={"Hari Dominan": "Hari"})
            day_chart = alt.Chart(day_frame).mark_bar(color="#f97316").encode(y=alt.Y("Hari:N", sort=pattern_days, title=None), x=alt.X("Keterlambatan:Q", title="Kali"), tooltip=["Hari:N", "Keterlambatan:Q"]).properties(height=180)
            st.altair_chart(day_chart, use_container_width=True)

    insights = []
    if int(day_counts.sum()) > 0:
        insights.append(f"⚠️ Keterlambatan paling sering terjadi pada hari {day_counts.idxmax()}, sebanyak {int(day_counts.max())} kali.")
    if len(trend) > 1:
        change = float(trend.iloc[-1]["Kehadiran Fisik"] - trend.iloc[-2]["Kehadiran Fisik"])
        if change:
            direction = "meningkat" if change > 0 else "menurun"
            insights.append(f"📈 Kehadiran Fisik {direction} {abs(change):.1f} poin dibanding {trend.iloc[-2]['Bulan']}.")
    if insights:
        st.markdown("<div class='section-title'>Insight Presensi</div>", unsafe_allow_html=True)
        for insight in insights[:3]:
            st.markdown(insight)

    st.markdown("<div class='section-title'>Rekap Presensi Bulanan</div>", unsafe_allow_html=True)
    monthly_summary = visible.copy()
    compliance = calculate_monthly_attendance_compliance(
        raw_daily, selected_nip, selected_year, month_rank[selected_month]
    )
    monthly_summary["Hari Kerja"] = compliance["calendar_workdays"]
    monthly_summary["Wajib Presensi"] = compliance["required_attendance_days"]
    compliance_percentage = compliance["compliance_percentage"]
    monthly_summary["Kepatuhan"] = (
        "-" if compliance_percentage is None
        else f"{float(compliance_percentage):.1f}%".replace(".", ",")
    )
    physical_percentage = compliance["physical_attendance_percentage"]
    monthly_summary["Hadir Fisik"] = (
        "-" if physical_percentage is None
        else f"{float(physical_percentage):.1f}%".replace(".", ",")
    )
    if compliance["duplicate_days"] or compliance["unclassified_days"]:
        st.warning(
            f"Validasi data harian: {compliance['duplicate_days']} tanggal duplikat dan "
            f"{compliance['unclassified_days']} hari wajib presensi belum terklasifikasi."
        )
    st.dataframe(
        monthly_summary[["Bulan", "Wajib Presensi", "Kepatuhan", "Hadir Fisik", "TK", "Terlambat", "Cuti", "WFH", "DL"]],
        hide_index=True,
        use_container_width=True,
        column_config={
            "Hari Kerja": st.column_config.NumberColumn("Hari Kerja", format="%d hari"),
            "Wajib Presensi": st.column_config.NumberColumn("Wajib Presensi", format="%d hari"),
            "Kepatuhan": st.column_config.TextColumn(
                "Kepatuhan",
                help="Persentase hari wajib presensi yang memiliki status sah, yaitu Hadir, Cuti, WFH, DL, SL, SK, MR, atau PBT. TK tidak dihitung sebagai status sah.",
            ),
            "Hadir Fisik": st.column_config.TextColumn(
                "Hadir Fisik",
                help="Persentase hari wajib presensi ketika pegawai benar-benar hadir. Cuti, WFH, dan DL tidak dihitung sebagai hadir fisik.",
            ),
            "TK": st.column_config.NumberColumn("TK", format="%d hari"),
            "Terlambat": st.column_config.NumberColumn("Terlambat", format="%d kali"),
            "Cuti": st.column_config.NumberColumn("Cuti", format="%d hari"),
            "WFH": st.column_config.NumberColumn("WFH", format="%d hari"),
            "DL": st.column_config.NumberColumn("DL", format="%d hari"),
        },
    )

def show_audit_trail_page() -> None:
    inject_dashboard_css()
    audit_events = st.session_state.setdefault(
        "audit_trail",
        [
            {"Waktu": "22/08 09:10", "User": "Admin", "Aktivitas": "Login ke sistem"},
            {"Waktu": "22/08 09:10", "User": "Admin", "Aktivitas": "Import data Agustus"},
            {"Waktu": "22/08 09:15", "User": "System", "Aktivitas": "Data berhasil diproses — 500 halaman"},
            {"Waktu": "22/08 09:15", "User": "System", "Aktivitas": "18 warning dibuat"},
            {"Waktu": "22/08 10:30", "User": "Operator", "Aktivitas": "Warning Pegawai A diverifikasi"},
            {"Waktu": "22/08 11:20", "User": "Operator", "Aktivitas": "Status diubah menjadi selesai"},
            {"Waktu": "22/08 11:35", "User": "Admin", "Aktivitas": "Data diperbaiki"},
            {"Waktu": "22/08 11:45", "User": "Operator", "Aktivitas": "Warning ditutup"},
        ],
    )
    with st.sidebar:
        st.markdown("# 🛡️ EWS Kehadiran")
        st.caption("Log aktivitas administrator dan operator")
        st.divider()
        user_filter = st.selectbox("Filter user", ["Semua User"] + sorted({event["User"] for event in audit_events}), key="audit_user")
        st.divider()
        st.caption(f"Login sebagai: {st.session_state.username}")
        st.button("Keluar", on_click=logout, use_container_width=True, key="audit_logout")

    visible_events = audit_events if user_filter == "Semua User" else [event for event in audit_events if event["User"] == user_filter]
    audit_table = pd.DataFrame(visible_events, columns=["Waktu", "User", "Aktivitas"])
    st.markdown("<h1 class='dashboard-title'>Audit Trail</h1>", unsafe_allow_html=True)
    st.markdown("<div class='page-context-grid'><div class='page-context-card risk-context-card'>Riwayat aktivitas administrator, operator, dan sistem</div></div>", unsafe_allow_html=True)
    total_events, system_events, operator_events = st.columns(3)
    total_events.metric("Total Aktivitas", len(visible_events))
    system_events.metric("Aktivitas System", sum(event["User"] == "System" for event in visible_events))
    operator_events.metric("Aktivitas Operator", sum(event["User"] == "Operator" for event in visible_events))
    st.markdown("<div class='section-title'>Log Aktivitas</div>", unsafe_allow_html=True)
    st.dataframe(audit_table, hide_index=True, use_container_width=True)


def show_action_center_page() -> None:
    inject_dashboard_css()
    data = load_employee_data()
    months = available_months(data)
    with st.sidebar:
        st.markdown("# 🛡️ EWS Kehadiran")
        st.caption("Pusat tindak lanjut warning")
        st.divider()
        chosen_month = st.selectbox("Periode bulan", ["Semua Bulan"] + months, key="action_month")
        units = ["Semua Unit"] + sorted(data["Unit Kerja"].unique().tolist())
        chosen_unit = st.selectbox("PIC/OPD", units, key="action_unit")
        st.divider()
        st.caption(f"Login sebagai: {st.session_state.username}")
        st.button("Keluar", on_click=logout, use_container_width=True, key="action_logout")

    filtered = data if chosen_month == "Semua Bulan" else data[data["Bulan"] == chosen_month]
    if chosen_unit != "Semua Unit":
        filtered = filtered[filtered["Unit Kerja"] == chosen_unit]
    filtered = filtered.copy()
    filtered["Risk Score"] = filtered.apply(lambda row: calculate_risk_score(row["TK"], row["Terlambat"]), axis=1)
    actions = filtered[(filtered["TK"] >= 3) | (filtered["Terlambat"] >= 8)].copy()
    actions["Warning"] = actions.apply(
        lambda row: "TK + Terlambat" if row["TK"] >= 3 and row["Terlambat"] >= 8 else ("TK" if row["TK"] >= 3 else "Terlambat"),
        axis=1,
    )
    month_number = {"Januari": "01", "Februari": "02", "Maret": "03"}
    actions["Tanggal"] = [f"{20 - (index % 10):02d}/{month_number.get(str(month), '08')}" for index, month in enumerate(actions["Bulan"])]
    actions["Status"] = actions["Risk Score"].apply(lambda score: "🔴 Belum ditindaklanjuti" if score >= 75 else "🟡 Verifikasi")
    actions["PIC"] = actions["Unit Kerja"]

    saved_status = st.session_state.setdefault("action_status", {})
    for nip, status in saved_status.items():
        actions.loc[actions["NIP"] == nip, "Status"] = status
    status_counts = actions["Status"].value_counts()
    st.markdown("<h1 class='dashboard-title'>Action Center</h1>", unsafe_allow_html=True)
    st.markdown(f"<div class='page-context-grid'><div class='page-context-card risk-context-card'>Tindak lanjut warning presensi</div><div class='page-context-card'>Periode: {chosen_month}</div></div>", unsafe_allow_html=True)
    card_1, card_2, card_3 = st.columns(3)
    card_1.metric("Belum ditindaklanjuti", int(status_counts.get("🔴 Belum ditindaklanjuti", 0)))
    card_2.metric("Verifikasi", int(status_counts.get("🟡 Verifikasi", 0)))
    card_3.metric("Selesai", int(status_counts.get("🟢 Selesai", 0)))

    st.markdown("<div class='section-title'>Daftar Action Center</div>", unsafe_allow_html=True)
    if actions.empty:
        st.success("Tidak ada warning yang perlu ditindaklanjuti pada filter ini.")
    else:
        st.dataframe(
            actions[["Nama Pegawai", "Warning", "Tanggal", "Status", "PIC"]].sort_values("Tanggal", ascending=False),
            hide_index=True,
            use_container_width=True,
        )
        selected_action = st.selectbox("Pilih pegawai untuk tindakan", actions["Nama Pegawai"].tolist(), key="action_employee")
        selected_nip = actions.loc[actions["Nama Pegawai"] == selected_action, "NIP"].iloc[0]
        action_buttons = st.columns(3)
        if action_buttons[0].button("Verifikasi", use_container_width=True, key="action_verify"):
            saved_status[selected_nip] = "🟡 Verifikasi"
            st.rerun()
        if action_buttons[1].button("Tindak Lanjut", use_container_width=True, key="action_followup"):
            saved_status[selected_nip] = "🔴 Belum ditindaklanjuti"
            st.rerun()
        if action_buttons[2].button("Tandai Selesai", use_container_width=True, key="action_done"):
            saved_status[selected_nip] = "🟢 Selesai"
            st.rerun()


ACTION_STATUSES = ["Belum Diverifikasi", "Terverifikasi", "Dalam Tindak Lanjut", "Selesai"]
ACTION_TRANSITIONS = {
    "Belum Diverifikasi": "Terverifikasi",
    "Terverifikasi": "Dalam Tindak Lanjut",
    "Dalam Tindak Lanjut": "Selesai",
}


def _action_center_warnings(data: pd.DataFrame) -> pd.DataFrame:
    warnings = data.copy()
    warnings["Status Risiko"] = warnings["TK"].map(warning_status)
    warnings = warnings[warnings["Status Risiko"] != "Normal"].copy()
    warning_year = warnings.get("Tahun", pd.Series("", index=warnings.index)).astype(str)
    warnings["warning_id"] = warnings["NIP"].astype(str) + "|" + warning_year + "|" + warnings["Bulan"].astype(str)
    states = st.session_state.setdefault("action_center_states", {})
    warnings["Status Penanganan"] = warnings["warning_id"].map(lambda warning_id: states.get(warning_id, {}).get("status", "Belum Diverifikasi"))
    warnings["Kehadiran"] = ((warnings["Hari Kerja"] - warnings["TK"] - warnings["Cuti"]) / warnings["Hari Kerja"].replace(0, 1) * 100).clip(lower=0)
    return warnings


def _record_action_activity(warning: pd.Series, action: str, before: str, after: str, note: str = "") -> None:
    user = st.session_state.get("username") or "System User"
    activity = {
        "Waktu": pd.Timestamp.now().strftime("%d/%m %H:%M"),
        "User": user,
        "Aktivitas": f"{action}: {warning['Nama Pegawai']} ({before} → {after})" + (f" — {note}" if note else ""),
    }
    log_system_activity(
        "ACTION_STATUS_CHANGE", "Status tindak lanjut berubah", activity["Aktivitas"],
        {"nip": str(warning.get("NIP", "")), "before": before, "after": after},
        module="action_center",
    )
    st.session_state.setdefault("action_center_activity", []).insert(0, activity)


def _record_report_download(file_type: str, period: str, opd: str) -> None:
    """Catat satu klik unduhan tanpa mengubah data laporan."""
    normalized = file_type.upper()
    log_system_activity(
        f"REPORT_{normalized}_DOWNLOAD", f"Laporan TK {normalized} diunduh",
        f"{period} • {opd}", {"user": st.session_state.get("username") or "System User"},
        module="laporan_tk",
    )


def show_action_center_page() -> None:
    """Antrian tindak lanjut warning; status risiko dan penanganan dipisahkan."""
    inject_dashboard_css()
    data = load_employee_data()
    months = available_months(data)
    st.markdown("<h1 style='display:block!important;visibility:visible!important;font-size:32px!important;font-weight:800!important;color:#102a43!important;text-align:left!important;margin:0 0 .3rem!important'>Action Center</h1>", unsafe_allow_html=True)
    st.markdown("<div style='color:#64748b;margin:0 0 1rem'>Kelola verifikasi dan tindak lanjut warning presensi pegawai.</div>", unsafe_allow_html=True)
    if data.empty:
        st.info("Tidak terdapat warning yang memerlukan tindak lanjut pada filter ini.")
        return

    period_col, unit_col, status_col, search_col = st.columns([1, 1, 1.25, 1.5])
    with period_col:
        selected_month = st.selectbox("Periode", ["Semua Bulan"] + months, key="action_center_period")
    with unit_col:
        selected_unit = st.selectbox("OPD", ["Semua OPD"] + sorted(data["Unit Kerja"].dropna().unique().tolist()), key="action_center_unit")
    with status_col:
        selected_status = st.selectbox("Status Penanganan", ["Semua Status"] + ACTION_STATUSES, key="action_center_status")
    with search_col:
        search = st.text_input("Cari Pegawai", placeholder="Nama, NIP, atau OPD", key="action_center_search")

    action_source = data.copy()
    if selected_month != "Semua Bulan":
        action_source = action_source[action_source["Bulan"] == selected_month]
    if selected_unit != "Semua OPD":
        action_source = action_source[action_source["Unit Kerja"] == selected_unit]
    if search:
        query = search.lower()
        action_source = action_source[action_source["Nama Pegawai"].astype(str).str.lower().str.contains(query) | action_source["NIP"].astype(str).str.lower().str.contains(query) | action_source["Unit Kerja"].astype(str).str.lower().str.contains(query)]
    if selected_month == "Semua Bulan":
        action_source = aggregate_risk_by_employee(action_source)
    warnings = _action_center_warnings(action_source)
    summary_source = warnings.copy()
    if selected_status != "Semua Status": warnings = warnings[warnings["Status Penanganan"] == selected_status]

    st.markdown("<div class='section-title'>Ringkasan Penanganan</div>", unsafe_allow_html=True)
    counts = summary_source["Status Penanganan"].value_counts()
    for column, status in zip(st.columns(4), ACTION_STATUSES):
        column.metric(status, f"{int(counts.get(status, 0))} warning")
    total = len(summary_source); completed = int(counts.get("Selesai", 0))
    st.markdown("<div class='section-title'>Progres Penanganan</div>", unsafe_allow_html=True)
    if not total:
        st.info("Tidak terdapat warning pada periode yang dipilih.")
    else:
        st.caption(f"{completed} dari {total} warning selesai")
        st.progress(completed / total)

    st.markdown("<div class='section-title'>Antrian Tindak Lanjut</div>", unsafe_allow_html=True)
    order_choice = st.selectbox("Urutkan berdasarkan", ["Prioritas Tertinggi", "Risk Score Tertinggi", "Nama Pegawai"], key="action_center_sort")
    priority = {"Kritis": 0, "Tinggi": 1, "Waspada": 2, "Normal": 3}
    warnings["_priority"] = warnings["Status Risiko"].map(priority).fillna(4)
    if order_choice == "Prioritas Tertinggi": warnings = warnings.sort_values(["_priority", "Risk Score"], ascending=[True, False])
    elif order_choice == "Risk Score Tertinggi": warnings = warnings.sort_values("Risk Score", ascending=False)
    else: warnings = warnings.sort_values("Nama Pegawai")
    if warnings.empty:
        st.success("✓ Tidak terdapat warning yang memerlukan tindak lanjut pada filter ini.")
    else:
        states = st.session_state.setdefault("action_center_states", {})
        for _, warning in warnings.head(10).iterrows():
            warning_id = warning["warning_id"]
            handling = warning["Status Penanganan"]
            risk_icon = {"Kritis": "🔴", "Tinggi": "🟠", "Waspada": "🟡"}.get(warning["Status Risiko"], "🟢")
            handling_icon = {"Belum Diverifikasi": "⚪", "Terverifikasi": "🔵", "Dalam Tindak Lanjut": "🟠", "Selesai": "🟢"}[handling]
            st.markdown(
                f"<div style='background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:14px 16px;margin:.55rem 0'><div style='display:flex;justify-content:space-between'><strong>{risk_icon} {warning['Status Risiko'].upper()}</strong><span>{handling_icon} {handling}</span></div><div style='font-size:18px;font-weight:750;color:#102a43;margin-top:.45rem'>{warning['Nama Pegawai']}</div><div style='color:#64748b'>{warning['Unit Kerja']}</div><div style='color:#64748b;margin-top:.25rem'>NIP: {warning['NIP']}</div></div>",
                unsafe_allow_html=True,
            )
            _legacy_action_card_markup = """
            handling = warning["Status Penanganan"]
            risk_icon = {"Kritis": "🔴", "Tinggi": "🟠", "Waspada": "🟡"}.get(warning["Status Risiko"], "🟢")
            handling_icon = {"Belum Diverifikasi": "⚪", "Terverifikasi": "🔵", "Dalam Tindak Lanjut": "🟠", "Selesai": "🟢"}[handling]
            st.markdown(f"<div style='background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:14px 16px;margin:.55rem 0'><div style='display:flex;justify-content:space-between'><strong>{risk_icon} {warning['Status Risiko'].upper()}</strong><span>{handling_icon} {handling}</span></div><div style='font-size:18px;font-weight:750;color:#102a43;margin-top:.45rem'>{warning['Nama Pegawai']}</div><div style='color:#64748b'>{warning['Unit Kerja']} • {warning['NIP']}</div><div style='margin-top:.45rem'>Risk Score <strong>{int(warning['Risk Score'])}/100</strong> • TK {int(warning['TK'])} hari • Terlambat {int(warning['Terlambat'])} kali</div></div>", unsafe_allow_html=True)
            st.caption(
                f"Periode: {warning['Bulan']} • Jumlah hari TK: {int(warning['TK'])} hari"
            )
            """
            detail_col, action_col = st.columns([1, 1])
            with detail_col:
                with st.expander("Detail", expanded=False):
                    st.write(f"NIP: {warning['NIP']}")
                    st.write(f"Risk Score: {int(warning['Risk Score'])} / 100")
                    st.write(f"Kehadiran: {warning['Kehadiran']:.1f}%")
                    employee_history = data[data["NIP"].astype(str) == str(warning["NIP"])].copy()
                    warning_year = int(warning["Tahun"]) if "Tahun" in warning and pd.notna(warning["Tahun"]) else int(employee_history["Tahun"].max())
                    employee_history = employee_history[employee_history["Tahun"] == warning_year]
                    if selected_month != "Semua Bulan":
                        visible_months = months[: months.index(selected_month) + 1]
                        employee_history = employee_history[employee_history["Bulan"].isin(visible_months)]
                    if selected_unit != "Semua OPD":
                        employee_history = employee_history[employee_history["Unit Kerja"] == selected_unit]
                    monthly_history = (
                        employee_history.groupby("Bulan")[["TK", "Terlambat"]]
                        .sum()
                        .reindex(months, fill_value=0)
                    )
                    tk_history = monthly_history[monthly_history["TK"] > 0][["TK"]].reset_index()
                    late_history = monthly_history[monthly_history["Terlambat"] > 0][["Terlambat"]].reset_index()
                    st.markdown("**Riwayat Tanpa Keterangan (TK)**")
                    if tk_history.empty:
                        st.caption("Tidak ada TK pada periode yang dipilih.")
                    else:
                        tk_history["TK"] = tk_history["TK"].astype(int).map(lambda value: f"{value} hari")
                        st.dataframe(tk_history, hide_index=True, use_container_width=True)
                    st.write(f"Total TK: {int(monthly_history['TK'].sum())} hari")
                    st.markdown("**Riwayat Keterlambatan**")
                    if late_history.empty:
                        st.caption("Tidak ada keterlambatan pada periode yang dipilih.")
                    else:
                        late_history["Terlambat"] = late_history["Terlambat"].astype(int).map(lambda value: f"{value} kali")
                        st.dataframe(late_history, hide_index=True, use_container_width=True)
                    st.write(f"Total Keterlambatan: {int(monthly_history['Terlambat'].sum())} kali")
                    if int(monthly_history["TK"].sum()) != int(warning["TK"]):
                        st.warning("Total riwayat TK tidak sesuai dengan warning. Periksa sumber agregasi.")
                    annual_tk = calculate_annual_tk(employee_history, warning_year)
                    pp94_result = get_pp94_indication(annual_tk)
                    st.markdown("**Indikasi Ketentuan Disiplin**")
                    st.write(f"Dasar: {REGULATION_PP94['name']} tentang {REGULATION_PP94['title']}")
                    st.write(f"Pelaksana: {REGULATION_PP94['implementation']}")
                    st.write(f"TK kumulatif tahun {warning_year}: {annual_tk} hari")
                    st.write(f"Status PP94: {pp94_result['pp94_status']}")
                    if pp94_result["has_indication"]:
                        st.write(f"Indikasi ketentuan ({pp94_result['article']}): {pp94_result['indication']}")
                    else:
                        st.write("Belum mencapai ambang ketentuan disiplin PP 94/2021. Tetap dilakukan monitoring.")
                    if pp94_result.get("near_threshold"):
                        threshold = pp94_result["near_threshold"]
                        st.info(f"Mendekati ambang berikutnya: {threshold['remaining_days']} hari lagi menuju {threshold['next_level']} ({threshold['next_threshold']} hari TK).")
                    st.caption("Pemeriksaan pola 10 hari berturut-turut memerlukan data presensi harian pada Detail Warning.")
                    st.caption("Catatan: Pemetaan ini merupakan indikasi berdasarkan data presensi. Penjatuhan hukuman disiplin tetap memerlukan verifikasi, pemeriksaan, serta keputusan pejabat yang berwenang.")
                    st.write("Rekomendasi: Verifikasi data presensi dan lakukan tindak lanjut sesuai ketentuan yang berlaku.")
            with action_col:
                if handling != "Selesai":
                    next_status = ACTION_TRANSITIONS[handling]
                    label = {"Terverifikasi": "✓ Verifikasi Data", "Dalam Tindak Lanjut": "📋 Mulai Tindak Lanjut", "Selesai": "✓ Tandai Selesai"}[next_status]
                    if st.button(label, key=f"action_center_{warning_id}"):
                        st.session_state["action_center_pending"] = {"warning": warning.to_dict(), "before": handling, "after": next_status, "label": label}
                        st.rerun()
                else:
                    st.button("✓ Selesai", disabled=True, key=f"action_done_{warning_id}")

    pending = st.session_state.get("action_center_pending")
    if pending:
        @st.dialog("Konfirmasi perubahan status")
        def confirm_action() -> None:
            st.write(f"Pegawai: {pending['warning']['Nama Pegawai']}")
            st.write(f"Status risiko: {pending['warning']['Status Risiko']}")
            st.write(f"Status penanganan: {pending['before']} → {pending['after']}")
            note = st.text_area("Catatan", key="action_center_note")
            if st.button("Konfirmasi", key="action_center_confirm"):
                current = st.session_state.setdefault("action_center_states", {}).get(pending["warning"]["warning_id"], {}).get("status", "Belum Diverifikasi")
                if current != pending["before"]:
                    st.error("Status warning sudah berubah. Muat ulang antrian.")
                    return
                st.session_state["action_center_states"][pending["warning"]["warning_id"]] = {"status": pending["after"], "updated_by": st.session_state.get("username") or "System User", "note": note}
                _record_action_activity(pd.Series(pending["warning"]), pending["label"], pending["before"], pending["after"], note)
                st.session_state.pop("action_center_pending", None)
                st.rerun()
        confirm_action()

    st.markdown("<div class='section-title'>Membutuhkan Perhatian</div>", unsafe_allow_html=True)
    pending_count = int((summary_source["Status Penanganan"] == "Belum Diverifikasi").sum())
    followup_count = int((summary_source["Status Penanganan"] == "Dalam Tindak Lanjut").sum())
    if pending_count or followup_count:
        st.caption(f"{pending_count} warning belum diverifikasi • {followup_count} warning dalam tindak lanjut. Usia warning belum tersedia pada sumber data saat ini.")
    else:
        st.caption("✓ Tidak ada warning tertunda.")

    activities = st.session_state.get("action_center_activity", [])[:5]
    if activities:
        st.markdown("<div class='section-title'>Aktivitas Terbaru</div>", unsafe_allow_html=True)
        st.dataframe(pd.DataFrame(activities)[["Waktu", "User", "Aktivitas"]], hide_index=True, use_container_width=True)
    else:
        st.caption("Belum terdapat aktivitas penanganan.")


ACTION_WORKFLOW = ["BELUM_DITINDAKLANJUTI", "DALAM_PROSES", "SELESAI"]
ACTION_LABELS = {
    "BELUM_DITINDAKLANJUTI": "Belum Ditindaklanjuti",
    "DALAM_PROSES": "Dalam Proses", "SELESAI": "Selesai",
}
ACTION_LEGACY_STATUS = {
    "BELUM_DIPROSES": "BELUM_DITINDAKLANJUTI", "VERIFIKASI": "DALAM_PROSES",
    "TINDAK_LANJUT": "DALAM_PROSES", "DITUNDA": "DALAM_PROSES",
}
ACTION_STORE_PATH = Path(__file__).resolve().parent / "data" / "action_center_state.json"
ACTION_FOLLOWUPS = ["Monitoring", "Konfirmasi kepada pegawai", "Konfirmasi kepada OPD", "Pembinaan", "Peringatan awal", "Evaluasi lebih lanjut", "Tidak diperlukan tindak lanjut"]


def _load_action_store() -> dict:
    """Status workflow tersimpan di disk, tidak hilang saat refresh atau restart."""
    if not ACTION_STORE_PATH.exists():
        return {"items": {}, "history": []}
    try:
        with ACTION_STORE_PATH.open("r", encoding="utf-8") as handle:
            store = json.load(handle)
        if isinstance(store, dict):
            store.setdefault("items", {})
            store.setdefault("history", [])
            for item in store["items"].values():
                current = item.get("action_status", ACTION_WORKFLOW[0])
                item["action_status"] = ACTION_LEGACY_STATUS.get(current, current)
            return store
    except (OSError, json.JSONDecodeError):
        pass
    return {"items": {}, "history": []}


def _save_action_store(store: dict) -> None:
    ACTION_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = ACTION_STORE_PATH.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(store, handle, ensure_ascii=False, indent=2)
    temporary.replace(ACTION_STORE_PATH)


def _action_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _action_date(value: str) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    return parsed.strftime("%d %B %Y %H:%M") if pd.notna(parsed) else "-"


def _action_priority(risk_level: str) -> str:
    return {
        "Prioritas Tindak Lanjut": "Tinggi",
        "Perlu Verifikasi": "Sedang",
        "Perlu Perhatian": "Rendah",
    }.get(risk_level, "Rendah")


def _action_waiting_days(item: dict) -> int:
    created = pd.to_datetime(item.get("created_at"), errors="coerce")
    return max((pd.Timestamp.now() - created).days, 0) if pd.notna(created) else 0


def get_action_center_items(data: pd.DataFrame) -> list[dict]:
    """Konsumsi hasil EWS dan buat tepat satu kasus per NIP + tahun."""
    active_data = apply_employee_active_status(data)
    sources = []
    for year, period_data in active_data.groupby("Tahun", dropna=False):
        source = employee_summary(period_data)
        types_by_nip = period_data.groupby("NIP")["Jenis Pegawai"].first() if "Jenis Pegawai" in period_data else pd.Series(dtype=str)
        source["Tahun"] = year
        source["Bulan"] = "Semua Bulan"
        source["Jenis Pegawai"] = source["NIP"].map(types_by_nip).fillna("Belum Diketahui")
        sources.append(source)
    source = pd.concat(sources, ignore_index=True) if sources else pd.DataFrame()
    warnings = _action_center_warnings(source) if not source.empty else source
    active_warning_ids = set(warnings["warning_id"].astype(str))
    store = _load_action_store()
    changed = False
    for _, warning in warnings.drop_duplicates("warning_id").iterrows():
        warning_id = str(warning["warning_id"])
        risk_level = str(warning["Status Risiko"])
        reason = f"TK tercatat: {int(warning['TK'])} hari"
        if int(warning.get("Terlambat", 0)):
            reason += f" • Indikator tambahan: terlambat {int(warning['Terlambat'])} kali"
        if warning_id not in store["items"]:
            now = _action_now()
            store["items"][warning_id] = {
                "warning_id": warning_id, "employee_id": str(warning["NIP"]), "nip": str(warning["NIP"]),
                "employee_name": str(warning["Nama Pegawai"]), "opd": str(warning["Unit Kerja"]),
                "risk_level": risk_level, "priority": _action_priority(risk_level), "warning_reason": reason,
                "recorded_tk_days": int(warning["TK"]), "employee_type": str(warning["Jenis Pegawai"]),
                "year": int(warning["Tahun"]), "verified_unexcused_days": None,
                "verification_status": "BELUM_DIVERIFIKASI", "pp94_indicator": None,
                "pp94_level": None, "pp94_article": None,
                "warning_date": now, "action_status": "BELUM_DITINDAKLANJUTI", "followup_type": "",
                "verification_note": "", "followup_note": "", "handled_by": "", "followup_date": "",
                "created_at": now, "updated_at": now,
            }
            store["history"].insert(0, {"warning_id": warning_id, "nip": str(warning["NIP"]), "employee_name": str(warning["Nama Pegawai"]), "timestamp": now, "user": "System", "status_before": "", "status_after": "BELUM_DITINDAKLANJUTI", "note": "Kasus dibuat dari EWS."})
            changed = True
        else:
            # Hanya metadata EWS yang diperbarui. Status workflow pengguna tetap utuh.
            item = store["items"][warning_id]
            for key, default in {
                "verified_unexcused_days": None, "verification_status": "BELUM_DIVERIFIKASI",
                "pp94_indicator": None, "pp94_level": None, "pp94_article": None,
            }.items():
                if key not in item:
                    item[key] = default
                    changed = True
            for key, value in {"employee_name": str(warning["Nama Pegawai"]), "opd": str(warning["Unit Kerja"]), "risk_level": risk_level, "priority": _action_priority(risk_level), "warning_reason": reason, "recorded_tk_days": int(warning["TK"]), "employee_type": str(warning["Jenis Pegawai"]), "year": int(warning["Tahun"])}.items():
                if item.get(key) != value:
                    item[key] = value
                    changed = True
    if changed:
        _save_action_store(store)
    # Item lama tetap tersimpan untuk backward compatibility/history, tetapi
    # pegawai yang kini Normal tidak masuk antrian aktif.
    return [item for warning_id, item in store["items"].items() if warning_id in active_warning_ids]


def update_action_center_item(warning_id: str, values: dict) -> None:
    """Simpan perubahan dan menambahkan riwayat; tidak pernah menimpa log lama."""
    store = _load_action_store()
    item = store["items"].get(warning_id)
    if item is None:
        return
    before = ACTION_LEGACY_STATUS.get(item.get("action_status"), item.get("action_status", ACTION_WORKFLOW[0]))
    requested_status = ACTION_LEGACY_STATUS.get(values.get("action_status"), values.get("action_status", before))
    if requested_status not in ACTION_WORKFLOW:
        raise ValueError("Status tindak lanjut tidak valid")
    now = _action_now()
    item.update(values)
    item["action_status"] = requested_status
    item["updated_at"] = now
    item["handled_by"] = values.get("handled_by") or st.session_state.get("username") or "System User"
    note = values.get("followup_note") or values.get("verification_note") or "Pembaruan tindak lanjut."
    store["history"].insert(0, {"warning_id": warning_id, "nip": item["nip"], "employee_name": item["employee_name"], "timestamp": now, "user": item["handled_by"], "status_before": before, "status_after": item["action_status"], "note": note})
    _save_action_store(store)
    log_system_activity(
        "ACTION_STATUS_CHANGE", "Status tindak lanjut berubah",
        f"{item['employee_name']} • {ACTION_LABELS.get(before, before)} → {ACTION_LABELS.get(item['action_status'], item['action_status'])}",
        {"warning_id": warning_id, "nip": item["nip"], "before": before, "after": item["action_status"], "user": item["handled_by"]},
        module="action_center",
    )
    if "verified_unexcused_days" in values:
        verified_label = "belum ditetapkan" if values["verified_unexcused_days"] is None else f"{values['verified_unexcused_days']} hari"
        st.session_state.setdefault("audit_trail", []).insert(0, {
            "Waktu": pd.Timestamp.now().strftime("%d/%m %H:%M"), "User": item["handled_by"],
            "Aktivitas": f"Verifikasi hari tanpa alasan sah disimpan: {item['employee_name']} — {verified_label}",
        })
    if values.get("pp94_indicator"):
        st.session_state.setdefault("audit_trail", []).insert(0, {
            "Waktu": pd.Timestamp.now().strftime("%d/%m %H:%M"), "User": item["handled_by"],
            "Aktivitas": f"Indikator PP94 diperbarui: {item['employee_name']} — {values['pp94_indicator']}",
        })


def show_action_center_page_focus() -> None:
    """Halaman operasional: antrian, workflow, dan riwayat tindak lanjut."""
    inject_dashboard_css()
    data = load_employee_data()
    st.markdown("<h1 style='font-size:30px;font-weight:700;color:#173b63;margin:0'>Action Center</h1>", unsafe_allow_html=True)
    st.markdown("<div style='font-size:14px;color:#64748b;margin:.25rem 0 .8rem'>Kelola proses verifikasi dan tindak lanjut warning presensi pegawai.</div>", unsafe_allow_html=True)
    if data.empty:
        st.info("✅ Tidak terdapat warning yang membutuhkan tindak lanjut saat ini.")
        return
    all_items = get_action_center_items(data)
    if not all_items:
        st.info("✅ Tidak terdapat warning yang membutuhkan tindak lanjut saat ini.")
        return
    items = pd.DataFrame(all_items)

    status_col, opd_col, priority_col, search_col = st.columns([1.2, 1.2, 1, 1.4])
    with status_col:
        status_options = ["Semua Status Aktif"] + ACTION_WORKFLOW
        selected_status = st.selectbox("Status Penanganan", status_options, format_func=lambda value: "Semua Status Aktif" if value == "Semua Status Aktif" else ACTION_LABELS[value], key="action_focus_status")
        statuses = ACTION_WORKFLOW[:3] if selected_status == "Semua Status Aktif" else [selected_status]
    with opd_col:
        selected_opd = st.selectbox("OPD", ["Semua OPD"] + sorted(items["opd"].dropna().unique().tolist()), key="action_focus_opd")
    with priority_col:
        selected_priority = st.selectbox("Prioritas", ["Semua Prioritas", "Tinggi", "Sedang", "Rendah"], key="action_focus_priority")
    with search_col:
        search = st.text_input("Cari Pegawai", placeholder="Nama atau NIP", key="action_focus_search")

    scoped = items.copy()
    if selected_opd != "Semua OPD":
        scoped = scoped[scoped["opd"] == selected_opd]
    if selected_priority != "Semua Prioritas":
        scoped = scoped[scoped["priority"] == selected_priority]
    if search:
        query = search.lower()
        scoped = scoped[scoped["employee_name"].astype(str).str.lower().str.contains(query, na=False) | scoped["nip"].astype(str).str.contains(query, na=False)]
    counts = scoped["action_status"].value_counts()
    action_helpers = {"BELUM_DIPROSES": "Perlu tindakan", "VERIFIKASI": "Sedang diperiksa", "TINDAK_LANJUT": "Sedang ditangani", "SELESAI": "Telah diselesaikan"}
    action_colors = {"BELUM_DIPROSES": "#e11d48", "VERIFIKASI": "#f59e0b", "TINDAK_LANJUT": "#2563eb", "SELESAI": "#16a34a"}
    action_cards = "".join(f"<div style='background:#fff;border:1px solid #e2e8f0;border-left:4px solid {action_colors[status]};border-radius:14px;padding:16px 18px;min-height:108px;box-shadow:0 3px 10px rgba(15,23,42,.035)'><div style='font-size:13px;font-weight:700;color:{action_colors[status]}'>{ACTION_LABELS[status]}</div><div style='font-size:32px;font-weight:750;color:#0f172a;margin-top:6px'>{int(counts.get(status, 0))}</div><div style='font-size:12px;color:#64748b;margin-top:5px'>{action_helpers[status]}</div></div>" for status in ACTION_WORKFLOW[:4])
    st.markdown(f"<div style='display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:0 0 18px'>{action_cards}</div>", unsafe_allow_html=True)

    queue = scoped[scoped["action_status"].isin(statuses)].copy() if statuses else scoped.iloc[0:0]
    status_order = {status: index for index, status in enumerate(ACTION_WORKFLOW)}
    priority_order = {"Tinggi": 0, "Sedang": 1, "Rendah": 2}
    queue["_status"] = queue["action_status"].map(status_order)
    queue["_priority"] = queue["priority"].map(priority_order)
    queue = queue.sort_values(["_status", "_priority", "updated_at"], ascending=[True, True, True])
    st.markdown("<div class='section-title'>🚨 Antrian Tindak Lanjut</div>", unsafe_allow_html=True)
    if queue.empty:
        st.info("Belum terdapat tindak lanjut yang selesai pada filter ini." if statuses == ["SELESAI"] else "✅ Tidak terdapat warning yang membutuhkan tindak lanjut saat ini.")
    for _, item in queue.iterrows():
        waiting = _action_waiting_days(item)
        priority_icon = {"Tinggi": "🔴", "Sedang": "🟠", "Rendah": "🟡"}.get(item["priority"], "⚪")
        st.markdown(f"<div style='background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:14px 16px;margin:.55rem 0'><div style='display:flex;justify-content:space-between'><strong>{priority_icon} PRIORITAS {item['priority'].upper()}</strong><span>{ACTION_LABELS[item['action_status']]}</span></div><div style='font-size:18px;font-weight:750;color:#102a43;margin-top:.45rem'>{item['employee_name']}</div><div style='color:#64748b'>{item['opd']} • NIP: {item['nip']}</div><div style='margin-top:.4rem'><strong>Alasan:</strong> {item['warning_reason']}<br><span style='color:#64748b'>Update: {_action_date(item['updated_at'])} • Menunggu: {waiting} hari</span></div></div>", unsafe_allow_html=True)
        with st.expander("Proses / Detail", expanded=False):
            st.markdown("#### Detail Tindak Lanjut")
            st.write(f"**Nama:** {item['employee_name']}  ")
            st.write(f"**NIP:** {item['nip']}  ")
            st.write(f"**OPD:** {item['opd']}")
            st.markdown("**Informasi Warning**")
            st.write(f"Status EWS: {item['risk_level']} • Alasan: {item['warning_reason']}")
            st.write(f"Tanggal warning: {_action_date(item['warning_date'])}")
            step = ACTION_WORKFLOW.index(item["action_status"]) if item["action_status"] in ACTION_WORKFLOW else 0
            st.progress(min(step, 3) / 3)
            st.caption(" → ".join(["Belum Diproses", "Verifikasi", "Tindak Lanjut", "Selesai"]))
            with st.form(f"action_form_{item['warning_id']}"):
                action_status = st.selectbox("Status Penanganan", ACTION_WORKFLOW, index=step, format_func=lambda status: ACTION_LABELS[status])
                recorded_tk = int(item.get("recorded_tk_days", 0))
                st.text_input("Hari TK tercatat", value=str(recorded_tk), disabled=True)
                saved_verified_days = item.get("verified_unexcused_days")
                saved_verified_days = None if pd.isna(saved_verified_days) else int(saved_verified_days)
                verified_days = st.number_input(
                    "Hari tidak masuk tanpa alasan sah terverifikasi",
                    min_value=0, max_value=recorded_tk, value=saved_verified_days,
                    step=1, placeholder="Belum diverifikasi",
                )
                verification_status = st.selectbox(
                    "Status Verifikasi",
                    ["BELUM_DIVERIFIKASI", "DALAM_VERIFIKASI", "TERVERIFIKASI"],
                    index=["BELUM_DIVERIFIKASI", "DALAM_VERIFIKASI", "TERVERIFIKASI"].index(item.get("verification_status", "BELUM_DIVERIFIKASI")) if item.get("verification_status") in {"BELUM_DIVERIFIKASI", "DALAM_VERIFIKASI", "TERVERIFIKASI"} else 0,
                )
                verification = st.text_area("Hasil Verifikasi", value=item.get("verification_note", ""), placeholder="Tuliskan hasil verifikasi terhadap data presensi...")
                followup_type = st.selectbox("Jenis Tindak Lanjut", ACTION_FOLLOWUPS, index=ACTION_FOLLOWUPS.index(item["followup_type"]) if item.get("followup_type") in ACTION_FOLLOWUPS else 0)
                followup_note = st.text_area("Catatan Tindak Lanjut", value=item.get("followup_note", ""), placeholder="Tuliskan catatan atau hasil tindak lanjut...")
                saved_date = pd.to_datetime(item.get("followup_date"), errors="coerce")
                followup_date = st.date_input("Tanggal Tindak Lanjut", value=saved_date.date() if pd.notna(saved_date) else date.today())
                st.caption(f"Ditangani oleh: {st.session_state.get('username') or 'System User'}")
                save_col, finish_col = st.columns(2)
                save = save_col.form_submit_button("Simpan Tindak Lanjut", use_container_width=True)
                finish = finish_col.form_submit_button("Tandai Selesai", use_container_width=True)
            if save or finish:
                pp94 = evaluate_pp94_indicator(
                    year=int(item.get("year", pd.Timestamp.now().year)),
                    recorded_tk_days=recorded_tk,
                    employee_type=item.get("employee_type"),
                    verified_unexcused_days=int(verified_days) if verified_days is not None else None,
                )
                update_action_center_item(str(item["warning_id"]), {
                    "action_status": "SELESAI" if finish else action_status,
                    "verified_unexcused_days": int(verified_days) if verified_days is not None else None,
                    "verification_status": verification_status,
                    "pp94_indicator": pp94.get("indicator"), "pp94_level": pp94.get("discipline_level"),
                    "pp94_article": pp94.get("article"),
                    "verification_note": verification, "followup_type": followup_type,
                    "followup_note": followup_note, "followup_date": followup_date.isoformat(),
                    "handled_by": st.session_state.get("username") or "System User",
                })
                st.rerun()
            pp94_indicator = safe_display(item.get("pp94_indicator"))
            pp94_level = safe_display(item.get("pp94_level"), "Belum mencapai ambang")
            pp94_article = safe_display(item.get("pp94_article"), REGULATION_PP94["name"])
            st.markdown("**Indikasi Tingkat Tindak Lanjut**")
            st.write(pp94_indicator)
            if pp94_indicator != "Tidak ada indikasi tindak lanjut":
                st.write(f"**Rekomendasi Tingkat Tindak Lanjut:** {pp94_level}")
                st.write(f"**Dasar Ketentuan:** {pp94_article}")
            else:
                st.info("Memerlukan Verifikasi Pengelola sebelum indikator PP 94/2021 ditampilkan.")
            st.caption("Rekomendasi sistem merupakan informasi pendukung berdasarkan indikator presensi dan bukan keputusan penjatuhan hukuman disiplin. Pemeriksaan, klarifikasi, verifikasi, dan keputusan tetap menjadi kewenangan pejabat yang berwenang.")

    overdue = scoped[(scoped["action_status"] == "BELUM_DIPROSES") & (scoped.apply(_action_waiting_days, axis=1) >= 3)]
    st.markdown("<div class='section-title'>Kasus Belum Ditindaklanjuti Terlalu Lama</div>", unsafe_allow_html=True)
    if not overdue.empty:
        st.warning(f"⚠️ {len(overdue)} warning belum diproses lebih dari 3 hari.")
    else:
        st.caption("Tidak ada kasus belum diproses lebih dari 3 hari.")
    completed = int(counts.get("SELESAI", 0))
    high_active = int(((scoped["priority"] == "Tinggi") & (scoped["action_status"] != "SELESAI")).sum())
    st.markdown("<div class='section-title'>💡 Insight Action Center</div>", unsafe_allow_html=True)
    st.markdown(f"⚠️ {len(overdue)} warning belum diproses lebih dari 3 hari.  ")
    st.markdown(f"🔴 {high_active} kasus prioritas tinggi masih aktif.  ")
    st.markdown(f"✅ {completed} tindak lanjut telah diselesaikan.")
    history = pd.DataFrame(_load_action_store().get("history", []))
    st.markdown("<div class='section-title'>Riwayat Tindakan</div>", unsafe_allow_html=True)
    if history.empty:
        st.caption("Belum terdapat riwayat tindakan.")
    else:
        history["Waktu"] = history["timestamp"].map(_action_date)
        history["Status"] = history.apply(lambda row: f"{ACTION_LABELS.get(row['status_before'], '-')} → {ACTION_LABELS.get(row['status_after'], '-')}", axis=1)
        st.dataframe(history[["Waktu", "employee_name", "user", "Status", "note"]].head(20), hide_index=True, use_container_width=True)
    st.caption("Action Center digunakan untuk membantu proses verifikasi dan pencatatan tindak lanjut atas indikator Early Warning System. Hasil sistem tidak secara otomatis menetapkan hukuman disiplin dan tetap memerlukan verifikasi serta keputusan pejabat yang berwenang.")


def show_action_center_page_focus() -> None:
    """Workflow operasional tindak lanjut; analitik EWS tetap di halaman EWS."""
    inject_dashboard_css()
    st.markdown("""
    <style>
    .action-center-page{color:#0f172a}.action-center-page h1{font-size:30px;color:#173b63;margin:0}
    .action-center-page .subtitle{color:#64748b;margin:4px 0 16px}.action-summary{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:16px 0 22px}
    .action-summary .card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:15px 18px}.action-summary .value{font-size:28px;font-weight:750}.action-summary .label{font-size:12px;color:#64748b}
    .action-case-card{background:#fff;border:1px solid #e2e8f0;border-radius:13px;padding:16px 18px 14px;margin:10px 0 6px;color:#0f172a}.action-case-card-grid{display:grid;grid-template-columns:minmax(220px,.9fr) minmax(280px,1.35fr);gap:14px 26px}
    .action-case-name{font-size:19px;font-weight:700;line-height:1.25;color:#173b63}.action-case-nip{font-size:12px;color:#64748b;margin-top:3px}.action-case-opd{font-size:14px;font-weight:600;line-height:1.4;color:#0f172a}
    .action-case-label{font-size:12px;font-weight:600;color:#64748b;margin-bottom:4px}.action-case-badges{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.action-case-indication,.action-status-badge{display:inline-block;border-radius:999px;padding:5px 10px;font-size:11px;font-weight:700}.action-case-indication{background:#fff7ed;border:1px solid #fed7aa;color:#9a3412}
    .action-status-badge.pending{background:#fffbeb;color:#92400e}.action-status-badge.process{background:#eff6ff;color:#1d4ed8}.action-status-badge.done{background:#ecfdf5;color:#047857}.action-case-reason{font-size:13px;font-weight:600;color:#334155}.action-case-rule{font-size:12px;line-height:1.5;color:#475569;margin-top:7px}.action-case-updated{font-size:13px;font-weight:600;color:#0f172a}
    .action-update-panel{padding:2px 2px 4px}.action-update-title{font-size:15px;font-weight:700;color:#173b63;margin:0 0 10px}.action-panel-divider{border-top:1px solid #e2e8f0;margin:16px 0}.action-history{border-left:2px solid #dbeafe;padding:1px 0 1px 12px;margin:8px 0}.action-history-date{font-size:11px;color:#64748b}.action-history-status{font-size:13px;font-weight:700;color:#173b63}.action-history-note{font-size:12px;color:#475569;margin-top:2px}
    @media(max-width:800px){.action-summary,.action-case-card-grid{grid-template-columns:1fr}.action-center-page{padding:0 2px}}
    </style><div class='action-center-page'><h1>Action Center</h1><div class='subtitle'>Pengelolaan tindak lanjut hasil monitoring presensi pegawai.</div></div>
    """, unsafe_allow_html=True)
    try:
        data = load_employee_data()
        all_items = get_action_center_items(data) if not data.empty else []
    except Exception:
        LOGGER.exception("Gagal memuat Action Center")
        st.error("Action Center belum berhasil dimuat.")
        return
    if not all_items:
        st.info("Belum terdapat tindak lanjut yang perlu diproses.")
        return

    items = pd.DataFrame(all_items).drop_duplicates("warning_id")
    items["action_status"] = items["action_status"].map(lambda value: ACTION_LEGACY_STATUS.get(value, value))
    filter_cols = st.columns(4)
    with filter_cols[0]:
        selected_opd = st.selectbox("OPD", ["Semua OPD"] + sorted(items["opd"].dropna().unique().tolist()), key="action_focus_opd_v2")
    with filter_cols[1]:
        selected_status = st.selectbox("Status Tindak Lanjut", ["Kasus Aktif", "Semua Status"] + ACTION_WORKFLOW, format_func=lambda value: ACTION_LABELS.get(value, value), key="action_focus_status_v2")
    with filter_cols[2]:
        indications = ["Semua Indikasi"] + [value for value in WARNING_STATUSES if value != "Normal"]
        selected_indication = st.selectbox("Tingkat Indikasi", indications, key="action_focus_indication")
    with filter_cols[3]:
        periods = ["Semua Periode"] + [str(value) for value in sorted(pd.to_numeric(items["year"], errors="coerce").dropna().astype(int).unique(), reverse=True)]
        selected_period = st.selectbox("Periode", periods, key="action_focus_period")

    scoped = items.copy()
    if selected_opd != "Semua OPD": scoped = scoped[scoped["opd"].eq(selected_opd)]
    if selected_indication != "Semua Indikasi": scoped = scoped[scoped["risk_level"].eq(selected_indication)]
    if selected_period != "Semua Periode": scoped = scoped[pd.to_numeric(scoped["year"], errors="coerce").eq(int(selected_period))]
    counts = scoped.drop_duplicates("warning_id")["action_status"].value_counts()
    card_classes = {ACTION_WORKFLOW[0]: "pending", ACTION_WORKFLOW[1]: "process", ACTION_WORKFLOW[2]: "done"}
    cards = "".join(f"<div class='card'><div class='value'>{int(counts.get(status, 0))}</div><div class='label'>{ACTION_LABELS[status]} · kasus unik</div></div>" for status in ACTION_WORKFLOW)
    st.markdown(f"<div class='action-center-page'><div class='action-summary'>{cards}</div></div>", unsafe_allow_html=True)

    if selected_status == "Kasus Aktif": scoped = scoped[scoped["action_status"].isin(ACTION_WORKFLOW[:2])]
    elif selected_status != "Semua Status": scoped = scoped[scoped["action_status"].eq(selected_status)]
    priority_order = {"Prioritas Tindak Lanjut": 0, "Perlu Verifikasi": 1, "Perlu Perhatian": 2}
    scoped["_priority"] = scoped["risk_level"].map(priority_order).fillna(9)
    scoped["_updated"] = pd.to_datetime(scoped["updated_at"], errors="coerce")
    scoped = scoped.sort_values(["_priority", "_updated"], ascending=[True, False])
    st.markdown("### Daftar Tindak Lanjut")
    if scoped.empty:
        message = "Seluruh tindak lanjut pada parameter ini telah selesai." if selected_status == "Kasus Aktif" and int(counts.get("SELESAI", 0)) else "Belum terdapat tindak lanjut yang perlu diproses."
        st.info(message)
        return

    store_history = _load_action_store().get("history", [])
    daily_rule_source = _load_excel_daily_data(_excel_source_signature())
    for _, item in scoped.iterrows():
        status = item["action_status"]
        updated = pd.to_datetime(item.get("updated_at") or item.get("created_at"), errors="coerce")
        month_short = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "Mei", 6: "Jun", 7: "Jul", 8: "Agu", 9: "Sep", 10: "Okt", 11: "Nov", 12: "Des"}
        updated_label = f"{updated.day} {month_short[updated.month]} {updated.year}" if pd.notna(updated) else "-"
        display_opd = str(item["opd"]).title().replace(" Dan ", " dan ")
        recorded_tk = int(item.get("recorded_tk_days", 0))
        item_daily = daily_rule_source[
            daily_rule_source["NIP"].astype(str).eq(str(item["nip"]))
            & pd.to_numeric(daily_rule_source["Tahun"], errors="coerce").eq(int(item["year"]))
        ]
        if item_daily.empty:
            annual_recorded_tk, consecutive_tk = recorded_tk, None
            rule_period, rule_granularity = str(item["year"]), "AGGREGATED"
        else:
            last_date = pd.to_datetime(item_daily["Tanggal"], errors="coerce").max()
            active_month_number = int(last_date.month)
            annual_recorded_tk = calculate_ytd_tk(item_daily, int(item["year"]), active_month_number)
            consecutive_tk = calculate_consecutive_unexcused_days(item_daily, int(item["year"]), active_month_number)
            rule_period, rule_granularity = last_date.date().isoformat(), "DAILY"
        verified_value = item.get("verified_unexcused_days")
        rule_days = int(verified_value) if pd.notna(verified_value) else annual_recorded_tk
        monitoring = evaluate_attendance_monitoring_rule(
            rule_days, consecutive_tk, consecutive_tk is not None,
            nip=str(item["nip"]), employee_type=item.get("employee_type"),
            period_end=rule_period, data_granularity=rule_granularity,
        )
        late_match = re.search(r"terlambat\s+(\d+)\s+kali", str(item.get("warning_reason", "")), flags=re.IGNORECASE)
        late_value = f"{late_match.group(1)} kali" if late_match else "tidak tersedia"
        reason_summary = f"TK tahun berjalan {annual_recorded_tk} hari • Terlambat {late_value}"
        st.markdown(f"""<article class='action-case-card'><div class='action-case-card-grid'>
            <div><div class='action-case-name'>{escape(str(item['employee_name']))}</div><div class='action-case-nip'>NIP {escape(str(item['nip']))}</div></div>
            <div><div class='action-case-label'>OPD</div><div class='action-case-opd'>{escape(display_opd)}</div></div>
            <div class='action-case-badges'><span class='action-case-indication'>{escape(str(item['risk_level']))}</span><span class='action-status-badge {card_classes[status]}'>{ACTION_LABELS[status]}</span></div>
            <div><div class='action-case-label'>Indikator Presensi</div><div class='action-case-reason'>{escape(reason_summary)}</div><div class='action-case-rule'><strong>Status Monitoring:</strong> {escape(str(monitoring['reference_status']))}<br><strong>Ambang Referensi:</strong> {escape(str(monitoring['reference_band']))}</div></div>
            <div><div class='action-case-label'>Terakhir diperbarui</div><div class='action-case-updated'>{updated_label}</div></div>
        </div></article>""", unsafe_allow_html=True)
        with st.expander("Lihat / Update Tindak Lanjut"):
            case_history = [event for event in store_history if str(event.get("warning_id")) == str(item["warning_id"])]
            st.markdown("<div class='action-update-panel'><div class='action-update-title'>Update Tindak Lanjut</div></div>", unsafe_allow_html=True)
            st.write(f"Catatan monitoring: {monitoring['monitoring_note']}")
            st.write(f"Dasar monitoring: {monitoring['regulation']} — {monitoring['reference_article'] or 'Monitoring awal'}")
            st.caption(MONITORING_DISCLAIMER)
            with st.form(f"action_case_{item['warning_id']}"):
                new_status = st.selectbox("Status Tindak Lanjut", ACTION_WORKFLOW, index=ACTION_WORKFLOW.index(status), format_func=lambda value: ACTION_LABELS[value])
                saved_date = pd.to_datetime(item.get("followup_date"), errors="coerce")
                followup_date = st.date_input("Tanggal Tindak Lanjut", value=saved_date.date() if pd.notna(saved_date) else date.today())
                note = st.text_area("Catatan Tindak Lanjut", value=str(item.get("followup_note") or ""), placeholder="Tuliskan hasil klarifikasi atau tindak lanjut...")
                saved = st.form_submit_button("Simpan Tindak Lanjut")
            if saved:
                try:
                    update_action_center_item(str(item["warning_id"]), {"action_status": new_status, "followup_note": note.strip(), "followup_date": followup_date.isoformat()})
                    st.rerun()
                except Exception:
                    LOGGER.exception("Gagal menyimpan tindak lanjut %s", item["warning_id"])
                    st.error("Tindak lanjut belum berhasil disimpan.")
            st.markdown("<div class='action-panel-divider'></div><div class='action-update-title'>Riwayat Tindak Lanjut</div>", unsafe_allow_html=True)
            if not case_history: st.caption("Belum terdapat riwayat tindak lanjut.")
            for event in case_history:
                event_status = ACTION_LEGACY_STATUS.get(event.get("status_after"), event.get("status_after"))
                event_time = pd.to_datetime(event.get("timestamp"), errors="coerce")
                event_date = f"{event_time.day} {month_short[event_time.month]} {event_time.year}" if pd.notna(event_time) else "-"
                st.markdown(f"<div class='action-history'><div class='action-history-date'>{event_date}</div><div class='action-history-status'>{escape(ACTION_LABELS.get(event_status, event_status or '-'))}</div><div class='action-history-note'>{escape(str(event.get('note') or '-'))}</div></div>", unsafe_allow_html=True)
            st.markdown("<div class='action-panel-divider'></div>", unsafe_allow_html=True)
            if st.button("Lihat Detail Pegawai →", key=f"action_employee_{item['warning_id']}"):
                st.session_state["employee_detail_opd"] = str(item["opd"])
                st.session_state["employee_detail_employee"] = f"{item['employee_name']} — {item['nip']}"
                st.session_state["navigate_to_page"] = "Detail Pegawai"
                st.rerun()


def _classify_audit_activity(text: str) -> str:
    lower = text.lower()
    if "login" in lower: return "Login"
    if "import" in lower: return "Import Data"
    if "verifikasi" in lower: return "Verifikasi Warning"
    if "tindak lanjut" in lower or "mulai" in lower: return "Tindak Lanjut"
    if "selesai" in lower or "ditutup" in lower: return "Warning Selesai"
    if "warning" in lower: return "Warning"
    if "data" in lower: return "Perubahan Data"
    return "Lainnya"


def _audit_target(text: str) -> str:
    """Menurunkan target hanya bila format aktivitas memang menyebutkannya."""
    if ":" in text:
        target = text.split(":", 1)[1].split("(", 1)[0].strip()
        return target or "-"
    return "-"


def _render_audit_activity_tab() -> None:
    """Tab aktivitas read-only yang memakai log session existing."""
    events = st.session_state.get("audit_trail", [])
    if not events:
        st.info("Belum terdapat aktivitas.")
        return

    logs = pd.DataFrame(events).copy()
    for column in ("Waktu", "User", "Aktivitas"):
        if column not in logs:
            logs[column] = "-"
    logs["log_id"] = [f"log-{index}" for index in logs.index]
    logs["Kategori"] = logs["Aktivitas"].astype(str).map(_classify_audit_activity)
    logs["Target"] = logs["Aktivitas"].astype(str).map(_audit_target)
    logs["_waktu"] = pd.to_datetime(logs["Waktu"], dayfirst=True, errors="coerce")
    logs = logs.sort_values("_waktu", ascending=False, na_position="last")

    period_col, actor_col, category_col, search_col = st.columns([1, 1, 1.25, 1.5])
    with period_col:
        period = st.selectbox("Periode", ["Semua Data", "Hari Ini", "7 Hari Terakhir", "30 Hari Terakhir", "Bulan Ini"], key="audit_period")
    with actor_col:
        actor = st.selectbox("Aktor", ["Semua Aktor"] + sorted(logs["User"].dropna().astype(str).unique().tolist()), key="audit_actor")
    with category_col:
        category = st.selectbox("Aktivitas", ["Semua Aktivitas"] + sorted(logs["Kategori"].unique().tolist()), key="audit_category")
    with search_col:
        search = st.text_input("Cari", placeholder="User, aktivitas, atau target", key="audit_search_v2")

    filtered = logs.copy()
    now = pd.Timestamp.now()
    if period != "Semua Data" and filtered["_waktu"].notna().any():
        cutoff = {"Hari Ini": now.normalize(), "7 Hari Terakhir": now - pd.Timedelta(days=7), "30 Hari Terakhir": now - pd.Timedelta(days=30), "Bulan Ini": now.replace(day=1).normalize()}[period]
        filtered = filtered[filtered["_waktu"].isna() | (filtered["_waktu"] >= cutoff)]
    if actor != "Semua Aktor": filtered = filtered[filtered["User"] == actor]
    if category != "Semua Aktivitas": filtered = filtered[filtered["Kategori"] == category]
    if search:
        query = search.lower()
        filtered = filtered[filtered[["User", "Aktivitas", "Target"]].astype(str).apply(lambda row: row.str.lower().str.contains(query).any(), axis=1)]

    st.markdown("<div class='section-title'>Ringkasan Aktivitas</div>", unsafe_allow_html=True)
    actors = filtered["User"].value_counts()
    summary_items = [("Total Aktivitas", len(filtered))] + [(name, int(count)) for name, count in actors.head(3).items()]
    for column, (label, count) in zip(st.columns(4), summary_items + [("-", 0)] * max(0, 4 - len(summary_items))):
        column.metric(label, count)

    if filtered.empty:
        st.info("Tidak terdapat aktivitas yang sesuai dengan filter.")
        return
    st.markdown("<div class='section-title'>Aktivitas Terbaru</div>", unsafe_allow_html=True)
    for _, log in filtered.head(5).iterrows():
        st.markdown(f"<div style='background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:10px 14px;margin:.35rem 0'><strong>{log['Waktu']}</strong> &nbsp; {log['Aktivitas']}<br><span style='color:#64748b'>{log['User']}</span></div>", unsafe_allow_html=True)

    st.markdown("<div class='section-title'>Log Aktivitas</div>", unsafe_allow_html=True)
    limit = st.selectbox("Tampilkan", [20, 50], key="audit_limit")
    table = filtered.head(limit).copy()
    st.caption(f"Menampilkan {len(table)} dari {len(filtered)} aktivitas")
    st.dataframe(table[["Waktu", "User", "Kategori", "Aktivitas", "Target"]], hide_index=True, use_container_width=True)
    st.download_button("Unduh Log CSV", filtered[["Waktu", "User", "Kategori", "Aktivitas", "Target"]].to_csv(index=False).encode("utf-8"), file_name="audit_trail.csv", mime="text/csv")

    details = {f"{row['Waktu']} — {row['Aktivitas']}": row["log_id"] for _, row in table.iterrows()}
    if details:
        selected = st.selectbox("Lihat detail aktivitas", list(details), key="audit_detail_select")
        detail = filtered[filtered["log_id"] == details[selected]].iloc[0]
        with st.expander("Detail Aktivitas", expanded=False):
            st.write(f"Aktor: {detail['User']}")
            st.write(f"Waktu: {detail['Waktu']}")
            st.write(f"Kategori: {detail['Kategori']}")
            st.write(f"Target: {detail['Target']}")
            st.write(f"Aktivitas: {detail['Aktivitas']}")


def _render_simple_audit_activity_timeline() -> None:
    """Timeline ringkas dari shared persistent activity log."""
    events = load_system_activities(20) + st.session_state.get("audit_trail", [])
    if not events:
        st.info("Belum terdapat aktivitas sistem.")
        return

    logs = pd.DataFrame(events).copy()
    if "timestamp" in logs:
        logs["Waktu"] = logs.get("Waktu", pd.Series(index=logs.index, dtype=object)).fillna(logs["timestamp"])
    if "title" in logs:
        descriptions = logs.get("description", pd.Series("", index=logs.index)).fillna("")
        logs["Aktivitas"] = logs.get("Aktivitas", pd.Series(index=logs.index, dtype=object)).fillna(
            logs["title"].fillna("") + descriptions.map(lambda value: f" — {value}" if value else "")
        )
    for column in ("Waktu", "User", "Aktivitas"):
        if column not in logs:
            logs[column] = "-"
    now = pd.Timestamp.now(tz=APP_TIMEZONE)

    def parse_time(value: object) -> pd.Timestamp:
        text = str(value).strip()
        try:
            parsed = pd.Timestamp(text)
            if parsed.tzinfo is not None:
                return parsed.tz_convert(APP_TIMEZONE)
        except (ValueError, TypeError):
            pass
        for date_format in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
            try:
                return pd.Timestamp(datetime.strptime(text, date_format), tz=APP_TIMEZONE)
            except ValueError:
                pass
        for date_format in ("%d/%m %H:%M", "%d/%m %H:%M:%S"):
            try:
                return pd.Timestamp(datetime.strptime(f"{text}/{now.year}", f"{date_format}/%Y"), tz=APP_TIMEZONE)
            except ValueError:
                pass
        return pd.NaT

    def icon_for(activity: object) -> str:
        text = str(activity).lower()
        if "pdf" in text:
            return "&#128196;"
        if "excel" in text:
            return "&#128202;"
        if any(word in text for word in ("gagal", "error", "warning")):
            return "&#9888;"
        if any(word in text for word in ("proses", "import", "selesai", "berhasil", "verifikasi", "ditutup")):
            return "&#10003;"
        return "&#8226;"

    def content_for(row: pd.Series) -> tuple[str, str]:
        activity = str(row["Aktivitas"]).strip() or "Aktivitas sistem"
        title, detail = activity, ""
        for separator in (" — ", " â€” "):
            if separator in activity:
                title, detail = activity.split(separator, 1)
                break
        actor = str(row.get("User", "")).strip()
        if not detail and actor and actor != "-":
            detail = f"Oleh {actor}"
        return title, detail

    logs["_waktu"] = logs["Waktu"].map(parse_time)
    logs = logs.sort_values("_waktu", ascending=False, na_position="last").head(20)
    month_names = {
        1: "Januari", 2: "Februari", 3: "Maret", 4: "April",
        5: "Mei", 6: "Juni", 7: "Juli", 8: "Agustus",
        9: "September", 10: "Oktober", 11: "November", 12: "Desember",
    }

    def date_label(timestamp: pd.Timestamp) -> str:
        if pd.isna(timestamp):
            return "Tanggal tidak tersedia"
        if timestamp.normalize() == now.normalize():
            return "Hari ini"
        if timestamp.normalize() == now.normalize() - pd.Timedelta(days=1):
            return "Kemarin"
        return f"{timestamp.day} {month_names[timestamp.month]} {timestamp.year}"

    st.markdown("""
    <style>
    .audit-activity-timeline{max-width:880px;background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:8px 20px 14px}
    .audit-activity-group{margin-top:18px}.audit-activity-group:first-child{margin-top:6px}.audit-activity-group-title{font-size:14px;font-weight:750;color:#173b63;padding:8px 0;border-bottom:1px solid #e2e8f0}
    .audit-activity-item{display:grid;grid-template-columns:52px 20px minmax(0,1fr);gap:9px;align-items:start;padding:12px 0;border-bottom:1px solid #f1f5f9}.audit-activity-item:last-child{border-bottom:0}
    .audit-activity-time{font-size:12px;font-weight:700;color:#475569;line-height:20px}.audit-activity-icon{font-size:14px;color:#2563eb;line-height:20px;text-align:center}.audit-activity-title{font-size:13px;font-weight:650;color:#172033;line-height:20px}.audit-activity-detail{font-size:12px;color:#64748b;line-height:1.45;margin-top:2px}
    @media(max-width:600px){.audit-activity-timeline{padding:7px 14px 12px}.audit-activity-item{grid-template-columns:46px 18px minmax(0,1fr);gap:7px}.audit-activity-time{font-size:11px}}
    </style>
    """, unsafe_allow_html=True)

    group_labels: list[str] = []
    labels = logs["_waktu"].map(date_label)
    for label in labels:
        if label not in group_labels:
            group_labels.append(label)
    html = "<div class='audit-activity-timeline'>"
    for label in group_labels:
        group = logs[labels.eq(label)]
        html += f"<section class='audit-activity-group'><div class='audit-activity-group-title'>{escape(label)}</div>"
        for _, row in group.iterrows():
            title, detail = content_for(row)
            time_text = row["_waktu"].strftime("%H:%M") if pd.notna(row["_waktu"]) else "--:--"
            detail_html = f"<div class='audit-activity-detail'>{escape(detail)}</div>" if detail else ""
            html += f"<div class='audit-activity-item'><div class='audit-activity-time'>{time_text}</div><div class='audit-activity-icon'>{icon_for(row['Aktivitas'])}</div><div><div class='audit-activity-title'>{escape(title)}</div>{detail_html}</div></div>"
        html += "</section>"
    st.markdown(html + "</div>", unsafe_allow_html=True)


def _render_audit_processing_tab() -> None:
    """Monitoring read-only atas output pipeline Excel existing."""
    signature = _excel_source_signature()
    raw = _load_excel_daily_data(signature)
    processed = load_employee_data()
    if raw.empty or processed.empty:
        st.info("Belum terdapat data presensi yang dapat ditampilkan.")
        return

    quality = raw.attrs.get("etl_quality", {})
    source_info = raw.attrs.get("source_info", {})
    source_name = str(source_info.get("name") or active_source_name())
    connection_status = str(source_info.get("connection_status") or "Tersedia")
    file_count = int(quality.get("files_processed", len(signature) if source_name.startswith("Excel") else 1))
    records_input = int(quality.get("records_input", len(raw)))
    records_valid = int(quality.get("records_valid", len(raw)))
    duplicates = int(quality.get("duplicates", 0))
    # ``Presensi_Tidak_Lengkap`` di parser dibentuk dari marker TK pada salah
    # satu pasangan masuk/pulang. Karena TK adalah status presensi valid,
    # indikator tersebut tidak menentukan status kualitas pada card ini.
    # Duplikat adalah satu-satunya issue kualitas nyata yang sudah tersedia
    # pada metadata ETL existing; tidak ada rule validasi baru yang dibuat.
    real_validation_issue_count = duplicates
    employee_count = int(processed["NIP"].astype(str).nunique())
    opd_count = int(processed["Unit Kerja"].nunique())
    years = sorted(pd.to_numeric(processed["Tahun"], errors="coerce").dropna().astype(int).unique().tolist())
    available = [month for month in ANALYTICS_MONTHS if month in set(processed["Bulan"].astype(str))]
    if available and years:
        period_text = f"{available[0]}–{available[-1]} {years[0]}" if len(years) == 1 else f"{available[0]} {years[0]}–{available[-1]} {years[-1]}"
    else:
        period_text = "Belum tersedia"

    st.markdown("""
    <style>
    .audit-processing-page{color:#0f172a}.processing-intro{font-size:13px;color:#64748b;margin:2px 0 16px}.processing-section{margin-top:24px}.processing-title{font-size:18px;font-weight:700;color:#173b63}.processing-subtitle{font-size:12px;color:#64748b;margin:3px 0 12px}
    .processing-kpi-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.processing-kpi-card,.processing-source-card,.processing-stepper,.processing-history,.processing-validation,.processing-preview{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:16px 18px;box-shadow:0 3px 12px rgba(15,23,42,.035)}
    .processing-kpi-card{transition:transform .18s ease,box-shadow .18s ease}.processing-kpi-card:hover{transform:translateY(-1px);box-shadow:0 5px 16px rgba(15,23,42,.05)}.processing-kpi-value{font-size:30px;font-weight:750;color:#173b63}.processing-kpi-value.warn{color:#b45309}.processing-kpi-label{font-size:12px;color:#64748b;margin-top:5px}.processing-kpi-note{font-size:11px;color:#64748b;margin-top:7px;line-height:1.35}
    .processing-source-grid{display:grid;grid-template-columns:150px 1fr;gap:9px 16px;font-size:13px}.processing-source-label{color:#64748b}.processing-source-value{font-weight:650;color:#173b63}.processing-ready{display:inline-block;background:#ecfdf5;color:#047857;border-radius:999px;padding:3px 8px;font-size:11px;font-weight:700}
    .processing-step{display:grid;grid-template-columns:24px minmax(0,1fr) auto;gap:10px;padding:10px 0;border-bottom:1px solid #f1f5f9}.processing-step:last-child{border-bottom:0}.processing-step-icon{width:20px;height:20px;border-radius:50%;background:#ecfdf5;color:#047857;text-align:center;font-size:12px;line-height:20px}.processing-step-icon.warn{background:#fffbeb;color:#a16207}.processing-step-title{font-size:13px;font-weight:700;color:#173b63}.processing-step-detail{font-size:12px;color:#64748b;margin-top:2px}.processing-badge{font-size:11px;font-weight:700;color:#047857}.processing-badge.warn{color:#a16207}
    .processing-issue-row{display:grid;grid-template-columns:180px 1fr 55px;gap:10px;align-items:center;margin:11px 0;font-size:12px;color:#475569}.processing-issue-track{height:7px;background:#eef2f7;border-radius:99px;overflow:hidden}.processing-issue-fill{height:100%;background:#f59e0b;border-radius:99px}.processing-dataset-meta{font-size:12px;color:#64748b;background:#f8fafc;border-radius:10px;padding:10px 12px;margin-bottom:12px}
    @media(max-width:800px){.processing-kpi-grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:520px){.processing-source-grid{grid-template-columns:1fr}.processing-step{grid-template-columns:24px 1fr}.processing-badge{grid-column:2}.processing-issue-row{grid-template-columns:130px 1fr 42px}}
    </style><div class='audit-processing-page'><div class='processing-intro'>Data yang digunakan dashboard telah melalui proses pembacaan, pembersihan, standardisasi, dan validasi existing.</div></div>
    """, unsafe_allow_html=True)

    process_has_issues = real_validation_issue_count > 0
    process_icon = "&#9888;" if process_has_issues else "&#10003;"
    process_label = "Perlu Pemeriksaan" if process_has_issues else "Proses Selesai"
    process_note = f"{real_validation_issue_count:,} data memerlukan pemeriksaan.".replace(",", ".") if process_has_issues else "Data siap digunakan untuk analisis."
    numeric_kpis = [(file_count, "File/Data Sumber"), (records_input, "Record Diproses"), (records_valid, "Data Siap Dianalisis")]
    kpi_html = "".join(f"<div class='processing-kpi-card'><div class='processing-kpi-value'>{value:,}</div><div class='processing-kpi-label'>{label}</div></div>" for value, label in numeric_kpis).replace(",", ".")
    kpi_html += f"<div class='processing-kpi-card'><div class='processing-kpi-value {'warn' if process_has_issues else ''}'>{process_icon}</div><div class='processing-kpi-label'>{process_label}</div><div class='processing-kpi-note'>{process_note}</div></div>"
    st.markdown("<section class='processing-section'><div class='processing-title'>Status Data</div><div class='processing-subtitle'>Ringkasan metadata pipeline yang tersedia.</div><div class='processing-kpi-grid'>" + kpi_html + "</div></section>", unsafe_allow_html=True)

    st.markdown(f"<section class='processing-section'><div class='processing-title'>Sumber Data Aktif</div><div class='processing-subtitle'>Sumber yang digunakan dataframe dashboard saat ini.</div><div class='processing-source-card'><div class='processing-source-grid'><span class='processing-source-label'>Sumber aktif</span><span class='processing-source-value'>{escape(source_name)}</span><span class='processing-source-label'>Status koneksi</span><span class='processing-source-value'>{escape(connection_status)}</span><span class='processing-source-label'>Periode</span><span class='processing-source-value'>{escape(period_text)}</span><span class='processing-source-label'>Jumlah OPD</span><span class='processing-source-value'>{opd_count}</span><span class='processing-source-label'>Pegawai Unik</span><span class='processing-source-value'>{employee_count:,}</span><span class='processing-source-label'>Status</span><span><span class='processing-ready'>✓ Siap Dianalisis</span></span><span class='processing-source-label'>Terakhir diproses</span><span class='processing-source-value'>Waktu pemrosesan terakhir belum tersedia.</span></div></div></section>".replace(",", "."), unsafe_allow_html=True)

    validation_status = "Perlu Pemeriksaan" if process_has_issues else "Selesai"
    validation_class = "warn" if process_has_issues else ""
    steps = [
        ("Sumber Data", f"{file_count} file Excel tersedia pada sumber aplikasi.", "Selesai", ""),
        ("Extract", f"{records_input:,} record dibaca oleh parser existing.".replace(",", "."), "Selesai", ""),
        ("Cleaning", f"{duplicates:,} duplikat NIP + tanggal ditangani oleh proses existing.".replace(",", "."), "Selesai", ""),
        ("Standardisasi", "Jenis pegawai serta format dashboard dibentuk oleh helper existing.", "Selesai", ""),
        ("Validasi", f"{records_valid:,} record siap dianalisis • {real_validation_issue_count:,} isu kualitas data nyata.".replace(",", "."), validation_status, validation_class),
        ("Integrasi", "Data agregat siap digunakan oleh dashboard dan halaman analisis.", "Selesai", ""),
    ]
    step_html = "".join(f"<div class='processing-step'><span class='processing-step-icon {css}'>{'!' if css else '✓'}</span><div><div class='processing-step-title'>{title}</div><div class='processing-step-detail'>{detail}</div></div><span class='processing-badge {css}'>{status}</span></div>" for title, detail, status, css in steps)
    st.markdown(f"<section class='processing-section'><div class='processing-title'>Alur Pemrosesan Data</div><div class='processing-subtitle'>Tahapan yang benar-benar digunakan oleh pipeline aplikasi.</div><div class='processing-stepper'>{step_html}</div></section>", unsafe_allow_html=True)

    source_summary = f"{file_count} file Excel Rekap Presensi" if source_name.startswith("Excel") else source_name
    st.markdown(f"<section class='processing-section'><div class='processing-title'>Ringkasan Pemrosesan Terkini</div><div class='processing-subtitle'>Riwayat bertanggal belum tersedia pada metadata source.</div><div class='processing-history'><div class='processing-source-grid'><span class='processing-source-label'>Sumber</span><span class='processing-source-value'>{escape(source_summary)}</span><span class='processing-source-label'>Periode data</span><span class='processing-source-value'>{escape(period_text)}</span><span class='processing-source-label'>Record masuk</span><span class='processing-source-value'>{records_input:,}</span><span class='processing-source-label'>Record hasil proses</span><span class='processing-source-value'>{records_valid:,}</span><span class='processing-source-label'>Status</span><span><span class='processing-ready'>Berhasil</span></span></div></div></section>".replace(",", "."), unsafe_allow_html=True)

    st.markdown("<section class='processing-section'><div class='processing-title'>Validasi &amp; Isu Data</div><div class='processing-subtitle'>Kategori memakai hasil validasi existing, tanpa rule tambahan.</div>", unsafe_allow_html=True)
    issues = [("Duplikat NIP + tanggal", duplicates)]
    issues = [(label, value) for label, value in issues if value > 0]
    if not issues:
        st.markdown("<div class='processing-validation'>✓ Tidak ditemukan data yang memerlukan pemeriksaan pada dataset aktif.</div>", unsafe_allow_html=True)
    else:
        max_issue = max(value for _, value in issues)
        issue_html = "".join(f"<div class='processing-issue-row'><span>{label}</span><div class='processing-issue-track'><div class='processing-issue-fill' style='width:{value/max_issue*100:.1f}%'></div></div><strong>{value:,}</strong></div>" for label, value in issues).replace(",", ".")
        st.markdown(f"<div class='processing-validation'>{issue_html}</div>", unsafe_allow_html=True)
        issue_filter = st.selectbox("Jenis Masalah", ["Semua Masalah"] + [label for label, _ in issues], key="audit_processing_issue")
        with st.expander("Lihat detail data yang perlu diperiksa", expanded=False):
            if issue_filter in ("Semua Masalah", "Duplikat NIP + tanggal"):
                st.caption("Baris duplikat telah dihapus oleh pipeline; detail baris tidak disimpan pada dataframe final.")

    preview_limit = st.selectbox("Tampilkan baris", [10, 25, 50], index=1, key="audit_processing_preview_limit")
    search = st.text_input("Cari NIP/Nama", key="audit_processing_preview_search", placeholder="Ketik NIP atau nama pegawai")
    preview = processed
    if search:
        query = search.lower()
        preview = preview[preview["NIP"].astype(str).str.lower().str.contains(query) | preview["Nama Pegawai"].astype(str).str.lower().str.contains(query)]
    preview_columns = [column for column in ["NIP", "Nama Pegawai", "Unit Kerja", "Jenis Pegawai", "Tahun", "Bulan", "Terlambat", "Cuti"] if column in preview.columns]
    preview_table = preview.loc[:, preview_columns].head(preview_limit).copy()
    preview_table["NIP"] = preview_table["NIP"].astype(str)
    st.markdown(f"<section class='processing-section'><div class='processing-title'>Preview Data Hasil Proses</div><div class='processing-subtitle'>Sampel terbatas dari dataframe agregat yang digunakan dashboard.</div><div class='processing-preview'><div class='processing-dataset-meta'>Dataset siap dianalisis • {records_valid:,} record valid • {employee_count:,} pegawai unik • {opd_count} OPD • {escape(period_text)}</div></div></section>".replace(",", "."), unsafe_allow_html=True)
    st.dataframe(preview_table, hide_index=True, use_container_width=True)


def show_audit_trail_page() -> None:
    """Audit Trail read-only dengan aktivitas dan monitoring pemrosesan."""
    inject_dashboard_css()
    st.markdown("<h1 style='display:block!important;visibility:visible!important;opacity:1!important;font-size:28px!important;font-weight:700!important;color:#173b63!important;text-align:left!important;margin:0 0 5px!important'>Audit Trail</h1>", unsafe_allow_html=True)
    st.markdown("<div style='color:#64748b;font-size:14px;margin:0 0 14px'>Riwayat aktivitas serta transparansi pemrosesan data presensi.</div>", unsafe_allow_html=True)
    activity_tab, processing_tab = st.tabs(["Aktivitas Sistem", "Pemrosesan Data"])
    with activity_tab:
        _render_simple_audit_activity_timeline()
    with processing_tab:
        _render_audit_processing_tab()


def show_early_warning_page() -> None:
    inject_dashboard_css()
    data = load_employee_data()
    month_order = available_months(data)

    with st.sidebar:
        st.markdown("# 🛡️ EWS Kehadiran")
        st.caption("Prioritas dan tindak lanjut risiko")
        st.divider()
        st.markdown("#### Filter Risiko")
        chosen_month = st.selectbox("Periode bulan", ["Semua Bulan"] + month_order, key="risk_page_month")
        units = ["Semua Unit"] + sorted(data["Unit Kerja"].unique().tolist())
        chosen_unit = st.selectbox("Unit kerja", units, key="risk_page_unit")
        search_name = st.text_input("Cari nama atau NIP", placeholder="Ketik untuk mencari", key="risk_page_search")
        st.divider()
        st.caption(f"Login sebagai: {st.session_state.username}")
        st.button("Keluar", on_click=logout, use_container_width=True, key="risk_page_logout")

    risk_data = data.copy()
    if chosen_month != "Semua Bulan":
        risk_data = risk_data[risk_data["Bulan"] == chosen_month]
    if chosen_unit != "Semua Unit":
        risk_data = risk_data[risk_data["Unit Kerja"] == chosen_unit]
    if search_name:
        query = search_name.lower()
        risk_data = risk_data[
            risk_data["Nama Pegawai"].str.lower().str.contains(query)
            | risk_data["NIP"].str.lower().str.contains(query)
        ]
    if chosen_month == "Semua Bulan":
        risk_data = aggregate_risk_by_employee(risk_data)

    risk_data = risk_data.copy()
    risk_data["Risk Score"] = risk_data.apply(
        lambda row: calculate_risk_score(row["TK"], row["Terlambat"]), axis=1
    )
    risk_data["Status"] = risk_data["Risk Score"].apply(risk_status)
    risk_data["Penyebab"] = risk_data.apply(
        lambda row: warning_indicators(row["TK"], row["Terlambat"]), axis=1
    )
    risk_data["Prioritas"] = risk_data["Status"].map(
        {"Kritis": "P1", "Tinggi": "P2", "Waspada": "P3", "Normal": "P4"}
    )
    risk_data["Rekomendasi"] = risk_data["Status"].apply(risk_recommendation)

    previous_average_score = None
    if chosen_month in month_order and month_order.index(chosen_month) > 0:
        previous_month = month_order[month_order.index(chosen_month) - 1]
        previous_data = data[data["Bulan"] == previous_month].copy()
        if chosen_unit != "Semua Unit":
            previous_data = previous_data[previous_data["Unit Kerja"] == chosen_unit]
        if search_name:
            query = search_name.lower()
            previous_data = previous_data[
                previous_data["Nama Pegawai"].str.lower().str.contains(query)
                | previous_data["NIP"].str.lower().str.contains(query)
            ]
        if not previous_data.empty:
            previous_average_score = previous_data.apply(
                lambda row: calculate_risk_score(row["TK"], row["Terlambat"]), axis=1
            ).mean()

    status_order = ["Normal", "Waspada", "Tinggi", "Kritis"]
    status_icons = {"Normal": "🟢", "Waspada": "🟡", "Tinggi": "🟠", "Kritis": "🔴"}
    status_counts = risk_data["Status"].value_counts().reindex(status_order, fill_value=0)
    total_risk_employees = len(risk_data)
    high_critical = int(status_counts["Tinggi"] + status_counts["Kritis"])
    overall_status = "Normal" if high_critical == 0 else "Perlu Perhatian" if high_critical <= max(1, total_risk_employees // 3) else "Risiko Tinggi"
    overall_icon = "🟢" if overall_status == "Normal" else "🟠" if overall_status == "Perlu Perhatian" else "🔴"
    st.markdown("<h1 class='ews-page-title'>EARLY WARNING SYSTEM</h1>", unsafe_allow_html=True)
    st.markdown("<div class='ews-page-subtitle'>Monitoring risiko presensi dan deteksi dini pegawai</div>", unsafe_allow_html=True)
    display_unit = "Semua OPD" if chosen_unit == "Semua Unit" else chosen_unit
    period_label = "Januari–Maret 2026" if chosen_month == "Semua Bulan" else f"{chosen_month} 2026"
    st.markdown(f"<div class='ews-filter-row'><span>Periode: <strong>{period_label}</strong></span><span class='ews-filter-chip'>OPD: <strong>{display_unit}</strong></span></div>", unsafe_allow_html=True)
    header_risk_source = data.copy()
    if chosen_unit != "Semua Unit":
        header_risk_source = header_risk_source[header_risk_source["Unit Kerja"] == chosen_unit]
    if search_name:
        header_query = search_name.lower()
        header_risk_source = header_risk_source[
            header_risk_source["Nama Pegawai"].str.lower().str.contains(header_query)
            | header_risk_source["NIP"].str.lower().str.contains(header_query)
        ]
    header_risk_source["Risk Score"] = header_risk_source.apply(lambda row: calculate_risk_score(row["TK"], row["Terlambat"]), axis=1)
    header_risk_counts = header_risk_source.assign(Berisiko=header_risk_source["Risk Score"] >= 25).groupby("Bulan")["Berisiko"].sum().reindex(month_order).fillna(0)
    header_previous = int(header_risk_counts.iloc[0]) if len(header_risk_counts) else 0
    header_current = int(header_risk_counts.iloc[-1]) if len(header_risk_counts) else 0
    header_change = header_current - header_previous
    header_change_pct = (header_change / header_previous * 100) if header_previous else 0
    header_icon = "🟢" if header_change <= 0 else "🔴"
    header_label = "Risiko Menurun" if header_change <= 0 else "Risiko Meningkat"
    header_arrow = "↓" if header_change <= 0 else "↑"
    if chosen_month != "Semua Bulan":
        st.markdown(f"<div class='ews-header-risk'><div class='risk-label'>{header_icon} {header_label}</div><div class='risk-count'>{header_previous} → {header_current} pegawai</div><div class='risk-change'>{header_arrow} {abs(header_change)} pegawai ({header_change_pct:+.1f}%) dibanding {month_order[0]}</div></div>", unsafe_allow_html=True)
    warning_total = int((risk_data["Status"] != "Normal").sum())
    st.markdown(f"<div class='ews-status-banner'><div class='status-label'>STATUS EWS</div><div class='status-value'>{overall_icon} {overall_status.upper()}</div><div class='status-detail'>{high_critical} dari {total_risk_employees} pegawai memerlukan perhatian<br><span style='opacity:.85'>{warning_total} warning aktif • {warning_total} belum ditindaklanjuti</span></div></div>", unsafe_allow_html=True)

    st.markdown("<div class='section-title'>Risk Summary</div>", unsafe_allow_html=True)
    summary_columns = st.columns(4)
    summary_styles = {"Normal": "green", "Waspada": "yellow", "Tinggi": "orange", "Kritis": "red"}
    for column, status_name in zip(summary_columns, status_order):
        column.markdown(
            f"<div class='ews-risk-summary'><div class='ews-card {summary_styles[status_name]}'><div class='label'>{status_icons[status_name]} {status_name.upper()}</div><div class='number'>{int(status_counts[status_name])}</div><div class='detail'>{(int(status_counts[status_name]) / total_risk_employees * 100) if total_risk_employees else 0:.1f}% pegawai</div></div></div>",
            unsafe_allow_html=True,
        )

    trend_col = st.container()
    with trend_col:
        st.markdown("<div class='section-title'>Tren Risiko</div>", unsafe_allow_html=True)
        risk_trend_source = data.copy()
        if chosen_unit != "Semua Unit":
            risk_trend_source = risk_trend_source[risk_trend_source["Unit Kerja"] == chosen_unit]
        if search_name:
            trend_query = search_name.lower()
            risk_trend_source = risk_trend_source[
                risk_trend_source["Nama Pegawai"].str.lower().str.contains(trend_query)
                | risk_trend_source["NIP"].str.lower().str.contains(trend_query)
            ]
        risk_trend_source["Risk Score"] = risk_trend_source.apply(lambda row: calculate_risk_score(row["TK"], row["Terlambat"]), axis=1)
        visible_months = month_order[: month_order.index(chosen_month) + 1] if chosen_month in month_order else month_order
        risk_counts = risk_trend_source.assign(Berisiko=risk_trend_source["Risk Score"] >= 25).groupby("Bulan")["Berisiko"].sum().reindex(visible_months).fillna(0)
        current_risk = int(risk_counts.iloc[-1]) if len(risk_counts) else 0
        previous_risk = int(risk_counts.iloc[-2]) if len(risk_counts) > 1 else current_risk
        risk_change = current_risk - previous_risk
        risk_change_pct = (risk_change / previous_risk * 100) if previous_risk else 0
        risk_peak_month = str(risk_counts.idxmax()) if len(risk_counts) else "-"
        improving = risk_change <= 0
        risk_direction = "↓" if improving else "↑"
        risk_status_text = "MEMBAIK" if improving else "MEMBURUK"
        risk_status_color = "#047857" if improving else "#dc2626"
        st.markdown(
            f"<div class='trend-summary-grid'><div class='trend-summary-card'><div class='trend-summary-label'>Risiko Saat Ini</div><div class='trend-summary-value'>{current_risk} pegawai</div></div><div class='trend-summary-card'><div class='trend-summary-label'>Perubahan</div><div class='trend-summary-value {'positive' if improving else 'negative'}'>{risk_direction} {abs(risk_change)} pegawai</div><div class='trend-summary-label'>{risk_change_pct:+.1f}%</div></div></div><div style='color:{risk_status_color};font-weight:800;font-size:.82rem;margin:.1rem 0 .35rem;'>{'🟢' if improving else '🔴'} {risk_status_text}</div><div class='note'>Perkembangan jumlah pegawai berisiko</div>",
            unsafe_allow_html=True,
        )
        risk_chart_data = pd.DataFrame({"Bulan": visible_months, "Risiko": risk_counts.values, "Batas Waspada": [30] * len(visible_months), "Batas Kritis": [40] * len(visible_months)})
        risk_base = alt.Chart(risk_chart_data).encode(
            x=alt.X("Bulan:N", sort=month_order, title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("Risiko:Q", scale=alt.Scale(domain=[0, max(45, int(risk_counts.max()) + 5)]), title="Pegawai berisiko"),
            tooltip=[alt.Tooltip("Bulan:N", title="Periode"), alt.Tooltip("Risiko:Q", title="Pegawai berisiko")],
        )
        risk_line = risk_base.mark_line(color="#173b63", strokeWidth=3, point=alt.OverlayMarkDef(size=65, filled=True, fill="#173b63", stroke="#ffffff", strokeWidth=2))
        critical_rule = alt.Chart(risk_chart_data).mark_rule(color="#e11d48", strokeDash=[5, 4]).encode(y="Batas Kritis:Q")
        warning_rule = alt.Chart(risk_chart_data).mark_rule(color="#f97316", strokeDash=[5, 4]).encode(y="Batas Waspada:Q")
        st.altair_chart((risk_line + critical_rule + warning_rule).properties(height=240), use_container_width=True)
        st.caption("Batas Waspada: 30 pegawai  •  Batas Kritis: 40 pegawai")

    st.markdown("<div class='section-title'>⚠ Peringatan Prioritas</div>", unsafe_allow_html=True)
    top_risk = risk_data[risk_data["Status"] != "Normal"].sort_values(
        ["Risk Score", "Nama Pegawai"], ascending=[False, True]
    ).head(10).copy()
    top_risk["Status"] = top_risk["Status"].map(lambda value: f"{status_icons[value]} {value}")
    if top_risk.empty:
        st.success("Tidak ada pegawai yang memerlukan perhatian pada filter ini.")
    else:
        st.dataframe(
            top_risk[["Nama Pegawai", "Unit Kerja", "Risk Score", "Penyebab", "Status"]],
            hide_index=True,
            use_container_width=True,
            column_config={
                "Risk Score": st.column_config.NumberColumn("Risk Score", format="%d"),
                "Penyebab": st.column_config.TextColumn("Penyebab", width="large"),
                "Status": st.column_config.TextColumn("Status", width="medium"),
            },
        )

    if not top_risk.empty:
        priority_record = risk_data.sort_values("Risk Score", ascending=False).iloc[0]
        priority_icon = status_icons[priority_record["Status"]]
        late_period_detail = ""
        verification_records = st.session_state.get("verification_records", {})
        verified = priority_record["NIP"] in verification_records
        handling_status = "🔵 Terverifikasi" if verified else "⚪ Belum Diverifikasi"
        verification_detail = "<br><strong>Diverifikasi:</strong><br>22 Agustus 2026 • 19:20 WIB<br>oleh Admin" if verified else ""
        verification_detail = f"{late_period_detail}{verification_detail}"
        st.markdown(
            f"<div class='ews-status-banner' style='background:#fff;border-left-color:#e11d48;border-color:#fecdd3'><div class='status-value' style='color:#9f1239'>{priority_record['Nama Pegawai']} &nbsp; {priority_icon} {priority_record['Status'].upper()}</div><div class='status-detail' style='color:#475569'>TK {int(priority_record['TK'])} hari &bull; Terlambat {int(priority_record['Terlambat'])} kali<br><strong>Jumlah hari TK {priority_record['Bulan']}:</strong> {int(priority_record['TK'])} hari<br><br><strong>Status Penanganan:</strong><br>{handling_status}{verification_detail}</div></div>",
            unsafe_allow_html=True,
        )
        _legacy_priority_markup = """
            f"<div class='ews-status-banner' style='background:#fff;border-left-color:#e11d48;border-color:#fecdd3'><div class='status-value' style='color:#9f1239'>{priority_record['Nama Pegawai']} &nbsp; {priority_icon} {priority_record['Status'].upper()}</div><div class='status-detail' style='color:#475569'>TK {int(priority_record['TK'])} hari • Terlambat {int(priority_record['Terlambat'])} kali<br><br><strong>Status Penanganan:</strong><br>{handling_status}{verification_detail}</div></div>",
            unsafe_allow_html=True,
        )
        """
        if verified:
            st.markdown("<span class='verified-badge'>✓ TERVERIFIKASI</span>", unsafe_allow_html=True)
        elif st.button("✓ Verifikasi Data", use_container_width=True, key="priority_verify_action"):
            st.session_state["verification_nip"] = priority_record["NIP"]
            st.rerun()
        if st.button("📋 Tindak Lanjut", use_container_width=True, key="priority_process_action"):
            st.session_state["navigate_to_page"] = "Action Center"
            st.rerun()
        if verified:
            priority_attendance = max(priority_record["Hari Kerja"] - priority_record["TK"] - priority_record["Cuti"], 0) / priority_record["Hari Kerja"] * 100 if priority_record["Hari Kerja"] else 0
            st.markdown(f"**{priority_record['Unit Kerja']}**  \nTK {int(priority_record['TK'])} hari • Terlambat {int(priority_record['Terlambat'])}× • Kehadiran {priority_attendance:.1f}%")
            st.info("**Rekomendasi**\n\nWarning telah diverifikasi. Lanjutkan proses tindak lanjut.")
            st.markdown("<div class='section-title'>Riwayat Penanganan</div>", unsafe_allow_html=True)
            st.markdown(
                "<div class='ews-status-banner' style='background:#fff;border-left-color:#2563eb;border-color:#dbeafe'>22 Agustus 2026 • 19:20 WIB<br><strong>✓ Warning diverifikasi oleh Admin</strong></div>",
                unsafe_allow_html=True,
            )

    analysis_left, analysis_right = st.columns(2)
    if False:
        st.markdown("<div class='section-title'>Risk Score & Warning Level</div>", unsafe_allow_html=True)
        average_score = risk_data["Risk Score"].mean() if not risk_data.empty else 0
        maximum_score = risk_data["Risk Score"].max() if not risk_data.empty else 0
        st.metric("Risk Score rata-rata", f"{average_score:.0f}/100")
        st.progress(min(average_score / 100, 1.0))
        st.caption(f"Warning level tertinggi: {status_icons[risk_status(maximum_score)]} {risk_status(maximum_score)}")
        if previous_average_score is not None:
            risk_delta = average_score - previous_average_score
            risk_arrow = "↑" if risk_delta >= 0 else "↓"
            st.metric("Rata-rata Risk Score", f"{risk_arrow} {abs(risk_delta):.0f} poin", "dibanding bulan sebelumnya")
    with analysis_right:
        with st.expander("Rule Engine — Info & Detail"):
            st.dataframe(
            pd.DataFrame(
                {
                    "Rule": ["TK", "Keterlambatan", "Risk Score"],
                    "Kondisi": ["≥ 3 hari", "≥ 8 kali", "TK × 8 + terlambat × 2"],
                    "Dampak": ["Warning", "Warning", "Status risiko"],
                }
            ),
                hide_index=True,
                use_container_width=True,
            )

    anomaly_count = int(((risk_data["TK"] >= 6) | (risk_data["Terlambat"] >= 12)).sum())
    pattern_summary = pd.DataFrame(
        {
            "Pattern Warning": ["TK ≥ 3 hari", "Keterlambatan ≥ 8 kali", "Anomali ekstrem"],
            "Jumlah Pegawai": [
                int((risk_data["TK"] >= 3).sum()),
                int((risk_data["Terlambat"] >= 8).sum()),
                anomaly_count,
            ],
        }
    )
    with st.container():
        st.markdown("<div class='section-title'>Indikator Warning</div>", unsafe_allow_html=True)
        anomaly_cols = st.columns(3)
        anomaly_labels = ["TK ≥ 3 hari", "Terlambat ≥ 8 kali", "Anomali ekstrem"]
        for anomaly_col, anomaly_label, anomaly_value in zip(anomaly_cols, anomaly_labels, pattern_summary["Jumlah Pegawai"].tolist()):
            anomaly_col.metric(anomaly_label, int(anomaly_value), "pegawai")
        st.caption("Anomali ekstrem ditandai oleh TK ≥ 6 hari atau keterlambatan ≥ 12 kali.")
    st.markdown("", unsafe_allow_html=True)
    if top_risk.empty or not st.session_state.get("verification_nip"):
        st.empty()
    else:
        top_record = risk_data.sort_values("Risk Score", ascending=False).iloc[0]
        icon = status_icons[top_record["Status"]]
        st.warning(
            f"{icon} **{top_record['Status'].upper()} WARNING**\n\n"
            f"Pegawai **{top_record['Nama Pegawai']}** mengalami {int(top_record['TK'])} TK "
            f"dan {int(top_record['Terlambat'])} keterlambatan dalam periode berjalan.\n\n"
            f"**Rekomendasi:** {top_record['Rekomendasi']}"
        )
        action_1, action_2 = st.columns(2)
        if action_1.button("✓ Verifikasi Data", use_container_width=True, key="risk_verify_action"):
            st.session_state["verification_nip"] = top_record["NIP"]
            st.rerun()
        if action_2.button("📋 Tindak Lanjut", use_container_width=True, key="risk_process_action"):
            st.session_state["navigate_to_page"] = "Action Center"
            st.rerun()

        detail_nip = st.session_state.get("warning_detail_nip")
        if detail_nip:
            detail_rows = risk_data[risk_data["NIP"] == detail_nip]
            if not detail_rows.empty:
                detail = detail_rows.iloc[0]
                detail_attendance = (
                    max(detail["Hari Kerja"] - detail["TK"] - detail["Cuti"], 0) / detail["Hari Kerja"] * 100
                    if detail["Hari Kerja"]
                    else 0
                )
                monday_tk = int(detail["TK"]) if detail["Hari Dominan"] == "Senin" else 0
                late_change_text = None
                if chosen_month in month_order and month_order.index(chosen_month) > 0:
                    previous_month = month_order[month_order.index(chosen_month) - 1]
                    previous_rows = data[(data["NIP"] == detail_nip) & (data["Bulan"] == previous_month)]
                    if not previous_rows.empty:
                        previous_late = int(previous_rows["Terlambat"].iloc[0])
                        if previous_late:
                            late_change_text = f"{(detail['Terlambat'] - previous_late) / previous_late * 100:+.0f}% dari bulan sebelumnya"
                        elif detail["Terlambat"]:
                            late_change_text = "meningkat dari 0 menjadi ada kejadian"
                with st.expander("Lihat Detail Analisis", expanded=True):
                    st.markdown(
                        f"**{detail['Status'].upper()} — {detail['Nama Pegawai']}**\n\n"
                        f"Risk Score: **{calculate_risk_score(detail['TK'], detail['Terlambat'])}/100**\n\n"
                        f"- TK: **{int(detail['TK'])} hari**\n"
                        f"- Keterlambatan: **{int(detail['Terlambat'])} kali**\n"
                        f"- Kehadiran: **{detail_attendance:.1f}%**\n"
                        f"- Pola dominan: **TK pada hari {detail['Hari Dominan']}**\n\n"
                        f"**Rekomendasi:** Verifikasi data presensi dan lakukan tindak lanjut sesuai ketentuan."
                    )

        verification_nip = st.session_state.get("verification_nip")
        if verification_nip:
            verification_rows = risk_data[risk_data["NIP"] == verification_nip]
            if not verification_rows.empty:
                verification_record = verification_rows.iloc[0]
                st.markdown("<div class='section-title'>Verifikasi Warning</div>", unsafe_allow_html=True)
                st.markdown(
                    f"**Pegawai**\n\n### {verification_record['Nama Pegawai']}\n\n**Data yang diverifikasi:**\n\n- TK: **{int(verification_record['TK'])} hari**\n- Keterlambatan: **{int(verification_record['Terlambat'])} kali**\n- Status Risiko: **{verification_record['Status'].upper()}**"
                )
                st.warning(
                    f"🔴 TK: **{int(verification_record['TK'])} hari** — "
                    "silakan cocokkan dengan dokumen cuti atau bukti kehadiran."
                )
                verification_status = "Data Valid"
                verification_note = st.text_area(
                    "Catatan Verifikasi",
                    value="Data telah diperiksa dan sesuai",
                    key="verification_note",
                )
                verify_save, verify_close = st.columns(2)
                if verify_save.button("✓ Konfirmasi Verifikasi", use_container_width=True, key="save_verification"):
                    st.session_state.setdefault("verification_records", {})[verification_nip] = {
                        "Status": verification_status,
                        "Catatan": verification_note,
                    }
                    st.success("Verifikasi data berhasil dikonfirmasi.")
                if verify_close.button("Batal", use_container_width=True, key="close_verification"):
                    st.session_state.pop("verification_nip", None)
                    st.rerun()


def show_dashboard() -> None:
    inject_dashboard_css()
    data = load_employee_data()
    month_order = available_months(data)

    with st.sidebar:
        st.markdown("# 🛡️ EWS Kehadiran")
        st.caption("Ringkasan eksekutif presensi pegawai")
        st.divider()
        st.markdown("#### Filter Dashboard")
        chosen_month = st.selectbox("Periode bulan", ["Semua Bulan"] + month_order, key="executive_month")
        units = ["Semua Unit"] + sorted(data["Unit Kerja"].unique().tolist())
        chosen_unit = st.selectbox("Unit kerja", units, key="executive_unit")
        search_name = st.text_input("Cari nama atau NIP", placeholder="Ketik untuk mencari", key="executive_search")
        st.divider()
        st.caption(f"Login sebagai: {st.session_state.username}")
        st.button("Keluar", on_click=logout, use_container_width=True, key="executive_logout")

    def filter_data(source: pd.DataFrame, month: str | None = None) -> pd.DataFrame:
        result = source.copy()
        if month and month != "Semua Bulan":
            result = result[result["Bulan"] == month]
        if chosen_unit != "Semua Unit":
            result = result[result["Unit Kerja"] == chosen_unit]
        if search_name:
            query = search_name.lower()
            result = result[
                result["Nama Pegawai"].str.lower().str.contains(query)
                | result["NIP"].str.lower().str.contains(query)
            ]
        return result

    def attendance_rate(source: pd.DataFrame) -> float:
        workdays = source["Hari Kerja"].sum()
        if not workdays:
            return 0.0
        present_days = (source["Hari Kerja"] - source["TK"] - source["Cuti"]).clip(lower=0).sum()
        return present_days / workdays * 100

    filtered = filter_data(data, chosen_month)
    total_employees = filtered["NIP"].nunique()
    attendance = attendance_rate(filtered)
    total_tk_tb = int(filtered["TK"].sum())
    total_late = int(filtered["Terlambat"].sum())
    total_at_risk = int((filtered["TK"] >= 3).sum())
    risk_scores = filtered.apply(lambda row: calculate_risk_score(row["TK"], row["Terlambat"]), axis=1)
    total_critical = int((risk_scores >= 75).sum())
    month_index = month_order.index(chosen_month) if chosen_month in month_order else None
    previous_month = month_order[month_index - 1] if month_index else None
    previous_attendance = attendance_rate(filter_data(data, previous_month)) if previous_month else None
    attendance_delta = attendance - previous_attendance if previous_attendance is not None else None

    trend_source = filter_data(data)
    attendance_trend = pd.Series(
        {month: attendance_rate(trend_source[trend_source["Bulan"] == month]) for month in month_order},
        name="Persentase Kehadiran",
    )
    trend_df = pd.DataFrame({"Bulan": month_order, "Kehadiran": [attendance_trend.get(month, 0.0) for month in month_order]})
    trend_df["Kondisi"] = trend_df["Kehadiran"].apply(
        lambda value: "Sangat Baik" if value >= 95 else "Baik" if value >= 90 else "Perlu Perhatian" if value >= 80 else "Risiko"
    )
    opd_summary = filtered.groupby("Unit Kerja").agg(
        Pegawai=("NIP", "nunique"),
        Hari_Kerja=("Hari Kerja", "sum"),
        TK_TB=("TK", "sum"),
        Keterlambatan=("Terlambat", "sum"),
    )
    opd_summary["Kehadiran"] = (
        (opd_summary["Hari_Kerja"] - opd_summary["TK_TB"]) / opd_summary["Hari_Kerja"] * 100
    ).clip(lower=0)
    opd_summary["Pegawai Berisiko"] = filtered.groupby("Unit Kerja")["TK"].apply(lambda values: (values >= 3).sum())
    opd_summary = opd_summary.sort_values("Kehadiran", ascending=False).reset_index()

    attendance_target = 95.0
    attendance_gap = attendance - attendance_target
    gap_style = "positive" if attendance_gap >= 0 else "negative"
    formatted_gap = f"{attendance_gap:+.1f}".replace(".", ",")
    formatted_attendance = f"{attendance:.1f}".replace(".", ",")
    formatted_total_employees = f"{total_employees:,}".replace(",", ".")

    st.markdown("<h1 class='dashboard-title' style='display:block!important;visibility:visible!important;color:#102a43!important;text-align:center!important;font-size:2.35rem!important;font-weight:800!important;'>Executive Dashboard</h1>", unsafe_allow_html=True)
    st.markdown("<div class='hero-subtitle'>Ringkasan kondisi presensi pegawai</div>", unsafe_allow_html=True)
    display_unit = "Semua OPD" if chosen_unit == "Semua Unit" else chosen_unit
    st.markdown(
        f"<div class='hero-filters'><div class='hero-filter-chip'>Periode: <strong>{chosen_month}</strong></div><div class='hero-filter-chip'>OPD: <strong>{display_unit}</strong></div></div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div class='hero-kpi-grid'><div class='hero-kpi-card'><div class='hero-kpi-icon'>👥</div><div><div class='hero-kpi-value'>{formatted_total_employees}</div><div class='hero-kpi-label'>Pegawai</div></div></div><div class='hero-kpi-card'><div class='hero-kpi-icon'>✅</div><div><div class='hero-kpi-value'>{attendance:.1f}%</div><div class='hero-kpi-label'>Kehadiran</div></div></div><div class='hero-kpi-card warning'><div class='hero-kpi-icon'>⚠️</div><div><div class='hero-kpi-value'>{total_at_risk}</div><div class='hero-kpi-label'>Warning</div></div></div><div class='hero-kpi-card critical'><div class='hero-kpi-icon'>🔴</div><div><div class='hero-kpi-value'>{total_critical}</div><div class='hero-kpi-label'>Critical</div></div></div></div>",
        unsafe_allow_html=True,
    )

    compare_col, change_col = st.columns(2)
    with compare_col:
        st.markdown("<div class='section-title'>Target vs Realisasi</div>", unsafe_allow_html=True)
        st.metric("Target Kehadiran", f"{attendance_target:.0f}%", f"Realisasi {attendance:.1f}%")
        st.progress(min(attendance / attendance_target, 1.0))
    with change_col:
        st.markdown("<div class='section-title'>Perubahan Dibanding Bulan Sebelumnya</div>", unsafe_allow_html=True)
        if attendance_delta is None:
            st.info("Data bulan sebelumnya belum tersedia untuk periode Januari.")
        else:
            direction = "naik" if attendance_delta >= 0 else "turun"
            arrow = "↑" if attendance_delta >= 0 else "↓"
            st.metric(
                "Perubahan Kehadiran",
                f"{arrow} {abs(attendance_delta):.1f}%",
                f"{direction} dibanding {previous_month}",
            )

    st.markdown("<div class='section-title'>Tren Kehadiran</div>", unsafe_allow_html=True)
    st.caption("Perkembangan tingkat kehadiran pegawai selama periode berjalan.")
    trend_margin_left, trend_center, trend_margin_right = st.columns([0.1, 0.8, 0.1])
    with trend_center:
        latest_value = float(trend_df.iloc[-1]["Kehadiran"])
        prior_value = float(trend_df.iloc[-2]["Kehadiran"]) if len(trend_df) > 1 else latest_value
        trend_change = latest_value - prior_value
        trend_status = "Sangat Baik" if latest_value >= 95 else "Baik" if latest_value >= 90 else "Perlu Perhatian" if latest_value >= 80 else "Risiko"
        trend_status_icon = "🟢" if latest_value >= 95 else "🔵" if latest_value >= 90 else "🟠" if latest_value >= 80 else "🔴"
        change_class = "positive" if trend_change >= 0 else "negative"
        change_arrow = "↑" if trend_change >= 0 else "↓"
        st.markdown(
            f"<div class='trend-summary-grid'><div class='trend-summary-card'><div class='trend-summary-label'>Kehadiran {month_order[-1]}</div><div class='trend-summary-value'>{latest_value:.1f}%</div></div><div class='trend-summary-card'><div class='trend-summary-label'>Perubahan</div><div class='trend-summary-value {change_class}'>{change_arrow} {abs(trend_change):.1f}%</div></div><div class='trend-summary-card'><div class='trend-summary-label'>Status</div><div class='trend-summary-value warning'>{trend_status_icon} {trend_status}</div></div></div>",
            unsafe_allow_html=True,
        )
        chart_data = trend_df.set_index("Bulan")[["Kehadiran"]].reindex(month_order)
        tooltip_data = chart_data.reset_index()
        trend_chart = (
            alt.Chart(tooltip_data)
            .mark_line(point=alt.OverlayMarkDef(size=85, filled=True, fill="#2563eb", stroke="#ffffff", strokeWidth=2), color="#2563eb", strokeWidth=3)
            .encode(
                x=alt.X("Bulan:N", sort=month_order, title=None, axis=alt.Axis(labelAngle=0)),
                y=alt.Y("Kehadiran:Q", scale=alt.Scale(domain=[70, 100]), title="Kehadiran (%)"),
                tooltip=[alt.Tooltip("Bulan:N", title="Periode"), alt.Tooltip("Kehadiran:Q", title="Persentase Kehadiran", format=".1f")],
            )
            .properties(height=270)
        )
        st.altair_chart(trend_chart, use_container_width=True)
        st.markdown(
            f"<div class='trend-target-note'>Target 95% <span>•</span> Rata-rata periode: <strong>{trend_df['Kehadiran'].mean():.1f}%</strong></div>",
            unsafe_allow_html=True,
        )
        st.markdown("<div class='legend'><span>🟢 ≥95% Sangat Baik</span><span>🔵 90–94,99% Baik</span><span>🟠 80–89,99% Perlu Perhatian</span><span>🔴 &lt;80% Risiko</span></div>", unsafe_allow_html=True)

    st.markdown("<div class='section-title'>Ringkasan Kondisi OPD</div>", unsafe_allow_html=True)
    st.dataframe(
        opd_summary[["Unit Kerja", "Pegawai", "Kehadiran", "TK_TB", "Keterlambatan", "Pegawai Berisiko"]],
        hide_index=True,
        use_container_width=True,
        column_config={
            "Kehadiran": st.column_config.NumberColumn("Kehadiran", format="%.1f%%"),
            "TK_TB": st.column_config.NumberColumn("TK", format="%d hari"),
            "Keterlambatan": st.column_config.NumberColumn("Terlambat", format="%d kali"),
        },
    )

    st.markdown("<div class='section-title'>Target dan KPI</div>", unsafe_allow_html=True)
    st.markdown(
        f"""
        <div class="target-kpi-card">
            <p>Target Kehadiran: <strong>&ge; {attendance_target:.0f}%</strong></p>
            <p>Realisasi: <strong>{formatted_attendance}%</strong></p>
            <p class="kpi-gap">Gap: {formatted_gap}% <span class="kpi-dot {gap_style}"></span></p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def prepare_executive_summary(source: pd.DataFrame, trend_source: pd.DataFrame, month_order: list[str]) -> dict:
    """Satu agregasi eksekutif berbasis NIP unik; dipakai ulang semua visual dashboard."""
    def attendance_rate(frame: pd.DataFrame) -> float:
        workdays = frame["Hari Kerja"].sum()
        present = (frame["Hari Kerja"] - frame["TK"] - frame["Cuti"]).clip(lower=0).sum()
        return float(present / workdays * 100) if workdays else 0.0

    risk = prepare_ews_data(source)
    risk["Level Visual"] = risk["Status EWS"].map({"Normal": "Normal", "Waspada": "Perhatian", "Tinggi": "Risiko Tinggi", "Kritis": "Kritis"})
    warning = risk[risk["Status EWS"].isin(["Tinggi", "Kritis"])].copy()
    trend_rows = []
    for month in month_order:
        month_data = trend_source[trend_source["Bulan"] == month]
        month_risk = prepare_ews_data(month_data)
        trend_rows.append({"Bulan": month, "Kehadiran": attendance_rate(month_data), "Pegawai Warning": int(month_risk[month_risk["Status EWS"].isin(["Tinggi", "Kritis"])]["NIP"].nunique())})
    trend = pd.DataFrame(trend_rows)
    totals = risk.groupby("Unit Kerja")["NIP"].nunique().rename("Total Pegawai")
    risks = warning.groupby("Unit Kerja")["NIP"].nunique().rename("Pegawai Warning")
    opd_risk = pd.concat([totals, risks], axis=1).fillna(0)
    opd_risk["Persentase Warning"] = opd_risk["Pegawai Warning"] / opd_risk["Total Pegawai"].replace(0, 1) * 100
    return {"risk": risk, "warning": warning, "attendance": attendance_rate(source), "trend": trend, "opd_risk": opd_risk.sort_values("Persentase Warning", ascending=False)}


def show_early_warning_page_focus() -> None:
    """EWS khusus deteksi, perubahan, dan prioritas risiko per NIP."""
    inject_dashboard_css()
    data = load_employee_data()
    if data.empty:
        st.info("Data presensi belum tersedia.")
        return

    level_map = {"Normal": "Normal", "Waspada": "Perhatian", "Tinggi": "Risiko Tinggi", "Kritis": "Kritis"}
    level_order = {"Kritis": 0, "Tinggi": 1, "Waspada": 2, "Normal": 3}
    month_rank = {name: index for index, name in enumerate(MONTH_NAMES.values())}
    st.markdown("""
    <style>
    .block-container:has(.ews-page){max-width:1500px;margin:auto;background:#f5f7fb}.ews-page{color:#173b63}
    .ews-header-title{font-size:28px;font-weight:700;line-height:1.2}.ews-header-subtitle,.ews-section-subtitle{font-size:13px;color:#64748b;margin-top:5px}
    .ews-filter-bar{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:10px 14px 2px;margin:18px 0 7px;box-shadow:0 3px 12px rgba(15,23,42,.035)}
    .ews-meta-strip{font-size:12px;color:#64748b;padding:7px 2px 16px}.ews-section{margin-top:24px}.ews-section-title{font-size:18px;font-weight:700;color:#173b63}.ews-section-subtitle{margin-bottom:12px}
    .ews-kpi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.ews-kpi-card,.ews-card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:16px 18px;box-shadow:0 3px 12px rgba(15,23,42,.035)}
    .ews-kpi-accent{width:32px;height:3px;border-radius:8px;margin-bottom:10px}.ews-kpi-value{font-size:26px;font-weight:750;color:#173b63}.ews-kpi-value.small{font-size:14px;line-height:32px}.ews-kpi-label{font-size:12px;color:#64748b}
    .ews-priority-item{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:14px 18px;margin:8px 0;transition:transform .18s ease,box-shadow .18s ease}.ews-priority-item:hover{transform:translateY(-1px);box-shadow:0 5px 16px rgba(15,23,42,.05)}
    .ews-priority-top{display:grid;grid-template-columns:42px minmax(0,1fr) auto;gap:10px}.ews-rank{font-size:15px;font-weight:750;color:#94a3b8}.ews-name{font-size:15px;font-weight:700;color:#173b63}.ews-opd{font-size:12px;color:#64748b;margin-top:2px}
    .ews-risk-badge,.ews-reason-chip,.ews-risk-change{display:inline-block;border-radius:999px;font-size:11px;font-weight:700;padding:4px 8px}.ews-risk-badge{background:#fff7ed;color:#c2410c}.ews-risk-badge.critical{background:#fff1f2;color:#be123c}.ews-risk-badge.watch{background:#fffbeb;color:#a16207}
    .ews-priority-bottom{display:flex;justify-content:space-between;gap:12px;align-items:end;margin:10px 0 0 52px}.ews-reason-chip{background:#eff6ff;color:#1d4ed8;margin:0 5px 5px 0}.ews-score{font-size:13px;color:#475569}.ews-score strong{color:#173b63}.ews-risk-change{margin-left:7px}.ews-risk-change.up{background:#fff7ed;color:#c2410c}.ews-risk-change.down{background:#ecfdf5;color:#047857}.ews-risk-change.flat{background:#f1f5f9;color:#475569}.ews-detail{font-size:12px;color:#2563eb;font-weight:700}
    .ews-dual{display:grid;grid-template-columns:1fr 1fr;gap:16px}.ews-summary-row{display:grid;grid-template-columns:120px 1fr 32px;gap:10px;align-items:center;margin:12px 0;font-size:12px;color:#475569}.ews-bar{height:7px;background:#eef2f7;border-radius:99px;overflow:hidden}.ews-bar-fill{height:100%;border-radius:99px;background:#64748b}.ews-bar-fill.up{background:#f97316}.ews-bar-fill.down{background:#10b981}.ews-bar-fill.new{background:#f59e0b}
    .ews-priority-matrix{width:100%;border-collapse:separate;border-spacing:6px;font-size:12px}.ews-priority-matrix th{color:#64748b;padding:7px}.ews-priority-matrix td{background:#f8fafc;border:1px solid #e2e8f0;border-radius:9px;text-align:center;padding:12px;color:#173b63;font-weight:700}.ews-priority-matrix td.hot{background:#fff1f2;border-color:#fecdd3;color:#be123c}
    .ews-insight{background:#fff;border-left:3px solid #2563eb;border-radius:10px;padding:10px 14px;margin:7px 0;font-size:13px;color:#334155}.ews-disclaimer{font-size:11px;line-height:1.55;color:#64748b;border-top:1px solid #e2e8f0;margin-top:24px;padding-top:12px}
    @media(max-width:900px){.ews-kpi-grid{grid-template-columns:repeat(2,1fr)}.ews-dual{grid-template-columns:1fr}}@media(max-width:640px){.ews-header-title{font-size:26px}.ews-priority-top{grid-template-columns:32px 1fr}.ews-risk-badge{grid-column:2}.ews-priority-bottom{margin-left:42px;align-items:flex-start;flex-direction:column}}
    </style><div class="ews-page"></div>
    """, unsafe_allow_html=True)
    st.markdown(
        "<h1 class='ews-page-title' style='display:block!important;visibility:visible!important;opacity:1!important;color:#173b63!important;font-size:28px!important;font-weight:700!important;line-height:1.2!important;text-align:left!important;margin:0 0 6px!important'>Early Warning System</h1>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<div class='ews-header-subtitle' style='display:block!important;visibility:visible!important;opacity:1!important;color:#64748b!important;font-size:14px!important;margin:0!important'>Identifikasi pegawai yang memerlukan perhatian berdasarkan pola dan perubahan risiko presensi.</div>",
        unsafe_allow_html=True,
    )

    years = sorted(pd.to_numeric(data["Tahun"], errors="coerce").dropna().astype(int).unique().tolist())
    default_year = years[-1] if years else "Semua Tahun"
    employee_types = [v for v in ["PNS", "PPPK", "Belum Diketahui"] if v in set(data.get("Jenis Pegawai", pd.Series(dtype=str)).dropna())]
    units = sorted(data["Unit Kerja"].dropna().astype(str).unique().tolist())

    def reset_filters() -> None:
        defaults = {"ews_filter_year": default_year, "ews_filter_month": "Semua Bulan", "ews_filter_opd": "Semua OPD", "ews_filter_employee_type": "Semua Jenis Pegawai", "ews_filter_risk": "Semua Level", "ews_filter_trend": "Semua Trend"}
        for key, value in defaults.items(): st.session_state[key] = value

    st.markdown("<div class='ews-filter-bar'>", unsafe_allow_html=True)
    cols = st.columns([1, 1, 1.5, 1.35, 1.2, .65])
    with cols[0]: year = st.selectbox("Tahun", years or ["Semua Tahun"], index=len(years) - 1 if years else 0, key="ews_filter_year")
    year_source = data if year == "Semua Tahun" else data[pd.to_numeric(data["Tahun"], errors="coerce") == int(year)]
    months = sorted(year_source["Bulan"].dropna().astype(str).unique(), key=lambda v: month_rank.get(v, 99))
    with cols[1]: month = st.selectbox("Bulan", ["Semua Bulan"] + months, key="ews_filter_month")
    with cols[2]: opd = st.selectbox("OPD", ["Semua OPD"] + units, key="ews_filter_opd")
    with cols[3]: employee_type = st.selectbox("Jenis Pegawai", ["Semua Jenis Pegawai"] + employee_types, key="ews_filter_employee_type")
    with cols[4]: risk_level = st.selectbox("Level Risiko", ["Semua Level", "Perhatian", "Risiko Tinggi", "Kritis", "Normal"], key="ews_filter_risk")
    with cols[5]: st.button("Reset", on_click=reset_filters, use_container_width=True, key="ews_filter_reset")
    st.markdown("</div>", unsafe_allow_html=True)

    scoped = year_source.copy()
    if opd != "Semua OPD": scoped = scoped[scoped["Unit Kerja"].astype(str) == opd]
    if employee_type != "Semua Jenis Pegawai": scoped = scoped[scoped["Jenis Pegawai"].astype(str) == employee_type]
    current_month = month if month in months else (months[-1] if months else None)
    previous_month = months[months.index(current_month) - 1] if current_month in months and months.index(current_month) > 0 else None
    current = prepare_ews_data(scoped[scoped["Bulan"] == current_month]) if current_month else pd.DataFrame()
    previous = prepare_ews_data(scoped[scoped["Bulan"] == previous_month]) if previous_month else pd.DataFrame()
    if current.empty:
        st.info("Tidak terdapat data EWS pada kombinasi filter yang dipilih.")
        return

    previous_index = previous.drop_duplicates("NIP").set_index("NIP") if not previous.empty else pd.DataFrame()
    ews = current.drop_duplicates("NIP").copy()
    ews["Level Tampilan"] = ews["Status EWS"].map(level_map)
    ews["Previous Score"] = ews["NIP"].map(previous_index["Risk Score"]) if "Risk Score" in previous_index else pd.NA
    ews["Previous Status"] = ews["NIP"].map(previous_index["Status EWS"]) if "Status EWS" in previous_index else pd.NA
    ews["Delta Risiko"] = ews["Risk Score"] - pd.to_numeric(ews["Previous Score"], errors="coerce")
    comparable = ews["Previous Score"].notna()
    ews["Warning Baru"] = comparable & ews["Status EWS"].ne("Normal") & ews["Previous Status"].eq("Normal")
    ews["Tren"] = "Data pembanding belum tersedia"
    ews.loc[comparable & ews["Delta Risiko"].gt(0), "Tren"] = "Memburuk"
    ews.loc[comparable & ews["Delta Risiko"].eq(0), "Tren"] = "Stabil"
    ews.loc[comparable & ews["Delta Risiko"].lt(0), "Tren"] = "Membaik"
    if risk_level != "Semua Level": ews = ews[ews["Level Tampilan"] == risk_level]

    type_label = "PNS & PPPK" if employee_type == "Semua Jenis Pegawai" else employee_type
    comparison_label = f" • dibanding {previous_month} {year}" if previous_month else ""
    st.markdown(f"<div class='ews-meta-strip'>{escape(str(current_month))} {year} • {escape(opd)} • {escape(type_label)} • {ews['NIP'].nunique()} Pegawai{comparison_label}</div>", unsafe_allow_html=True)
    at_risk = ews[ews["Status EWS"] != "Normal"].copy()
    comparison_available = previous_month is not None
    new_count = int(ews.loc[ews["Warning Baru"], "NIP"].nunique())
    rising_count = int(ews.loc[ews["Tren"] == "Memburuk", "NIP"].nunique())
    falling_count = int(ews.loc[ews["Tren"] == "Membaik", "NIP"].nunique())
    kpis = [(at_risk["NIP"].nunique(), "Pegawai Berisiko", "#2563eb"), (new_count if comparison_available else "Belum tersedia", "Warning Baru", "#f59e0b"), (rising_count if comparison_available else "Belum tersedia", "Risiko Meningkat", "#f97316"), (falling_count if comparison_available else "Belum tersedia", "Risiko Menurun", "#10b981")]
    st.markdown("<section class='ews-section'><div class='ews-section-title'>Ringkasan Early Warning</div><div class='ews-section-subtitle'>Sinyal risiko unik pegawai pada periode aktif.</div><div class='ews-kpi-grid'>" + "".join(f"<div class='ews-kpi-card'><div class='ews-kpi-accent' style='background:{color}'></div><div class='ews-kpi-value{' small' if isinstance(value, str) else ''}'>{value}</div><div class='ews-kpi-label'>{label}</div></div>" for value, label, color in kpis) + "</div></section>", unsafe_allow_html=True)

    st.markdown("<section class='ews-section'><div class='ews-section-title'>Prioritas Perhatian</div><div class='ews-section-subtitle'>Pegawai yang memerlukan perhatian berdasarkan tingkat dan perubahan risiko.</div></section>", unsafe_allow_html=True)
    trend_filter = st.selectbox("Quick filter arah risiko", ["Semua Trend", "Memburuk", "Stabil", "Membaik", "Warning Baru"], key="ews_filter_trend")
    priorities = at_risk.copy()
    if trend_filter == "Warning Baru": priorities = priorities[priorities["Warning Baru"]]
    elif trend_filter != "Semua Trend": priorities = priorities[priorities["Tren"] == trend_filter]
    priorities["_level"] = priorities["Status EWS"].map(level_order)
    priorities["_worsening"] = priorities["Tren"].ne("Memburuk")
    priorities = priorities.sort_values(["_level", "_worsening", "Risk Score"], ascending=[True, True, False])
    limit = st.radio("Jumlah daftar", [5, 10], horizontal=True, key="ews_priority_limit", label_visibility="collapsed")
    if priorities.empty: st.markdown("<div class='ews-card'>✅ Tidak terdapat pegawai yang memerlukan perhatian pada kombinasi filter yang dipilih.</div>", unsafe_allow_html=True)
    for rank, (_, employee) in enumerate(priorities.head(limit).iterrows(), 1):
        reasons = [part.strip().title() for part in str(employee["Penyebab Utama"]).split("•") if part.strip()][:2]
        chips = "".join(f"<span class='ews-reason-chip'>{escape(reason)}</span>" for reason in reasons)
        trend, delta = str(employee["Tren"]), employee["Delta Risiko"]
        if trend == "Memburuk": change, change_class = f"↑ +{int(delta)} · Memburuk", "up"
        elif trend == "Membaik": change, change_class = f"↓ {int(delta)} · Membaik", "down"
        elif trend == "Stabil": change, change_class = "→ Stabil", "flat"
        else: change, change_class = "Data pembanding belum tersedia", "flat"
        if bool(employee["Warning Baru"]): change = "🆕 Warning Baru · " + change
        badge_class = "critical" if employee["Status EWS"] == "Kritis" else "watch" if employee["Status EWS"] == "Waspada" else ""
        st.markdown(f"<article class='ews-priority-item'><div class='ews-priority-top'><div class='ews-rank'>{rank:02d}</div><div><div class='ews-name'>{escape(str(employee['Nama Pegawai']))}</div><div class='ews-opd'>{escape(str(employee['Unit Kerja']))}</div></div><span class='ews-risk-badge {badge_class}'>{escape(str(employee['Level Tampilan']).upper())}</span></div><div class='ews-priority-bottom'><div><div>{chips}</div><div class='ews-score'>Risk Score <strong>{int(employee['Risk Score'])}/100</strong><span class='ews-risk-change {change_class}'>{escape(change)}</span></div></div><span class='ews-detail'>Lihat Detail →</span></div></article>", unsafe_allow_html=True)
        if st.button(f"Buka detail {employee['Nama Pegawai']}", key=f"ews_detail_{employee['NIP']}"):
            st.session_state["employee_detail_opd"] = str(employee["Unit Kerja"])
            st.session_state["employee_detail_employee"] = f"{employee['Nama Pegawai']} — {employee['NIP']}"
            st.session_state["navigate_to_page"] = "Detail Pegawai"
            st.rerun()

    status_counts = ews["Tren"].value_counts()
    status_rows = [("Memburuk", rising_count, "up"), ("Stabil", int(status_counts.get("Stabil", 0)), ""), ("Membaik", falling_count, "down"), ("Warning Baru", new_count, "new")]
    cause_source = at_risk.drop_duplicates("NIP").copy()
    cause_source["Kategori Penyebab"] = cause_source.apply(lambda r: "TK + Keterlambatan" if r["TK"] > 0 and r["Terlambat"] > 0 else "TK Berulang" if r["TK"] > 0 else "Keterlambatan Meningkat" if r["Terlambat"] > 0 else "Perlu Pemantauan", axis=1)
    causes = cause_source.groupby("Kategori Penyebab")["NIP"].nunique().sort_values(ascending=False).head(3)
    max_status = max([v for _, v, _ in status_rows] + [1]); max_cause = max([int(v) for v in causes.tolist()] + [1])
    status_html = "".join(f"<div class='ews-summary-row'><span>{label}</span><div class='ews-bar'><div class='ews-bar-fill {css}' style='width:{value/max_status*100:.1f}%'></div></div><strong>{value}</strong></div>" for label, value, css in status_rows)
    cause_html = "".join(f"<div class='ews-summary-row'><span>{escape(str(label))}</span><div class='ews-bar'><div class='ews-bar-fill' style='width:{int(value)/max_cause*100:.1f}%'></div></div><strong>{int(value)}</strong></div>" for label, value in causes.items()) or "<div class='ews-section-subtitle'>Tidak terdapat penyebab warning.</div>"
    change_html = status_html if comparison_available else "<div class='ews-section-subtitle'>Data periode sebelumnya belum tersedia untuk analisis perubahan risiko.</div>"
    st.markdown(f"<div class='ews-dual ews-section'><section class='ews-card'><div class='ews-section-title'>Perubahan Status Risiko</div><div class='ews-section-subtitle'>Arah perubahan kondisi EWS antarperiode.</div>{change_html}</section><section class='ews-card ews-cause-card'><div class='ews-section-title'>Penyebab Dominan Warning</div><div class='ews-section-subtitle'>Pola utama yang memicu warning pada pegawai berisiko.</div>{cause_html}</section></div>", unsafe_allow_html=True)

    matrix_source = at_risk[at_risk["Tren"].isin(["Stabil", "Memburuk", "Membaik"])]
    matrix = matrix_source.groupby(["Level Tampilan", "Tren"])["NIP"].nunique()
    levels = [level for level in ["Kritis", "Risiko Tinggi", "Perhatian"] if level in set(at_risk["Level Tampilan"])]
    high_worsening = int(matrix.get(("Kritis", "Memburuk"), 0) + matrix.get(("Risiko Tinggi", "Memburuk"), 0))
    rows = []
    for level in levels:
        cells = []
        for trend in ["Stabil", "Memburuk", "Membaik"]:
            value = int(matrix.get((level, trend), 0)); hot = "hot" if level in ["Kritis", "Risiko Tinggi"] and trend == "Memburuk" and value else ""
            cells.append(f"<td class='{hot}'>{value}</td>")
        rows.append(f"<tr><th>{escape(level)}</th>{''.join(cells)}</tr>")
    matrix_note = f"⚠️ Prioritas utama: {high_worsening} pegawai Risiko Tinggi/Kritis mengalami peningkatan risiko." if high_worsening else "Tidak ada pegawai Risiko Tinggi/Kritis dengan tren memburuk pada filter aktif."
    st.markdown(f"<section class='ews-card ews-section'><div class='ews-section-title'>Matrix Prioritas</div><div class='ews-section-subtitle'>Kombinasi level risiko dan arah perubahan.</div><table class='ews-priority-matrix'><thead><tr><th>Level Risiko</th><th>Stabil</th><th>Memburuk</th><th>Membaik</th></tr></thead><tbody>{''.join(rows)}</tbody></table><div class='ews-section-subtitle'>{matrix_note}</div></section>", unsafe_allow_html=True)

    insights = []
    if high_worsening: insights.append(f"⚠️ {high_worsening} pegawai Risiko Tinggi/Kritis menunjukkan tren memburuk.")
    if comparison_available and new_count: insights.append(f"🆕 {new_count} pegawai baru masuk kategori risiko pada periode ini.")
    if not causes.empty: insights.append(f"🔎 {causes.index[0]} merupakan penyebab warning dominan ({int(causes.iloc[0])} pegawai).")
    if not insights: insights.append("✅ Tidak ada sinyal prioritas tambahan pada kombinasi filter aktif.")
    st.markdown("<section class='ews-section'><div class='ews-section-title'>Insight EWS</div><div class='ews-section-subtitle'>Ringkasan keputusan berdasarkan sinyal prioritas.</div>" + "".join(f"<div class='ews-insight'>{escape(item)}</div>" for item in insights[:3]) + "</section>", unsafe_allow_html=True)
    st.markdown("<div class='ews-disclaimer'><strong>Catatan:</strong> Early Warning System merupakan indikator awal untuk membantu proses monitoring presensi. Hasil sistem tidak secara otomatis menetapkan keputusan disiplin dan tetap memerlukan proses verifikasi/tindak lanjut sesuai kewenangan.</div>", unsafe_allow_html=True)


def show_dashboard_focus() -> None:
    """Ringkasan pimpinan; detail operasional tetap berada di EWS dan Action Center."""
    inject_dashboard_css()
    data = load_employee_data()
    months = available_months(data)
    with st.sidebar:
        st.markdown("#### Filter Dashboard")
        selected_month = st.selectbox("Periode", ["Semua Bulan"] + months, key="executive_focus_month")
        selected_unit = st.selectbox("OPD", ["Semua OPD"] + sorted(data["Unit Kerja"].dropna().unique().tolist()), key="executive_focus_unit")
        search = st.text_input("Cari nama atau NIP", key="executive_focus_search")

    scoped = data.copy()
    if selected_month != "Semua Bulan":
        scoped = scoped[scoped["Bulan"] == selected_month]
    if selected_unit != "Semua OPD":
        scoped = scoped[scoped["Unit Kerja"] == selected_unit]
    if search:
        query = search.lower()
        scoped = scoped[scoped["Nama Pegawai"].astype(str).str.lower().str.contains(query, na=False) | scoped["NIP"].astype(str).str.contains(query, na=False)]
    if scoped.empty:
        st.info("Tidak terdapat data untuk filter yang dipilih.")
        return
    trend_source = data.copy()
    if selected_unit != "Semua OPD":
        trend_source = trend_source[trend_source["Unit Kerja"] == selected_unit]
    if search:
        trend_source = trend_source[trend_source["Nama Pegawai"].astype(str).str.lower().str.contains(query, na=False) | trend_source["NIP"].astype(str).str.contains(query, na=False)]
    summary = prepare_executive_summary(scoped, trend_source, months)
    risk = summary["risk"]
    warnings = summary["warning"]
    trend = summary["trend"]
    current_month = selected_month if selected_month in months else (months[-1] if months else "-")
    current_index = months.index(current_month) if current_month in months else 0
    previous_month = months[current_index - 1] if current_index > 0 else None
    current_trend = trend[trend["Bulan"] == current_month]
    previous_trend = trend[trend["Bulan"] == previous_month] if previous_month else pd.DataFrame()
    attendance_delta = None
    warning_delta = None
    if not current_trend.empty and not previous_trend.empty:
        attendance_delta = float(current_trend.iloc[0]["Kehadiran"] - previous_trend.iloc[0]["Kehadiran"])
        warning_delta = int(current_trend.iloc[0]["Pegawai Warning"] - previous_trend.iloc[0]["Pegawai Warning"])
    try:
        latest_mtime = max(item[1] for item in _excel_source_signature())
        updated_at = pd.Timestamp(latest_mtime, unit="ns").strftime("%d %B %Y, %H:%M WIB")
    except (ValueError, OSError):
        updated_at = pd.Timestamp.now().strftime("%d %B %Y, %H:%M WIB")
    period_label = selected_month if selected_month != "Semua Bulan" else (f"{months[0]} – {months[-1]}" if months else "-")
    total_employees = int(scoped["NIP"].nunique())
    total_opd = int(scoped["Unit Kerja"].nunique())
    attendance = summary["attendance"]
    warning_count = int(warnings["NIP"].nunique())
    new_warning_count = max(warning_delta or 0, 0)
    fmt_num = lambda value: f"{int(value):,}".replace(",", ".")
    fmt_pct = lambda value, digits=1: f"{value:.{digits}f}".replace(".", ",") + "%"

    st.markdown(
        """<style>
        .block-container:has(.executive-dashboard-anchor) {max-width:1500px;margin-left:auto;margin-right:auto;}
        .block-container:has(.executive-dashboard-anchor) [data-testid="stMetric"] {min-height:110px;background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:16px 18px;box-shadow:0 3px 10px rgba(15,23,42,.035);}
        .block-container:has(.executive-dashboard-anchor) [data-testid="stMetricValue"] {font-size:30px;font-weight:750;color:#173b63;}
        .block-container:has(.executive-dashboard-anchor) [data-testid="stMetricLabel"] {font-size:13px;font-weight:600;color:#475569;}
        .executive-insight-card {background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:16px 18px;box-shadow:0 3px 10px rgba(15,23,42,.035);margin-top:18px;}.executive-insight-title {color:#173b63;font-size:18px;font-weight:750;margin-bottom:8px;}.executive-insight-item {color:#475569;font-size:14px;padding:3px 0;}
        .block-container:has(.executive-dashboard-anchor) [data-testid="stButton"] button {width:auto!important;min-height:38px!important;padding:8px 14px!important;border-radius:9px!important;}
        .executive-cta-row {display:flex;justify-content:flex-end;gap:10px;margin-top:14px;}.executive-cta-row button {width:auto!important;min-height:38px!important;padding:8px 14px!important;border-radius:9px!important;}
        @media(max-width:640px){.executive-cta-row{justify-content:flex-start;flex-wrap:wrap;}}
        </style><div class='executive-dashboard-anchor'></div>""",
        unsafe_allow_html=True,
    )
    st.markdown("<h1 class='dashboard-title'>Executive Dashboard</h1>", unsafe_allow_html=True)
    st.markdown("<div class='hero-subtitle'>Ringkasan kondisi presensi dan indikator risiko pegawai.</div>", unsafe_allow_html=True)
    metadata_years = sorted(scoped["Tahun"].dropna().astype(str).unique().tolist()) if "Tahun" in scoped.columns else []
    metadata_period = f"{period_label} {metadata_years[0]}" if len(metadata_years) == 1 else period_label
    st.markdown(
        f"""
        <style>
        .executive-meta-strip {{display:flex;align-items:stretch;flex-wrap:wrap;gap:0;background:#f8fafc;border:1px solid #e5eaf0;border-radius:12px;padding:10px 14px;margin:10px 0 18px;}}
        .executive-meta-item {{display:flex;align-items:center;gap:7px;flex:1 1 190px;padding:2px 18px;color:#334155;font-size:14px;line-height:1.35;}}
        .executive-meta-item:first-child {{padding-left:2px;}} .executive-meta-item + .executive-meta-item {{border-left:1px solid #e2e8f0;}}
        .executive-meta-icon {{color:#3b82f6;font-size:15px;}} .executive-meta-value {{font-weight:600;color:#334155;}}
        @media (max-width:640px) {{.executive-meta-strip {{display:block;}} .executive-meta-item {{padding:5px 2px;}} .executive-meta-item + .executive-meta-item {{border-left:0;}}}}
        </style>
        <div class="executive-meta-strip">
            <div class="executive-meta-item"><span class="executive-meta-icon">📅</span><span class="executive-meta-value">{metadata_period}</span></div>
            <div class="executive-meta-item"><span class="executive-meta-icon">🏢</span><span class="executive-meta-value">{total_opd} OPD Terpantau</span></div>
            <div class="executive-meta-item"><span class="executive-meta-icon">👥</span><span class="executive-meta-value">{fmt_num(total_employees)} Pegawai Dianalisis</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption(f"Data diperbarui: {updated_at} (berdasarkan waktu perubahan berkas sumber)")

    attendance_delta_text = "Data periode sebelumnya belum tersedia" if attendance_delta is None else f"{'↑' if attendance_delta >= 0 else '↓'} {fmt_pct(abs(attendance_delta))} dibanding {previous_month}"
    warning_delta_text = "Data periode sebelumnya belum tersedia" if warning_delta is None else f"{'↑' if warning_delta > 0 else '↓' if warning_delta < 0 else '→'} {abs(warning_delta)} pegawai dibanding {previous_month}"
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("👥 Pegawai Dianalisis", fmt_num(total_employees))
    k2.metric("✅ Tingkat Kehadiran", fmt_pct(attendance), attendance_delta_text)
    k3.metric("🔴 Pegawai Perlu Warning", fmt_num(warning_count), warning_delta_text)
    k4.metric("🏢 OPD Terpantau", fmt_num(total_opd))
    k5.metric("⚠️ Warning Baru", fmt_num(new_warning_count), "periode berjalan")

    distribution_col, trend_col = st.columns(2)
    with distribution_col, st.container(border=True):
        st.markdown("<div style='font-size:18px;font-weight:750;color:#102a43'>Distribusi Tingkat Risiko</div><div style='color:#64748b;font-size:13px;margin:.2rem 0 .75rem'>Komposisi pegawai berdasarkan indikator EWS</div>", unsafe_allow_html=True)
        risk_order = ["Normal", "Perhatian", "Risiko Tinggi", "Kritis"]
        distribution = risk["Level Visual"].value_counts().reindex(risk_order, fill_value=0)
        distribution_data = distribution.rename_axis("Level Risiko").reset_index(name="Pegawai")
        distribution_data = distribution_data[distribution_data["Pegawai"] > 0]
        if distribution_data.empty:
            st.caption("Belum ada data risiko pada filter ini.")
        else:
            distribution_data["Persentase"] = distribution_data["Pegawai"] / distribution_data["Pegawai"].sum() * 100
            st.altair_chart(alt.Chart(distribution_data).mark_arc(innerRadius=55).encode(
                theta=alt.Theta("Pegawai:Q"), color=alt.Color("Level Risiko:N", scale=alt.Scale(domain=risk_order, range=["#10b981", "#facc15", "#f97316", "#e11d48"])),
                tooltip=[alt.Tooltip("Level Risiko:N", title="Level"), alt.Tooltip("Pegawai:Q", title="Pegawai"), alt.Tooltip("Persentase:Q", title="Persentase", format=".1f")],
            ).properties(height=210), use_container_width=True)
            for _, row in distribution_data.iterrows():
                st.caption(f"{row['Level Risiko']}: {fmt_num(row['Pegawai'])} pegawai • {fmt_pct(row['Persentase'], 1)}")

    with trend_col, st.container(border=True):
        st.markdown("<div style='font-size:18px;font-weight:750;color:#102a43'>Tren Kondisi Presensi</div><div style='color:#64748b;font-size:13px;margin:.2rem 0 .75rem'>Kehadiran dan jumlah pegawai warning per bulan</div>", unsafe_allow_html=True)
        if len(trend) < 2:
            st.caption("Data periode sebelumnya belum tersedia untuk perbandingan.")
        else:
            base = alt.Chart(trend).encode(x=alt.X("Bulan:N", sort=months, title=None))
            attendance_line = base.mark_line(point=True, color="#2563eb").encode(
                y=alt.Y("Kehadiran:Q", title="Kehadiran (%)"),
                tooltip=[alt.Tooltip("Bulan:N", title="Bulan"), alt.Tooltip("Kehadiran:Q", title="Kehadiran", format=".1f"), alt.Tooltip("Pegawai Warning:Q", title="Pegawai warning")],
            )
            warning_line = base.mark_bar(color="#f97316", opacity=0.72).encode(
                y=alt.Y("Pegawai Warning:Q", title="Pegawai Warning"),
                tooltip=[alt.Tooltip("Bulan:N", title="Bulan"), alt.Tooltip("Kehadiran:Q", title="Kehadiran", format=".1f"), alt.Tooltip("Pegawai Warning:Q", title="Pegawai warning")],
            )
            st.altair_chart(alt.layer(warning_line, attendance_line).resolve_scale(y="independent").properties(height=250), use_container_width=True)

    opd_col, priority_col = st.columns(2)
    with opd_col, st.container(border=True):
        st.markdown("<div style='font-size:18px;font-weight:750;color:#102a43'>OPD Perlu Perhatian</div><div style='color:#64748b;font-size:13px;margin:.2rem 0 .75rem'>Berdasarkan persentase pegawai warning, bukan jumlah record</div>", unsafe_allow_html=True)
        top_opd = summary["opd_risk"].head(5)
        if top_opd.empty or top_opd["Pegawai Warning"].sum() == 0:
            st.success("✅ Tidak terdapat pegawai dengan status warning pada periode ini.")
        else:
            for rank, (opd, row) in enumerate(top_opd.iterrows(), start=1):
                st.markdown(f"<div style='display:flex;justify-content:space-between;gap:12px'><strong title='{opd}'>{rank}. {opd}</strong><span>{fmt_pct(row['Persentase Warning'], 2)}</span></div><div style='font-size:13px;color:#64748b'>{int(row['Pegawai Warning'])} dari {int(row['Total Pegawai'])} pegawai</div>", unsafe_allow_html=True)
                st.progress(min(float(row["Persentase Warning"]) / 100, 1.0))

    with priority_col, st.container(border=True):
        st.markdown(
            """
            <style>
            .executive-priority-card {background:#fff;border:1px solid #e4eaf2;border-left:4px solid #f97316;border-radius:16px;padding:18px 20px;margin:.65rem 0;box-shadow:0 4px 14px rgba(15,23,42,.05);transition:all .2s ease;}
            .executive-priority-card:hover {transform:translateY(-2px);box-shadow:0 7px 18px rgba(15,23,42,.08);}
            .priority-header {display:flex;justify-content:space-between;align-items:start;gap:12px;margin-bottom:12px;}
            .priority-title {font-size:18px;font-weight:750;color:#102a43;}.priority-subtitle {font-size:13px;color:#64748b;margin-top:2px;}
            .priority-count {font-size:12px;color:#475569;background:#f1f5f9;border-radius:999px;padding:4px 9px;white-space:nowrap;}
            .priority-employee-row {display:flex;justify-content:space-between;gap:12px;align-items:start;}.priority-employee {font-size:19px;font-weight:750;color:#102a43;line-height:1.3;}
            .priority-opd {font-size:13px;color:#64748b;margin-top:4px;line-height:1.45;}.priority-risk-badge {display:inline-block;border-radius:999px;padding:5px 9px;font-size:12px;font-weight:750;white-space:nowrap;}
            .priority-risk-kritis {background:#fff1f2;border:1px solid #fecdd3;color:#be123c;}.priority-risk-tinggi {background:#fff7ed;border:1px solid #fed7aa;color:#c2410c;}.priority-risk-waspada {background:#fefce8;border:1px solid #fde68a;color:#a16207;}
            .priority-reasons {display:flex;flex-wrap:wrap;gap:6px;margin-top:12px;}.priority-reason-chip {background:#f8fafc;border:1px solid #e2e8f0;border-radius:999px;padding:4px 9px;color:#475569;font-size:12px;}
            .priority-insight {margin-top:12px;padding:8px 10px;border-radius:9px;background:#fff8e8;color:#854d0e;font-size:13px;}
            @media (max-width:640px) {.priority-employee-row {display:block;}.priority-risk-badge {margin-top:8px;}.priority-header {display:block;}.priority-count {display:inline-block;margin-top:7px;}}
            </style>
            """,
            unsafe_allow_html=True,
        )
        priority_order = {"Kritis": 0, "Tinggi": 1, "Waspada": 2}
        priorities = warnings.copy()
        priorities["_priority"] = priorities["Status EWS"].map(priority_order)
        priorities = priorities.sort_values(["_priority", "Risk Score"], ascending=[True, False])
        if priorities.empty:
            st.success("✅ Tidak terdapat pegawai dengan status warning pada periode ini.")
        else:
            visible_priorities = priorities.head(3)
            st.markdown(f"<div class='priority-header'><div><div class='priority-title'>🚨 Prioritas Pimpinan</div><div class='priority-subtitle'>Pegawai yang memerlukan perhatian utama</div></div><span class='priority-count'>{len(priorities)} Prioritas</span></div>", unsafe_allow_html=True)
            for rank, (_, employee) in enumerate(visible_priorities.iterrows(), start=1):
                risk_class = {"Kritis": "priority-risk-kritis", "Tinggi": "priority-risk-tinggi", "Waspada": "priority-risk-waspada"}.get(employee["Status EWS"], "priority-risk-waspada")
                risk_label = employee["Level Visual"].upper()
                reason_labels = [reason.strip().title() for reason in str(get_warning_reason(employee)).split(" • ") if reason.strip()][:2]
                chips = "".join(f"<span class='priority-reason-chip'>↻ {reason}</span>" for reason in reason_labels)
                accent = "#e11d48" if employee["Status EWS"] == "Kritis" else "#f97316" if employee["Status EWS"] == "Tinggi" else "#eab308"
                st.markdown(f"<div class='executive-priority-card' style='border-left-color:{accent}'><div class='priority-employee-row'><div><div class='priority-employee'>{rank:02d} &nbsp;{employee['Nama Pegawai']}</div><div class='priority-opd'>🏢 {employee['Unit Kerja']}</div></div><span class='priority-risk-badge {risk_class}'>● {risk_label}</span></div><div class='priority-reasons'>{chips}</div><div class='priority-insight'>⚠ Risiko meningkat dan memerlukan peninjauan lebih lanjut</div></div>", unsafe_allow_html=True)
            if len(priorities) > 3:
                if st.button("Lihat Semua di EWS →", key="executive_all_priorities"):
                    st.session_state["navigate_to_page"] = "Early Warning System"
                    st.rerun()

    insights = []
    critical_count = int((risk["Status EWS"] == "Kritis").sum())
    if critical_count:
        insights.append(f"🔴 {critical_count} pegawai berada pada tingkat risiko kritis.")
    if warning_delta is not None:
        insights.append(f"{'📈' if warning_delta > 0 else '📉' if warning_delta < 0 else '➡️'} Jumlah pegawai warning {'meningkat' if warning_delta > 0 else 'menurun' if warning_delta < 0 else 'stabil'} dibanding {previous_month}.")
    if not summary["opd_risk"].empty and summary["opd_risk"]["Pegawai Warning"].sum() > 0:
        top_name, top_row = next(iter(summary["opd_risk"].iterrows()))
        insights.append(f"🏢 {top_name} memiliki persentase pegawai warning tertinggi ({fmt_pct(top_row['Persentase Warning'], 2)}).")
    if attendance_delta is not None:
        insights.append(f"{'✅' if attendance_delta >= 0 else '⚠️'} Tingkat kehadiran {'meningkat' if attendance_delta >= 0 else 'menurun'} {fmt_pct(abs(attendance_delta))} dibanding {previous_month}.")
    st.markdown("<div class='section-title'>💡 Executive Insight</div>", unsafe_allow_html=True)
    for insight in insights[:4]:
        st.markdown(insight)
    nav_ews, nav_action = st.columns(2)
    if nav_ews.button("Buka Early Warning System", key="executive_open_ews", use_container_width=True):
        st.session_state["navigate_to_page"] = "Early Warning System"
        st.rerun()
    if nav_action.button("Buka Action Center", key="executive_open_action", use_container_width=True):
        st.session_state["navigate_to_page"] = "Action Center"
        st.rerun()


def show_early_warning_page_focus_legacy() -> None:
    """Halaman EWS fokus pada warning dan prioritas, bukan rekap presensi."""
    inject_dashboard_css()
    st.markdown("""<style>
    .block-container:has(.ews-focus-anchor) {max-width:1500px;margin-left:auto;margin-right:auto;}
    .block-container:has(.ews-focus-anchor) [data-testid="stExpander"] {margin-top:-.35rem;margin-bottom:.35rem;}
    .block-container:has(.ews-focus-anchor) [data-testid="stExpander"] details {border-radius:10px;border-color:#e2e8f0;}
    </style><div class='ews-focus-anchor'></div>""", unsafe_allow_html=True)
    data = load_employee_data()
    months = available_months(data)
    if data.empty:
        st.info("Data presensi belum tersedia.")
        return

    with st.sidebar:
        st.markdown("#### Filter Early Warning")
        selected_month = st.selectbox("Periode", ["Semua Bulan"] + months, key="ews_focus_month")
        selected_unit = st.selectbox("OPD", ["Semua OPD"] + sorted(data["Unit Kerja"].dropna().unique()), key="ews_focus_unit")
        selected_level = st.selectbox("Level Risiko", ["Semua", "Kritis", "Risiko Tinggi", "Warning", "Perhatian", "Normal"], key="ews_focus_level")
        selected_problem = st.selectbox("Jenis Masalah", ["Semua", "TK", "Keterlambatan", "Kombinasi"], key="ews_focus_problem")
        search = st.text_input("Cari nama atau NIP", key="ews_focus_search")

    scoped = data.copy()
    if selected_unit != "Semua OPD":
        scoped = scoped[scoped["Unit Kerja"] == selected_unit]
    if search:
        query = search.lower()
        scoped = scoped[scoped["Nama Pegawai"].astype(str).str.lower().str.contains(query) | scoped["NIP"].astype(str).str.contains(query)]
    period_source = scoped if selected_month == "Semua Bulan" else scoped[scoped["Bulan"] == selected_month]
    ews_df = prepare_ews_data(period_source)

    level_map = {"Normal": "Normal", "Waspada": "Perhatian", "Tinggi": "Risiko Tinggi", "Kritis": "Kritis"}
    icon_map = {"Normal": "🟢", "Waspada": "🟡", "Tinggi": "🟠", "Kritis": "🔴"}
    ews_df["Level Tampilan"] = ews_df["Status EWS"].map(level_map)
    if selected_level != "Semua":
        ews_df = ews_df[ews_df["Level Tampilan"] == selected_level]
    if selected_problem == "TK":
        ews_df = ews_df[ews_df["TK"] > 0]
    elif selected_problem == "Keterlambatan":
        ews_df = ews_df[ews_df["Terlambat"] > 0]
    elif selected_problem == "Kombinasi":
        ews_df = ews_df[(ews_df["TK"] > 0) & (ews_df["Terlambat"] > 0)]

    period_label = " – ".join([months[0], months[-1]]) if selected_month == "Semua Bulan" and months else selected_month
    years = sorted(pd.to_numeric(scoped.get("Tahun", pd.Series(dtype=int)), errors="coerce").dropna().unique())
    year_label = str(int(years[-1])) if years else ""
    active_period = f"{period_label} {year_label}".strip()
    st.markdown(
        f"""
        <style>
        .ews-header-card {{background:linear-gradient(135deg,#fff 0%,#f8fbff 100%);border:1px solid #e2e8f0;border-left:4px solid #3b82f6;border-radius:16px;padding:20px 24px;margin:0 0 20px;box-shadow:0 4px 14px rgba(15,23,42,.035);}}
        .ews-header-top {{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px;}} .ews-header-eyebrow {{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:700;letter-spacing:.05em;color:#2563eb;}}
        .ews-header-icon {{display:inline-flex;align-items:center;justify-content:center;width:25px;height:25px;background:#eff6ff;border-radius:8px;font-size:16px;}} .ews-header-title {{font-size:28px;font-weight:750;color:#173b63;line-height:1.2;}}
        .ews-header-subtitle {{font-size:14px;font-weight:400;color:#64748b;line-height:1.6;margin-top:8px;max-width:760px;}} .ews-period-badge {{display:inline-flex;align-items:center;gap:7px;padding:7px 11px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:999px;font-size:13px;font-weight:600;color:#475569;white-space:nowrap;}}
        @media (max-width:640px) {{.ews-header-card {{padding:18px;}} .ews-header-top {{align-items:flex-start;flex-direction:column;}} .ews-header-title {{font-size:24px;}} .ews-period-badge {{white-space:normal;}}}}
        </style>
        <section class="ews-header-card">
            <div class="ews-header-top"><div class="ews-header-eyebrow"><span class="ews-header-icon">🛡️</span> EARLY WARNING SYSTEM</div><span class="ews-period-badge">📅 {active_period}</span></div>
            <div class="ews-header-title">Early Warning System Presensi Pegawai</div>
            <div class="ews-header-subtitle">Identifikasi dini pegawai yang memerlukan perhatian berdasarkan pola dan risiko presensi.</div>
        </section>
        """,
        unsafe_allow_html=True,
    )

    monthly_snapshots = {month: prepare_ews_data(scoped[scoped["Bulan"] == month]) for month in months}
    current_month = selected_month if selected_month in months else (months[-1] if months else None)
    previous_month = months[months.index(current_month) - 1] if current_month in months and months.index(current_month) else None
    current_snapshot = monthly_snapshots.get(current_month, pd.DataFrame())
    previous_scores = (monthly_snapshots.get(previous_month, pd.DataFrame()).set_index("NIP")["Risk Score"] if previous_month else pd.Series(dtype=float))
    if not current_snapshot.empty:
        current_snapshot = current_snapshot.copy()
        current_snapshot["Previous Score"] = current_snapshot["NIP"].map(previous_scores).fillna(0)
        current_snapshot["Delta Risiko"] = current_snapshot["Risk Score"] - current_snapshot["Previous Score"]
        current_snapshot["Warning Baru"] = (current_snapshot["Risk Score"] >= 25) & (current_snapshot["Previous Score"] < 25)
        current_snapshot["Level Tampilan"] = current_snapshot["Status EWS"].map(level_map)
    else:
        current_snapshot = pd.DataFrame(columns=["NIP", "Risk Score", "Delta Risiko", "Warning Baru"])

    high_count = int(ews_df[ews_df["Status EWS"].isin(["Tinggi", "Kritis"])]["NIP"].nunique())
    monitor_count = int((ews_df["Status EWS"] == "Waspada").sum())
    new_count = int(current_snapshot.get("Warning Baru", pd.Series(dtype=bool)).sum())
    rising_count = int((current_snapshot.get("Delta Risiko", pd.Series(dtype=float)) > 0).sum())
    normal_count = int((ews_df["Status EWS"] == "Normal").sum())
    for column, label, value in zip(st.columns(5), ["🔴 Perlu Warning", "🟠 Perlu Pemantauan", "⚠️ Warning Baru", "📈 Risiko Meningkat", "🟢 Kondisi Normal"], [high_count, monitor_count, new_count, rising_count, normal_count]):
        column.metric(label, value)

    priority_df = ews_df[ews_df["Status EWS"] != "Normal"].copy()
    latest_delta = current_snapshot.set_index("NIP").get("Delta Risiko", pd.Series(dtype=float))
    priority_df["Delta Risiko"] = priority_df["NIP"].map(latest_delta).fillna(0)
    priority_df["Tren"] = priority_df["Delta Risiko"].map(lambda value: "📈 Risiko meningkat" if value > 0 else "📉 Risiko menurun" if value < 0 else "➡️ Stabil")
    priority_order = {"Kritis": 0, "Tinggi": 1, "Waspada": 2, "Normal": 3}
    priority_df["_priority"] = priority_df["Status EWS"].map(priority_order)
    priority_df = priority_df.sort_values(["_priority", "Risk Score"], ascending=[True, False])
    analysis_priority_df = priority_df.copy()
    new_warning_ids = set(current_snapshot.loc[current_snapshot.get("Warning Baru", pd.Series(dtype=bool)), "NIP"].astype(str)) if not current_snapshot.empty else set()
    focused_reason = st.session_state.get("ews_reason_filter", "")
    focused_cause_nips = st.session_state.get("ews_cause_nips", [])
    if st.session_state.get("ews_show_new_only", False):
        priority_df = priority_df[priority_df["NIP"].astype(str).isin(new_warning_ids)]
        st.caption("Menampilkan pegawai warning baru. Gunakan tombol di bawah untuk kembali ke seluruh antrian.")
        if st.button("Tampilkan Semua Pegawai", key="ews_clear_new_filter"):
            st.session_state["ews_show_new_only"] = False
            st.rerun()
    if focused_reason:
        priority_df = priority_df[priority_df["Penyebab Utama"] == focused_reason]
        st.caption(f"Menampilkan pegawai dengan penyebab: {focused_reason}.")
        if st.button("Hapus Filter Penyebab", key="ews_clear_reason_filter"):
            st.session_state["ews_reason_filter"] = ""
            st.rerun()
    if focused_cause_nips:
        priority_df = priority_df[priority_df["NIP"].astype(str).isin(focused_cause_nips)]
        st.caption("Menampilkan pegawai yang terdampak TK atau keterlambatan.")
        if st.button("Hapus Filter Pegawai Terdampak", key="ews_clear_cause_filter"):
            st.session_state["ews_cause_nips"] = []
            st.rerun()
    st.markdown("<div class='section-title'>🚨 Pegawai Prioritas Warning</div>", unsafe_allow_html=True)
    if priority_df.empty:
        st.success("Tidak terdapat pegawai yang memenuhi kategori warning pada filter yang dipilih.")
    else:
        show_all = st.session_state.get("ews_show_all", False)
        visible_priority = priority_df if show_all else priority_df.head(10)
        for rank, (_, employee) in enumerate(visible_priority.iterrows(), start=1):
            status = employee["Status EWS"]
            st.markdown(f"<div style='background:#fff;border:1px solid #e2e8f0;border-left:4px solid #e11d48;border-radius:12px;padding:13px 16px;margin:.5rem 0'><strong>{rank}. {icon_map[status]} {employee['Level Tampilan'].upper()}</strong><div style='font-size:18px;font-weight:750;color:#102a43;margin-top:.35rem'>{employee['Nama Pegawai']}</div><div style='color:#64748b'>{employee['Unit Kerja']} • NIP: {employee['NIP']}</div><div style='margin-top:.35rem'><strong>Penyebab utama:</strong> {employee['Penyebab Utama']}<br>{employee['Tren']}</div></div>", unsafe_allow_html=True)
            with st.expander(f"Detail {employee['Nama Pegawai']}", expanded=False):
                history = scoped[scoped["NIP"].astype(str) == str(employee["NIP"])].copy()
                if "Tahun" in history.columns and "Tahun" in employee.index:
                    history = history[history["Tahun"] == employee["Tahun"]]
                if selected_month != "Semua Bulan":
                    history = history[history["Bulan"] == selected_month]
                monthly = history.groupby("Bulan")[["TK", "Terlambat"]].sum().reindex(months, fill_value=0)
                st.write(f"Risk Score: {int(employee['Risk Score'])}/100")
                st.write(f"Rekomendasi: {employee['Rekomendasi']}")
                st.dataframe(monthly.reset_index(), hide_index=True, use_container_width=True)
        if len(priority_df) > 10:
            if st.button("Lihat Semua Pegawai" if not show_all else "Tampilkan Top 10", key="ews_show_all_button"):
                st.session_state["ews_show_all"] = not show_all
                st.rerun()

    def format_number(value: float) -> str:
        return f"{int(value):,}".replace(",", ".")

    def format_percent(value: float) -> str:
        return f"{value:.2f}".replace(".", ",") + "%"

    # Compact warning state: this is deliberately not an alert or raw-data table.
    new_rows = current_snapshot[current_snapshot.get("Warning Baru", False)] if not current_snapshot.empty else pd.DataFrame()
    with st.container(border=True):
        if new_rows.empty:
            st.markdown("<div style='font-size:17px;font-weight:750;color:#102a43'>✅ Tidak Ada Warning Baru</div><div style='color:#64748b;margin-top:.2rem'>Seluruh pegawai memiliki status yang sama atau lebih baik dibanding periode sebelumnya.</div>", unsafe_allow_html=True)
        else:
            st.markdown(f"<div style='font-size:17px;font-weight:750;color:#102a43'>⚠️ {len(new_rows)} Warning Baru</div><div style='color:#64748b;margin-top:.2rem'>{len(new_rows)} pegawai baru masuk kategori warning pada periode ini.</div>", unsafe_allow_html=True)
            if st.button("Lihat Pegawai", key="ews_show_new_button"):
                st.session_state["ews_show_new_only"] = True
                st.rerun()

    chart_left, chart_right = st.columns(2)
    with chart_left:
        cause_source = analysis_priority_df.copy()
        tk_nips = set(cause_source.loc[cause_source["TK"] > 0, "NIP"].astype(str))
        late_nips = set(cause_source.loc[cause_source["Terlambat"] > 0, "NIP"].astype(str))
        affected_nips = tk_nips | late_nips
        affected_count = len(affected_nips)
        total_warning_unique = cause_source["NIP"].astype(str).nunique()
        affected_pct = affected_count / total_warning_unique * 100 if total_warning_unique else 0
        tk_count, late_count = len(tk_nips), len(late_nips)
        tk_pct = tk_count / total_warning_unique * 100 if total_warning_unique else 0
        late_pct = late_count / total_warning_unique * 100 if total_warning_unique else 0
        if affected_count == 0:
            st.markdown("""
                <style>
                .ews-cause-card{background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:20px 22px;box-shadow:0 4px 14px rgba(15,23,42,.04)}
                .ews-cause-header{font-size:19px;font-weight:700;color:#173b63}.ews-cause-subtitle{font-size:13px;color:#64748b;margin-top:3px}
                </style>
                <section class='ews-cause-card'><div class='ews-cause-header'>📊 Penyebab Utama Warning</div><div class='ews-cause-subtitle'>Pola utama yang memicu warning pegawai</div><div style='color:#475569;margin-top:20px'>✅ Tidak terdapat penyebab warning dominan pada filter yang dipilih.</div></section>
            """, unsafe_allow_html=True)
        else:
            dominant = "TK dan keterlambatan" if tk_count and late_count else "TK" if tk_count else "keterlambatan"
            count_label = f"{affected_count:,}".replace(",", ".")
            tk_count_label = f"{tk_count:,}".replace(",", ".")
            late_count_label = f"{late_count:,}".replace(",", ".")
            pct_label = f"{affected_pct:.1f}".replace(".", ",")
            tk_width, late_width = min(tk_pct, 100), min(late_pct, 100)
            st.markdown(f"""
                <style>
                .ews-cause-card{{background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:20px 22px;box-shadow:0 4px 14px rgba(15,23,42,.04)}}
                .ews-cause-header{{font-size:19px;font-weight:700;color:#173b63}} .ews-cause-subtitle{{font-size:13px;color:#64748b;margin-top:3px}}
                .ews-cause-main{{display:grid;grid-template-columns:40% 60%;align-items:center;gap:14px;margin:18px 0}}
                .ews-cause-ring{{width:78px;height:78px;border-radius:50%;background:conic-gradient(#2563eb {affected_pct:.3f}%,#eef2f7 0);display:grid;place-items:center}}
                .ews-cause-ring-inner{{width:64px;height:64px;border-radius:50%;background:#fff;display:flex;flex-direction:column;align-items:center;justify-content:center;line-height:1.05}}
                .ews-cause-total{{font-size:29px;font-weight:750;color:#173b63}} .ews-cause-total-label{{font-size:12px;color:#64748b;margin-top:3px}}
                .ews-cause-summary-label{{font-size:11px;letter-spacing:.06em;font-weight:700;color:#64748b}} .ews-cause-summary-title{{font-size:16px;font-weight:700;color:#173b63;margin-top:4px}} .ews-cause-summary-value{{font-size:13px;color:#475569;margin-top:4px}}
                .ews-cause-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px}} .ews-cause-item{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:12px 14px}} .ews-cause-item:hover{{border-color:#bfdbfe;box-shadow:0 4px 12px rgba(37,99,235,.06)}}
                .ews-cause-item-title{{font-size:13px;font-weight:700;color:#173b63}} .ews-cause-item-subtitle,.ews-cause-item-count{{font-size:12px;color:#64748b}} .ews-cause-item-count{{margin-top:8px;font-weight:600;color:#475569}}
                .ews-cause-progress-track{{height:6px;background:#eef2f7;border-radius:99px;margin-top:8px;overflow:hidden}} .ews-cause-progress-fill{{height:100%;background:#2563eb;border-radius:99px}}
                .ews-cause-footer{{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;margin-top:16px}} .ews-cause-insight{{font-size:13px;line-height:1.45;color:#475569;max-width:72%}}
                @media(max-width:640px){{.ews-cause-main,.ews-cause-grid{{grid-template-columns:1fr}}.ews-cause-footer{{align-items:flex-start;flex-direction:column}}.ews-cause-insight{{max-width:100%}}}}
                </style>
                <section class='ews-cause-card'>
                    <div class='ews-cause-header'>📊 Penyebab Utama Warning</div>
                    <div class='ews-cause-subtitle'>Pola utama yang memicu warning pegawai</div>
                    <div class='ews-cause-main'>
                        <div class='ews-cause-ring'><div class='ews-cause-ring-inner'><div class='ews-cause-total'>{count_label}</div><div class='ews-cause-total-label'>Pegawai</div></div></div>
                        <div><div class='ews-cause-summary-label'>PENYEBAB DOMINAN</div><div class='ews-cause-summary-title'>{dominant.title()}</div><div class='ews-cause-summary-value'>{pct_label}% dari pegawai warning</div></div>
                    </div>
                    <div class='ews-cause-grid'>
                        <div class='ews-cause-item'><div class='ews-cause-item-title'>📅 TK</div><div class='ews-cause-item-subtitle'>Tanpa Keterangan</div><div class='ews-cause-item-count'>{tk_count_label} Pegawai</div><div class='ews-cause-progress-track'><div class='ews-cause-progress-fill' style='width:{tk_width:.3f}%'></div></div></div>
                        <div class='ews-cause-item'><div class='ews-cause-item-title'>⏱ Keterlambatan</div><div class='ews-cause-item-subtitle'>Datang melewati batas waktu</div><div class='ews-cause-item-count'>{late_count_label} Pegawai</div><div class='ews-cause-progress-track'><div class='ews-cause-progress-fill' style='width:{late_width:.3f}%'></div></div></div>
                    </div>
                    <div class='ews-cause-footer'><div class='ews-cause-insight'>{dominant.capitalize()} terdeteksi pada {pct_label}% pegawai warning periode aktif.</div></div>
                </section>
            """, unsafe_allow_html=True)
            if st.button(f"Lihat {count_label} Pegawai →", key="ews_cause_people", type="primary"):
                st.session_state["ews_cause_nips"] = sorted(affected_nips)
                st.rerun()

    OPD_RANKING_LIMIT = 3
    with chart_right:
        totals = ews_df.groupby("Unit Kerja")["NIP"].nunique().rename("Total")
        risks = analysis_priority_df.groupby("Unit Kerja")["NIP"].nunique().rename("Warning")
        opd_risk = pd.concat([totals, risks], axis=1).fillna(0)
        opd_risk["Persentase"] = opd_risk["Warning"] / opd_risk["Total"].replace(0, 1) * 100
        opd_risk = opd_risk.sort_values("Persentase", ascending=False)
        if opd_risk.empty:
            st.markdown("""
                <style>
                .ews-opd-card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:16px 18px;box-shadow:0 3px 12px rgba(15,23,42,.035)}
                .ews-opd-header{font-size:17px;font-weight:700;color:#173b63}.ews-opd-subtitle{font-size:13px;color:#64748b;margin-top:4px}
                </style>
                <section class='ews-opd-card'><div class='ews-opd-header'>🏢 OPD Perlu Perhatian</div><div class='ews-opd-subtitle'>Perbandingan proporsi pegawai berisiko berdasarkan unit kerja.</div><div style='color:#475569;margin-top:18px'>✅ Tidak terdapat pegawai warning pada OPD yang tercakup dalam filter aktif.</div></section>
            """, unsafe_allow_html=True)
        else:
            displayed_opd = opd_risk.head(OPD_RANKING_LIMIT)
            max_risk_pct = float(displayed_opd["Persentase"].max())
            badge = f"Top {len(displayed_opd)}"
            opd_rows_html = []
            for rank, (opd, row) in enumerate(displayed_opd.iterrows(), start=1):
                display_name = escape(str(opd).title())
                original_name = escape(str(opd), quote=True)
                relative_width = float(row["Persentase"]) / max_risk_pct * 100 if max_risk_pct else 0
                risk_pct_label = format_percent(float(row["Persentase"]))
                warning_label = format_number(float(row["Warning"]))
                total_label = format_number(float(row["Total"]))
                opd_rows_html.append(f"""
                    <div class='ews-opd-row'>
                        <div class='ews-opd-topline'><div class='ews-opd-rank'>{rank:02d}</div><div class='ews-opd-name' title='{original_name}'>{display_name}</div><div class='ews-opd-percentage'>{risk_pct_label}</div></div>
                        <div class='ews-opd-meta'>{warning_label} pegawai berisiko • dari {total_label} pegawai</div>
                        <div class='ews-opd-progress-track' title='Panjang bar menunjukkan perbandingan relatif antar-OPD.'><div class='ews-opd-progress-fill' style='width:{relative_width:.3f}%'></div></div>
                    </div>
                """)
            top_opd, top_row = displayed_opd.iloc[0].name, displayed_opd.iloc[0]
            all_zero = bool(displayed_opd["Warning"].sum() == 0)
            insight_html = (
                "✅ Tidak terdapat pegawai warning pada OPD yang tercakup dalam filter aktif."
                if all_zero
                else f"💡 {escape(str(top_opd).title())} memiliki proporsi pegawai warning tertinggi sebesar {format_percent(float(top_row['Persentase']))} atau {format_number(float(top_row['Warning']))} dari {format_number(float(top_row['Total']))} pegawai."
            )
            st.markdown(f"""
                <style>
                .ews-opd-card{{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:16px 18px;box-shadow:0 3px 12px rgba(15,23,42,.035)}}
                .ews-opd-header-wrap{{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}} .ews-opd-header{{font-size:17px;font-weight:700;color:#173b63}} .ews-opd-badge{{font-size:11px;font-weight:700;color:#2563eb;background:#eff6ff;border-radius:999px;padding:4px 8px;white-space:nowrap}}
                .ews-opd-subtitle{{font-size:13px;color:#64748b;margin-top:4px}} .ews-opd-list{{margin-top:16px}} .ews-opd-row{{padding:0 0 15px;margin-bottom:15px;border-bottom:1px solid #f1f5f9}} .ews-opd-row:last-child{{margin-bottom:0}}
                .ews-opd-topline{{display:grid;grid-template-columns:28px minmax(0,1fr) auto;gap:8px;align-items:start}} .ews-opd-rank{{font-size:12px;font-weight:700;color:#64748b;padding-top:2px}} .ews-opd-name{{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden;font-size:13px;font-weight:700;color:#173b63;line-height:1.35;overflow-wrap:anywhere}} .ews-opd-percentage{{font-size:15px;font-weight:700;color:#173b63;white-space:nowrap}}
                .ews-opd-meta{{font-size:12px;color:#64748b;margin:5px 0 8px 36px}} .ews-opd-progress-track{{height:6px;background:#eef2f7;border-radius:999px;overflow:hidden;margin-left:36px}} .ews-opd-progress-fill{{height:100%;background:#2563eb;border-radius:999px}} .ews-opd-row:not(:first-child) .ews-opd-progress-fill{{opacity:.72}}
                .ews-opd-insight{{font-size:12px;line-height:1.45;color:#475569;margin-top:14px}}
                @media(max-width:640px){{.ews-opd-card{{padding:16px}}.ews-opd-topline{{grid-template-columns:24px minmax(0,1fr) auto;gap:7px}}.ews-opd-meta,.ews-opd-progress-track{{margin-left:31px}}}}
                </style>
                <section class='ews-opd-card'>
                    <div class='ews-opd-header-wrap'><div class='ews-opd-header'>🏢 OPD Perlu Perhatian</div>{f"<span class='ews-opd-badge'>{badge}</span>" if badge else ""}</div>
                    <div class='ews-opd-subtitle'>Perbandingan proporsi pegawai berisiko berdasarkan unit kerja.</div>
                    <div class='ews-opd-list'>{''.join(item.strip() for item in opd_rows_html)}</div>
                    <div class='ews-opd-insight'>{insight_html}</div>
                </section>
            """, unsafe_allow_html=True)

    day_left, peak_right = st.columns(2)
    weekday_order = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat"]
    day_counts = scoped.groupby("Hari Dominan")["Terlambat"].sum().reindex(weekday_order, fill_value=0)
    with day_left, st.container(border=True):
        st.markdown("<div style='font-size:18px;font-weight:750;color:#102a43'>📅 Hari Rawan Pelanggaran</div><div style='color:#64748b;font-size:13px;margin:.2rem 0 .75rem'>Pola pelanggaran berdasarkan hari kerja</div>", unsafe_allow_html=True)
        if day_counts.sum() > 0:
            day_data = day_counts.rename_axis("Hari").reset_index(name="Pelanggaran")
            day_data["Persentase"] = day_data["Pelanggaran"] / day_data["Pelanggaran"].sum() * 100
            peak_day = day_counts.idxmax()
            st.altair_chart(alt.Chart(day_data).mark_bar().encode(
                x=alt.X("Hari:N", sort=weekday_order, title=None), y=alt.Y("Pelanggaran:Q", title="Pelanggaran"),
                color=alt.condition(alt.datum.Hari == peak_day, alt.value("#f97316"), alt.value("#cbd5e1")),
                tooltip=[alt.Tooltip("Hari:N", title="Hari"), alt.Tooltip("Pelanggaran:Q", title="Jumlah", format=","), alt.Tooltip("Persentase:Q", title="Persentase", format=".1f")],
            ).properties(height=190), use_container_width=True)
            st.caption(f"⚠️ {peak_day} merupakan hari dengan pelanggaran tertinggi: {format_number(day_counts.max())} pelanggaran ({format_percent(day_counts.max() / day_counts.sum() * 100)}).")
        else:
            st.caption("Data hari pelanggaran belum tersedia.")

    late_arrival = scoped[(scoped["Terlambat"] > 0) & scoped["Jam Datang"].notna()].copy()
    with peak_right, st.container(border=True):
        st.markdown("<div style='font-size:18px;font-weight:750;color:#102a43'>⏰ Distribusi Waktu Keterlambatan</div><div style='color:#64748b;font-size:13px;margin:.2rem 0 .75rem'>Rentang jam ketika keterlambatan paling sering terjadi</div>", unsafe_allow_html=True)
        if late_arrival.empty:
            st.caption("Data jam keterlambatan belum tersedia.")
            peak_counts = pd.Series(dtype=float)
        else:
            labels = ["07.31–07.45", "07.46–08.00", "08.01–08.30", "> 08.30"]
            late_arrival["Rentang Jam"] = pd.cut(late_arrival["Jam Datang"], bins=[7.5, 7.75, 8, 8.5, 24], labels=labels)
            peak_counts = late_arrival.groupby("Rentang Jam", observed=False)["Terlambat"].sum().reindex(labels, fill_value=0)
            peak_data = peak_counts.rename_axis("Rentang Jam").reset_index(name="Keterlambatan")
            peak_data["Persentase"] = peak_data["Keterlambatan"] / peak_data["Keterlambatan"].sum() * 100 if peak_data["Keterlambatan"].sum() else 0
            peak_range = peak_counts.idxmax()
            st.altair_chart(alt.Chart(peak_data).mark_bar().encode(
                y=alt.Y("Rentang Jam:N", sort=labels, title=None), x=alt.X("Keterlambatan:Q", title="Keterlambatan"),
                color=alt.condition(alt.datum["Rentang Jam"] == peak_range, alt.value("#2563eb"), alt.value("#cbd5e1")),
                tooltip=[alt.Tooltip("Rentang Jam:N", title="Rentang jam"), alt.Tooltip("Keterlambatan:Q", title="Jumlah", format=","), alt.Tooltip("Persentase:Q", title="Persentase", format=".1f")],
            ).properties(height=190), use_container_width=True)
            if peak_counts.sum() > 0:
                st.caption(f"⏰ Puncak keterlambatan terjadi pukul {peak_range}: {format_number(peak_counts.max())} kejadian ({format_percent(peak_counts.max() / peak_counts.sum() * 100)}).")

    insights = []
    if rising_count:
        insights.append(f"⚠️ {rising_count} pegawai mengalami peningkatan risiko dibanding periode sebelumnya.")
    if not priority_df.empty:
        insights.append(f"⚠️ {len(priority_df)} pegawai memerlukan perhatian pada filter aktif.")
    if day_counts.sum() > 0:
        insights.append(f"📅 {day_counts.idxmax()} merupakan hari dengan pelanggaran tertinggi.")
    if not opd_risk.empty:
        top_opd = opd_risk.sort_values("Persentase", ascending=False).iloc[0]
        insights.append(f"🏢 {top_opd.name} memiliki persentase pegawai warning tertinggi ({top_opd['Persentase']:.1f}%).")
    st.markdown("<div class='section-title'>💡 Insight Early Warning</div>", unsafe_allow_html=True)
    for insight in insights[:5]:
        st.markdown(insight)
    st.caption("Hasil Early Warning System merupakan indikator awal berdasarkan data presensi dan tidak secara otomatis menetapkan hukuman disiplin. Verifikasi dan penetapan tindak lanjut dilakukan oleh pejabat yang berwenang sesuai ketentuan peraturan perundang-undangan.")


def show_dashboard_focus_legacy_bi() -> None:
    """Renderer BI lama; dipertahankan sementara sebagai referensi nonaktif."""
    inject_dashboard_css()
    data = load_employee_data()
    if data.empty:
        st.info("Belum ada data presensi yang dapat ditampilkan.")
        return

    years = sorted(pd.to_numeric(data["Tahun"], errors="coerce").dropna().astype(int).unique().tolist())
    months = [month for month in ANALYTICS_MONTHS if month in set(data["Bulan"])]
    opds = sorted(data["Unit Kerja"].dropna().astype(str).unique().tolist())
    preferred_employee_types = ["PNS", "PPPK", "Belum Diketahui"]
    available_employee_types = (
        set(data["Jenis Pegawai"].dropna().astype(str).str.strip())
        if "Jenis Pegawai" in data.columns else set()
    )
    employee_type_options = ["Semua Jenis Pegawai"] + [
        item for item in preferred_employee_types if item in available_employee_types
    ]
    if "Jenis Pegawai" not in data.columns:
        LOGGER.warning("Kolom Jenis Pegawai belum tersedia saat filter dirender.")
    elif available_employee_types == {"Belum Diketahui"}:
        LOGGER.warning("Deteksi PNS/PPPK belum menghasilkan klasifikasi; periksa format NIP dan urutan processing data.")
    st.markdown("<h1 class='dashboard-title'>Executive Dashboard</h1>", unsafe_allow_html=True)
    st.markdown("<p class='dashboard-subtitle'>Monitoring kepatuhan presensi dan indikator awal tindak lanjut.</p>", unsafe_allow_html=True)

    f1, f2, f3, f4, f5, f6 = st.columns([1, 1, 2, 1.4, 1.5, .8])
    with f1:
        year = st.selectbox("Tahun", ["Semua Tahun"] + years, key="global_year")
    with f2:
        month = st.selectbox("Bulan", ["Semua Bulan"] + months, key="global_month")
    with f3:
        opd = st.selectbox("OPD", ["Semua OPD"] + opds, key="global_opd")
    with f4:
        if len(employee_type_options) > 1:
            employee_type_key = "executive_filter_jenis_pegawai"
            if st.session_state.get(employee_type_key) not in employee_type_options:
                st.session_state[employee_type_key] = "Semua Jenis Pegawai"
            employee_type = st.selectbox(
                "Jenis Pegawai", employee_type_options, key=employee_type_key
            )
        else:
            employee_type = "Semua Jenis Pegawai"
            st.selectbox(
                "Jenis Pegawai", ["Data tidak tersedia"],
                key="global_employee_type_unavailable", disabled=True,
            )
    with f5:
        warning = st.selectbox("Status Early Warning", ["Semua Status"] + WARNING_STATUSES, key="global_warning")
    with f6:
        st.write("")
        if st.button("Reset", key="global_reset", use_container_width=True):
            for key in ["global_year", "global_month", "global_opd", "executive_filter_jenis_pegawai", "global_warning"]:
                st.session_state.pop(key, None)
            st.rerun()
    st.caption("Data yang ditampilkan menyesuaikan periode, OPD, jenis pegawai, dan status early warning yang dipilih.")

    scoped = apply_filters(
        data, year=year, month=month, opd=opd,
        employee_type=employee_type, warning=warning,
    )
    if scoped.empty:
        st.info("Tidak terdapat data untuk kombinasi filter yang dipilih.")
        return
    metrics = dashboard_metrics(scoped)
    trend_scope = apply_filters(
        data, year=year, opd=opd,
        employee_type=employee_type, warning=warning,
    )
    trend = monthly_trend(trend_scope)
    previous_delta = None
    if month != "Semua Bulan" and len(trend) > 1:
        current = trend[(trend["Bulan"] == month) & ((trend["Tahun"] == int(year)) if year != "Semua Tahun" else True)]
        if not current.empty:
            position = current.index[-1]
            if position > trend.index.min():
                previous_delta = float(trend.loc[position, "Perubahan"])

    cards = st.columns(5)
    cards[0].metric("TOTAL PEGAWAI", f"{metrics['total_employees']:,}".replace(",", "."), help="Pegawai unik (NIP) dalam data terpilih")
    cards[1].metric("TINGKAT KEPATUHAN", f"{metrics['attendance_rate']:.1f}%".replace(".", ","), None if previous_delta is None else f"{previous_delta:+.1f} poin dari periode sebelumnya")
    cards[2].metric("TOTAL TK", f"{metrics['total_tk']} hari", f"{metrics['employees_with_tk']} pegawai teridentifikasi")
    cards[3].metric("EARLY WARNING", f"{metrics['early_warning']} Pegawai", f"{metrics['priority']} Prioritas Tindak Lanjut")
    cards[4].metric("OPD PERLU PERHATIAN", metrics["opd_attention"], help="OPD dengan kepatuhan di bawah rata-rata data terpilih")

    st.markdown("<div class='section-title'>Tren Kepatuhan Presensi</div>", unsafe_allow_html=True)
    st.caption("Menunjukkan perubahan tingkat kepatuhan presensi dari waktu ke waktu.")
    if trend.empty:
        st.info("Data tren tidak tersedia.")
    else:
        st.altair_chart(alt.Chart(trend).mark_line(point=True, color="#2563eb").encode(
            x=alt.X("Periode:N", sort=None, title="Periode"), y=alt.Y("Kepatuhan:Q", title="Kepatuhan (%)", scale=alt.Scale(domain=[0, 100])),
            tooltip=["Periode:N", alt.Tooltip("Kepatuhan:Q", format=".1f"), alt.Tooltip("Perubahan:Q", format="+.1f")]
        ).properties(height=280), use_container_width=True)

    st.markdown("<div class='section-title'>Tren Ketidakhadiran Tanpa Keterangan</div>", unsafe_allow_html=True)
    st.altair_chart(alt.Chart(trend).mark_bar(color="#f97316").encode(
        x=alt.X("Periode:N", sort=None, title="Periode"), y=alt.Y("TK:Q", title="Jumlah TK"), tooltip=["Periode:N", "TK:Q", alt.Tooltip("Perubahan TK (%):Q", format="+.1f")]
    ).properties(height=250), use_container_width=True)
    if len(trend) > 1 and pd.notna(trend.iloc[-1]["Perubahan TK (%)"]):
        change = float(trend.iloc[-1]["Perubahan TK (%)"])
        st.caption(f"Jumlah TK {'meningkat' if change > 0 else 'menurun' if change < 0 else 'tetap'} {abs(change):.1f}% dibandingkan periode sebelumnya.")

    opd_data = opd_summary(scoped)
    st.markdown("<div class='section-title'>Perbandingan OPD</div>", unsafe_allow_html=True)
    left, right = st.columns(2)
    with left:
        st.markdown("**Tingkat Kepatuhan per OPD**")
        average = float(metrics["attendance_rate"])
        chart = alt.Chart(opd_data).mark_bar(color="#2563eb").encode(y=alt.Y("OPD:N", sort="-x"), x=alt.X("Kepatuhan:Q", scale=alt.Scale(domain=[0, 100])), tooltip=["OPD:N", alt.Tooltip("Kepatuhan:Q", format=".1f")])
        rule = alt.Chart(pd.DataFrame({"Rata-rata": [average]})).mark_rule(color="#dc2626", strokeDash=[5, 4]).encode(x="Rata-rata:Q")
        st.altair_chart((chart + rule).properties(height=max(220, len(opd_data) * 42)), use_container_width=True)
    with right:
        st.markdown("**Ketidakhadiran Tanpa Keterangan per OPD**")
        st.altair_chart(alt.Chart(opd_data).mark_bar(color="#f97316").encode(y=alt.Y("OPD:N", sort="-x"), x="TK:Q", tooltip=["OPD:N", "TK:Q"]).properties(height=max(220, len(opd_data) * 42)), use_container_width=True)

    employees = employee_summary(scoped)
    st.markdown("<div class='section-title'>Distribusi Early Warning</div>", unsafe_allow_html=True)
    distribution = employees["Status Early Warning"].value_counts().reindex(WARNING_STATUSES, fill_value=0).rename_axis("Status").reset_index(name="Pegawai")
    st.altair_chart(alt.Chart(distribution).mark_arc(innerRadius=55).encode(theta="Pegawai:Q", color=alt.Color("Status:N", scale=alt.Scale(domain=WARNING_STATUSES, range=["#10b981", "#eab308", "#f97316", "#dc2626"])), tooltip=["Status:N", "Pegawai:Q"]).properties(height=260), use_container_width=True)
    st.caption("Status early warning merupakan indikator awal berdasarkan data presensi dan tidak menyatakan bahwa pegawai telah terbukti melakukan pelanggaran disiplin.")

    st.markdown("<div class='section-title'>Pegawai Memerlukan Perhatian</div>", unsafe_allow_html=True)
    priorities = employees[employees["Status Early Warning"].ne("Normal")].copy()
    rank = {status: index for index, status in enumerate(WARNING_STATUSES)}
    priorities["_rank"] = priorities["Status Early Warning"].map(rank)
    priorities = priorities.sort_values(["_rank", "TK"], ascending=[False, False]).head(10)
    if priorities.empty:
        st.success("Tidak terdapat pegawai dengan indikator early warning pada filter aktif.")
    else:
        st.dataframe(priorities[["Nama Pegawai", "Unit Kerja", "Status Early Warning"]].rename(columns={"Unit Kerja": "OPD"}), hide_index=True, use_container_width=True)
        chosen_detail = st.selectbox("Pilih pegawai untuk melihat detail", [f"{row['Nama Pegawai']} — {row['NIP']}" for _, row in priorities.iterrows()], key="dashboard_detail_employee")
        if st.button("Lihat Detail", key="dashboard_open_detail"):
            st.session_state["navigate_to_page"] = "Detail Pegawai"
            st.session_state["employee_detail_employee"] = chosen_detail
            st.rerun()

    raw = load_semua_presensi()
    raw = raw[raw["NIP"].isin(scoped["NIP"].unique())]
    if year != "Semua Tahun": raw = raw[raw["Tahun"].eq(int(year))]
    if month != "Semua Bulan": raw = raw[raw["Nama_Bulan"].eq(month)]
    if opd != "Semua OPD": raw = raw[raw["Unit Kerja"].eq(opd)]
    weekdays = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat"]
    day = raw.groupby("Hari")[["TK", "Terlambat"]].sum().reindex(weekdays, fill_value=0).reset_index()
    st.markdown("<div class='section-title'>Hari Rawan Ketidakpatuhan</div>", unsafe_allow_html=True)
    if day[["TK", "Terlambat"]].sum().sum() == 0:
        st.info("Tidak terdapat kejadian TK atau keterlambatan pada filter aktif.")
    else:
        st.bar_chart(day.set_index("Hari"), height=260)
        totals = day.assign(Total=day["TK"] + day["Terlambat"])
        st.caption(f"Ketidakpatuhan paling banyak terjadi pada hari {totals.loc[totals['Total'].idxmax(), 'Hari']}.")

    st.markdown("<div class='section-title'>Distribusi Waktu Kedatangan</div>", unsafe_allow_html=True)
    arrivals = pd.to_datetime(raw.loc[raw["Jam_Masuk"].astype(str).str.match(r"^\d{1,2}:\d{2}$", na=False), "Jam_Masuk"], errors="coerce")
    if arrivals.dropna().empty:
        st.info("Data jam masuk tidak tersedia pada filter aktif.")
    else:
        minutes = arrivals.dt.hour * 60 + arrivals.dt.minute
        bins = pd.cut(minutes, bins=[389, 420, 450, 480, 1440], labels=["06.30–07.00", "07.00–07.30", "07.30–08.00", ">08.00"], include_lowest=True)
        st.bar_chart(bins.value_counts(sort=False).rename("Kedatangan"), height=240)

    st.markdown("<div class='section-title'>Kualitas Data</div>", unsafe_allow_html=True)
    quality = raw.attrs.get("etl_quality", {})
    if quality:
        labels = [("File diproses", "files_processed"), ("Record masuk", "records_input"), ("Data valid", "records_valid"), ("Duplikat", "duplicates"), ("Tidak lengkap", "incomplete"), ("Perlu verifikasi", "needs_verification")]
        for column, (label, key) in zip(st.columns(6), labels): column.metric(label, quality.get(key, 0))
    else:
        st.caption(f"{len(_excel_source_signature())} file diproses • {len(raw)} record valid • {int(raw['Presensi_Tidak_Lengkap'].sum())} record tidak lengkap • {int(raw['TK'].sum())} TK perlu verifikasi")


def show_dashboard_focus() -> None:
    """Executive summary read-only berbasis agregasi existing dan NIP unik."""
    inject_dashboard_css()
    data = load_employee_data()
    if data.empty:
        st.info("Tidak terdapat data pada kombinasi filter yang dipilih.")
        return

    years = sorted(pd.to_numeric(data["Tahun"], errors="coerce").dropna().astype(int).unique().tolist())
    months = [month for month in ANALYTICS_MONTHS if month in set(data["Bulan"].astype(str))]
    opds = sorted(data["Unit Kerja"].dropna().astype(str).unique().tolist())
    type_values = set(data.get("Jenis Pegawai", pd.Series(dtype=str)).dropna().astype(str))
    employee_types = [value for value in ["PNS", "PPPK", "Belum Diketahui"] if value in type_values]

    st.markdown("""
    <style>
    .block-container:has(.executive-page){max-width:1500px;margin-left:auto;margin-right:auto}.executive-page{color:#0f172a}
    .executive-header{margin-bottom:16px}.executive-title{font-size:28px;font-weight:700;line-height:1.2;color:#173b63}.executive-subtitle{font-size:14px;color:#64748b;margin-top:5px;max-width:820px}
    .executive-filter-bar{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:10px 14px 2px;box-shadow:0 3px 12px rgba(15,23,42,.035)}
    .executive-meta-strip{font-size:12px;color:#64748b;padding:9px 2px 4px}.executive-section{margin-top:24px}.executive-section-title{font-size:18px;font-weight:700;color:#173b63}.executive-section-subtitle{font-size:13px;color:#64748b;margin:3px 0 12px}
    .executive-kpi-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.executive-card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:16px 18px;box-shadow:0 3px 12px rgba(15,23,42,.035)}
    .executive-interactive{transition:transform .18s ease,box-shadow .18s ease,border-color .18s ease}.executive-interactive:hover{transform:translateY(-1px);box-shadow:0 5px 16px rgba(15,23,42,.05);border-color:#cbd5e1}
    .executive-kpi-value{font-size:30px;font-weight:750;line-height:1.1;color:#173b63}.executive-kpi-label{font-size:12px;color:#64748b;margin-top:5px}.executive-delta{font-size:11px;margin-top:8px}.executive-delta.good{color:#047857}.executive-delta.bad{color:#c2410c}.executive-delta.neutral{color:#64748b}
    .executive-overview-grid{display:grid;grid-template-columns:1.15fr .85fr;gap:16px}.executive-risk-layout{display:grid;grid-template-columns:210px 1fr;gap:12px;align-items:center}.executive-risk-row,.executive-condition-row{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:10px;align-items:center;padding:9px 0;border-bottom:1px solid #f1f5f9;font-size:12px}.executive-risk-row:last-child,.executive-condition-row:last-child{border-bottom:0}.executive-risk-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:7px}.executive-risk-count{font-weight:700;color:#173b63}.executive-risk-pct{color:#64748b;min-width:48px;text-align:right}
    .executive-condition-row{grid-template-columns:minmax(0,1fr) auto}.executive-condition-value{font-size:13px;font-weight:700;color:#173b63}.executive-trend{padding-bottom:8px}.executive-trend-insight{font-size:12px;color:#475569;background:#f8fafc;border-radius:10px;padding:10px 12px;margin-top:8px}
    .executive-priority{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.executive-priority-item{position:relative;min-height:108px}.executive-priority-no{font-size:12px;font-weight:750;color:#94a3b8}.executive-priority-title{font-size:14px;font-weight:700;color:#173b63;margin-top:8px}.executive-priority-text{font-size:12px;line-height:1.5;color:#64748b;margin-top:5px}
    .executive-opd-row{display:grid;grid-template-columns:28px minmax(180px,1fr) 70px 2fr;gap:10px;align-items:center;padding:9px 0;border-bottom:1px solid #f1f5f9;font-size:12px}.executive-opd-row:last-child{border-bottom:0}.executive-opd-rank{font-weight:700;color:#94a3b8}.executive-opd-name{font-weight:650;color:#173b63}.executive-opd-value{text-align:right;font-weight:700;color:#173b63}.executive-opd-track{height:7px;background:#eef2f7;border-radius:99px;overflow:hidden}.executive-opd-fill{height:100%;background:#2563eb;border-radius:99px}
    .executive-insight{font-size:13px;color:#334155;background:#fff;border-left:3px solid #2563eb;border-radius:10px;padding:10px 14px;margin:7px 0}
    @media(max-width:900px){.executive-kpi-grid{grid-template-columns:repeat(2,1fr)}.executive-overview-grid{grid-template-columns:1fr}.executive-priority{grid-template-columns:1fr}.executive-risk-layout{grid-template-columns:180px 1fr}}
    @media(max-width:640px){.executive-title{font-size:26px}.executive-risk-layout{grid-template-columns:1fr}.executive-opd-row{grid-template-columns:24px 1fr 60px}.executive-opd-track{grid-column:2/4}}
    </style><div class='executive-page'></div>
    """, unsafe_allow_html=True)
    st.markdown(
        "<h1 class='executive-page-title' style='display:block!important;visibility:visible!important;opacity:1!important;color:#173b63!important;font-size:28px!important;font-weight:700!important;line-height:1.2!important;text-align:left!important;margin:0 0 6px!important'>Executive Dashboard</h1>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<div class='executive-subtitle' style='display:block!important;visibility:visible!important;opacity:1!important;color:#64748b!important;font-size:14px!important;margin:0 0 16px!important'>Ringkasan kondisi kepatuhan presensi ASN untuk mendukung monitoring dan pengambilan keputusan.</div>",
        unsafe_allow_html=True,
    )

    def reset_executive_filters() -> None:
        for key in ["executive_filter_year", "executive_filter_month", "executive_filter_opd", "executive_filter_employee_type", "executive_filter_risk"]:
            st.session_state.pop(key, None)

    st.markdown("<div class='executive-filter-bar'>", unsafe_allow_html=True)
    filters = st.columns([1, 1, 1.8, 1.35, 1.45, .7])
    with filters[0]: year = st.selectbox("Tahun", ["Semua Tahun"] + years, key="executive_filter_year")
    with filters[1]: month = st.selectbox("Bulan", ["Semua Bulan"] + months, key="executive_filter_month")
    with filters[2]: opd = st.selectbox("OPD", ["Semua OPD"] + opds, key="executive_filter_opd")
    with filters[3]: employee_type = st.selectbox("Jenis Pegawai", ["Semua Jenis Pegawai"] + employee_types, key="executive_filter_employee_type")
    with filters[4]: warning = st.selectbox("Status Early Warning", ["Semua Status"] + WARNING_STATUSES, key="executive_filter_risk")
    with filters[5]:
        st.write("")
        st.button("Reset", key="executive_filter_reset", on_click=reset_executive_filters, use_container_width=True)
    st.markdown("</div>", unsafe_allow_html=True)

    executive_df = apply_filters(data, year=year, month=month, opd=opd, employee_type=employee_type, warning=warning)
    if executive_df.empty:
        st.info("Tidak terdapat data pada kombinasi filter yang dipilih.")
        return
    employees = employee_summary(executive_df)
    metrics = dashboard_metrics(executive_df)
    allowed_nips = set(executive_df["NIP"].astype(str))
    daily_executive = _daily_attendance_scope(
        year=year, month=month, opd=opd, employee_type=employee_type,
        allowed_nips=allowed_nips,
    )
    metrics["attendance_rate"] = summarize_attendance_indicators(daily_executive)["compliance_percentage"]
    period_text = f"{month} {year}" if month != "Semua Bulan" and year != "Semua Tahun" else month if month != "Semua Bulan" else str(year) if year != "Semua Tahun" else "Seluruh periode"
    type_text = "PNS & PPPK" if employee_type == "Semua Jenis Pegawai" else employee_type
    st.markdown(f"<div class='executive-meta-strip'>{escape(period_text)} • {escape(opd)} • {escape(type_text)} • {employees['NIP'].nunique():,} Pegawai</div>".replace(",", "."), unsafe_allow_html=True)

    history_scope = apply_filters(data, year=year, opd=opd, employee_type=employee_type, warning=warning)
    trend = monthly_trend(history_scope)
    daily_history = _daily_attendance_scope(
        year=year, opd=opd, employee_type=employee_type,
        allowed_nips=set(history_scope["NIP"].astype(str)),
    )
    canonical_trend = aggregate_attendance_trend(daily_history, "Bulanan")
    compliance_by_period = {
        (int(row["Periode"].year), MONTH_NAMES[f"{int(row['Periode'].month):02d}"]): float(row["Kepatuhan Presensi"])
        for _, row in canonical_trend.iterrows()
    }
    if not trend.empty:
        trend["Kepatuhan"] = [
            compliance_by_period.get((int(row.Tahun), str(row.Bulan)), row.Kepatuhan)
            for row in trend.itertuples()
        ]
    warning_by_period = []
    for (trend_year, trend_month), group in history_scope.groupby(["Tahun", "Bulan"], dropna=False):
        month_employees = employee_summary(group)
        warning_by_period.append({"Tahun": int(trend_year), "Bulan": str(trend_month), "Pegawai Warning": int(month_employees.loc[month_employees["Status Early Warning"].ne("Normal"), "NIP"].nunique())})
    warning_trend = pd.DataFrame(warning_by_period)
    if not trend.empty and not warning_trend.empty:
        trend = trend.merge(warning_trend, on=["Tahun", "Bulan"], how="left")
    else: trend["Pegawai Warning"] = 0
    trend["Pegawai Warning"] = trend.get("Pegawai Warning", pd.Series(0, index=trend.index)).fillna(0).astype(int)
    comparison_index = None
    if not trend.empty:
        matches = trend.index[(trend["Bulan"] == month) & ((trend["Tahun"] == int(year)) if year != "Semua Tahun" else True)] if month != "Semua Bulan" else trend.index[-1:]
        if len(matches): comparison_index = int(matches[-1])
    attendance_delta = warning_delta = None
    previous_period = None
    if comparison_index is not None and comparison_index > 0:
        attendance_delta = float(trend.loc[comparison_index, "Kepatuhan"] - trend.loc[comparison_index - 1, "Kepatuhan"])
        warning_delta = int(trend.loc[comparison_index, "Pegawai Warning"] - trend.loc[comparison_index - 1, "Pegawai Warning"])
        previous_period = str(trend.loc[comparison_index - 1, "Periode"])

    warning_count = int(employees.loc[employees["Status Early Warning"].ne("Normal"), "NIP"].nunique())
    opd_count = int(executive_df["Unit Kerja"].nunique())
    attendance_delta_html = "" if attendance_delta is None else f"<div class='executive-delta {'good' if attendance_delta >= 0 else 'bad'}'>{'↑' if attendance_delta > 0 else '↓' if attendance_delta < 0 else '→'} {abs(attendance_delta):.1f} poin vs {escape(previous_period)}</div>"
    warning_delta_html = "" if warning_delta is None else f"<div class='executive-delta {'good' if warning_delta < 0 else 'bad' if warning_delta > 0 else 'neutral'}'>{'↑' if warning_delta > 0 else '↓' if warning_delta < 0 else '→'} {abs(warning_delta)} pegawai vs {escape(previous_period)}</div>"
    kpis = [
        (f"{metrics['total_employees']:,}".replace(",", "."), "Total Pegawai", ""),
        (f"{metrics['attendance_rate']:.1f}%".replace(".", ","), "Tingkat Kepatuhan Presensi", attendance_delta_html),
        (warning_count, "Pegawai Warning", warning_delta_html), (opd_count, "OPD Terpantau", ""),
    ]
    st.markdown("<section class='executive-section'><div class='executive-section-title'>Ringkasan Utama</div><div class='executive-section-subtitle'>Empat indikator organisasi pada scope aktif.</div><div class='executive-kpi-grid'>" + "".join(f"<div class='executive-card executive-interactive'><div class='executive-kpi-value'>{value}</div><div class='executive-kpi-label'>{label}</div>{delta}</div>" for value, label, delta in kpis) + "</div></section>", unsafe_allow_html=True)

    distribution = employees["Status Early Warning"].value_counts().reindex(WARNING_STATUSES, fill_value=0).rename_axis("Status").reset_index(name="Pegawai")
    distribution["Persentase"] = distribution["Pegawai"] / max(int(distribution["Pegawai"].sum()), 1) * 100
    risk_colors = {"Normal": "#10b981", "Perlu Perhatian": "#eab308", "Perlu Verifikasi": "#f97316", "Prioritas Tindak Lanjut": "#dc2626"}
    donut = alt.Chart(distribution).mark_arc(innerRadius=48, outerRadius=76).encode(theta="Pegawai:Q", color=alt.Color("Status:N", scale=alt.Scale(domain=list(risk_colors), range=list(risk_colors.values())), legend=None), tooltip=["Status:N", "Pegawai:Q", alt.Tooltip("Persentase:Q", format=".1f")]).properties(height=190)
    risk_rows = "".join(f"<div class='executive-risk-row'><span><i class='executive-risk-dot' style='background:{risk_colors[row.Status]}'></i>{escape(str(row.Status))}</span><span class='executive-risk-count'>{int(row.Pegawai)}</span><span class='executive-risk-pct'>{row.Persentase:.1f}%</span></div>" for row in distribution.itertuples())
    total_tk = int(executive_df["TK"].sum()); total_late = int(executive_df["Terlambat"].sum())
    condition_rows = [("Kepatuhan", f"{metrics['attendance_rate']:.1f}%".replace(".", ",")), ("Pegawai Warning", warning_count), ("TK", total_tk), ("Keterlambatan", total_late)]
    condition_html = "".join(f"<div class='executive-condition-row'><span>{label}</span><span class='executive-condition-value'>{value}</span></div>" for label, value in condition_rows)
    st.markdown("<section class='executive-section'><div class='executive-section-title'>Status Keseluruhan</div><div class='executive-section-subtitle'>Distribusi risiko dan kondisi ringkas periode aktif.</div></section>", unsafe_allow_html=True)
    overview_left, overview_right = st.columns([1.15, .85])
    with overview_left:
        st.markdown("<div class='executive-card'><div class='executive-section-title'>Distribusi Risiko</div>", unsafe_allow_html=True)
        chart_col, legend_col = st.columns([.85, 1.15]); chart_col.altair_chart(donut, use_container_width=True); legend_col.markdown(risk_rows, unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)
    with overview_right:
        st.markdown(f"<div class='executive-card'><div class='executive-section-title'>Kondisi Periode Ini</div><div style='margin-top:10px'>{condition_html}</div></div>", unsafe_allow_html=True)

    st.markdown("<section class='executive-section'><div class='executive-section-title'>Perubahan Kondisi</div><div class='executive-section-subtitle'>Arah kepatuhan dan jumlah pegawai warning berdasarkan data historis tersedia.</div></section>", unsafe_allow_html=True)
    if len(trend) < 2:
        st.markdown("<div class='executive-card'>Data tren belum tersedia untuk periode yang dipilih.</div>", unsafe_allow_html=True)
    else:
        line = alt.Chart(trend).mark_line(point=True, strokeWidth=3, color="#2563eb").encode(x=alt.X("Periode:N", sort=None, title=None), y=alt.Y("Kepatuhan:Q", title="Kepatuhan (%)", scale=alt.Scale(domain=[0, 100])), tooltip=["Periode:N", alt.Tooltip("Kepatuhan:Q", format=".1f", title="Kepatuhan"), alt.Tooltip("Pegawai Warning:Q", title="Pegawai Warning")])
        bars = alt.Chart(trend).mark_bar(color="#f59e0b", opacity=.35).encode(x=alt.X("Periode:N", sort=None), y=alt.Y("Pegawai Warning:Q", title="Pegawai Warning"), tooltip=["Periode:N", "Pegawai Warning:Q"])
        st.altair_chart(alt.layer(line, bars).resolve_scale(y="independent").properties(height=245), use_container_width=True)
        if attendance_delta is not None and warning_delta is not None:
            trend_insight = f"Kepatuhan {'meningkat' if attendance_delta > 0 else 'menurun' if attendance_delta < 0 else 'stabil'} {abs(attendance_delta):.1f} poin; pegawai warning {'bertambah' if warning_delta > 0 else 'berkurang' if warning_delta < 0 else 'tetap'} {abs(warning_delta)} dibanding {previous_period}."
            st.markdown(f"<div class='executive-trend-insight'>💡 {escape(trend_insight)}</div>", unsafe_allow_html=True)

    totals_by_opd = employees.groupby("Unit Kerja")["NIP"].nunique().rename("Total Pegawai")
    warning_by_opd = employees[employees["Status Early Warning"].ne("Normal")].groupby("Unit Kerja")["NIP"].nunique().rename("Pegawai Warning")
    opd_risk = pd.concat([totals_by_opd, warning_by_opd], axis=1).fillna(0)
    opd_risk["Persentase"] = opd_risk["Pegawai Warning"] / opd_risk["Total Pegawai"].replace(0, 1) * 100
    opd_risk = opd_risk.sort_values(["Persentase", "Pegawai Warning"], ascending=False)
    top_opd_name = str(opd_risk.index[0]) if not opd_risk.empty else None
    top_opd_pct = float(opd_risk.iloc[0]["Persentase"]) if not opd_risk.empty else 0
    highest_priority = int((employees["Status Early Warning"] == "Prioritas Tindak Lanjut").sum())
    priorities = []
    if highest_priority: priorities.append(("Prioritas Risiko Tertinggi", f"{highest_priority} pegawai berada pada kategori Prioritas Tindak Lanjut."))
    if warning_delta is not None and warning_delta > 0: priorities.append(("Pegawai Warning Bertambah", f"Jumlah pegawai warning bertambah {warning_delta} dibanding {previous_period}."))
    if top_opd_name: priorities.append(("Proporsi Warning OPD Tertinggi", f"{top_opd_name.title()} • {top_opd_pct:.1f}% pegawai warning."))
    if not priorities: priorities.append(("Kondisi Terkendali", "Tidak ada peningkatan indikator prioritas pada scope aktif."))
    priority_html = "".join(f"<div class='executive-card executive-priority-item executive-interactive'><div class='executive-priority-no'>{index:02d}</div><div class='executive-priority-title'>{escape(title)}</div><div class='executive-priority-text'>{escape(text)}</div></div>" for index, (title, text) in enumerate(priorities[:3], 1))
    st.markdown("<section class='executive-section'><div class='executive-section-title'>Perlu Perhatian Pimpinan</div><div class='executive-section-subtitle'>Maksimal tiga isu organisasi berdasarkan indikator existing.</div><div class='executive-priority'>" + priority_html + "</div></section>", unsafe_allow_html=True)
    nav_ews, nav_opd, _ = st.columns([1, 1.25, 4])
    if nav_ews.button("Lihat EWS →", key="executive_go_ews"):
        st.session_state["navigate_to_page"] = "Early Warning System"; st.rerun()
    if nav_opd.button("Lihat Analisis OPD →", key="executive_go_opd"):
        st.session_state["navigate_to_page"] = "Analisis OPD"; st.rerun()

    max_pct = max(float(opd_risk["Persentase"].max()) if not opd_risk.empty else 0, 1)
    opd_rows = "".join(f"<div class='executive-opd-row'><span class='executive-opd-rank'>{rank:02d}</span><span class='executive-opd-name'>{escape(str(name).title())}</span><span class='executive-opd-value'>{float(row['Persentase']):.1f}%</span><div class='executive-opd-track'><div class='executive-opd-fill' style='width:{float(row['Persentase'])/max_pct*100:.1f}%'></div></div></div>" for rank, (name, row) in enumerate(opd_risk.head(5).iterrows(), 1))
    st.markdown("<section class='executive-section'><div class='executive-section-title'>OPD dengan Perhatian Tertinggi</div><div class='executive-section-subtitle'>Top 5 berdasarkan proporsi pegawai warning pada scope aktif.</div><div class='executive-card executive-opd-ranking'>" + (opd_rows or "Tidak terdapat OPD dengan indikator warning.") + "</div></section>", unsafe_allow_html=True)

    insights = []
    if attendance_delta is not None: insights.append(f"{'↑' if attendance_delta > 0 else '↓' if attendance_delta < 0 else '→'} Kepatuhan {'meningkat' if attendance_delta > 0 else 'menurun' if attendance_delta < 0 else 'stabil'} {abs(attendance_delta):.1f} poin dibanding {previous_period}.")
    insights.append(f"⚠ {warning_count} pegawai berada pada kategori yang memerlukan perhatian.")
    risky_opds = int((opd_risk["Pegawai Warning"] > 0).sum())
    insights.append(f"🏢 {risky_opds} OPD memiliki sedikitnya satu pegawai dengan indikator warning.")
    st.markdown("<section class='executive-section'><div class='executive-section-title'>Executive Insight</div><div class='executive-section-subtitle'>Ringkasan singkat untuk arah monitoring berikutnya.</div>" + "".join(f"<div class='executive-insight'>{escape(text)}</div>" for text in insights[:3]) + "</section>", unsafe_allow_html=True)


def show_early_warning_page_centralized() -> None:
    """UI EWS aktif berbasis employee_warning_summary, tanpa Risk Score."""
    inject_dashboard_css()
    data = apply_employee_active_status(load_employee_data())
    if data.empty:
        st.info("Data presensi belum tersedia.")
        return
    years = sorted(pd.to_numeric(data["Tahun"], errors="coerce").dropna().astype(int).unique().tolist())
    opds = sorted(data["Unit Kerja"].dropna().astype(str).unique().tolist())
    employee_types = [value for value in ["PNS", "PPPK", "Belum Diketahui"] if value in set(data.get("Jenis Pegawai", pd.Series(dtype=str)).astype(str))]

    st.markdown("""
    <style>
    .central-ews-title{font-size:28px;font-weight:700;color:#173b63}.central-ews-subtitle{font-size:14px;color:#64748b;margin:4px 0 16px}.central-ews-kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0 24px}.central-ews-card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:16px 18px;box-shadow:0 3px 12px rgba(15,23,42,.035)}.central-ews-value{font-size:28px;font-weight:750;color:#173b63}.central-ews-label{font-size:12px;color:#64748b;margin-top:5px}
    .ews-premium-card{position:relative;overflow:hidden;background:#fff;border:1px solid #e6ecf3;border-radius:18px;padding:20px 22px;margin:12px 0 8px;box-shadow:0 6px 20px rgba(15,23,42,.045);transition:transform .2s ease,box-shadow .2s ease,border-color .2s ease}.ews-premium-card:hover{transform:translateY(-2px);box-shadow:0 10px 28px rgba(15,23,42,.07);border-color:#d8e1ec}.ews-risk-accent{position:absolute;inset:0 auto 0 0;width:4px;border-radius:18px 0 0 18px}.ews-risk-normal .ews-risk-accent{background:#10b981}.ews-risk-attention .ews-risk-accent{background:#eab308}.ews-risk-verification .ews-risk-accent{background:#f97316}.ews-risk-priority .ews-risk-accent{background:#dc2626}
    .ews-premium-header{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:16px;align-items:start}.ews-identity{display:grid;grid-template-columns:32px minmax(0,1fr);gap:10px;align-items:start;min-width:0}.ews-rank{font-size:12px;font-weight:700;letter-spacing:.08em;color:#94a3b8;padding-top:4px}.ews-name{font-size:19px;font-weight:700;letter-spacing:-.01em;line-height:1.25;color:#173b63}.ews-opd{max-width:70%;font-size:13px;font-weight:500;line-height:1.45;color:#64748b;margin:6px 0 0 42px;text-transform:none}.ews-status-badge{display:inline-flex;align-items:center;border:1px solid;border-radius:999px;padding:6px 11px;font-size:12px;font-weight:650;line-height:1.2;white-space:nowrap}.ews-risk-normal .ews-status-badge{background:#ecfdf5;color:#047857;border-color:#a7f3d0}.ews-risk-attention .ews-status-badge{background:#fefce8;color:#a16207;border-color:#fde68a}.ews-risk-verification .ews-status-badge{background:#fff7ed;color:#c2410c;border-color:#fed7aa}.ews-risk-priority .ews-status-badge{background:#fef2f2;color:#b91c1c;border-color:#fecaca}
    .ews-metrics{display:flex;gap:11px;margin:17px 0 0 42px;flex-wrap:wrap}.ews-mini-metric{min-width:140px;background:#f8fafc;border:1px solid #e8eef5;border-radius:12px;padding:11px 14px}.ews-metric-label{font-size:10.5px;font-weight:650;letter-spacing:.06em;color:#94a3b8}.ews-metric-value{font-size:21px;font-weight:750;line-height:1.25;color:#173b63;margin-top:3px}.ews-rule-summary{display:flex;gap:18px;flex-wrap:wrap;margin:12px 0 0 42px;font-size:12px;color:#64748b}.ews-rule-summary strong{color:#334155}.ews-footer{display:flex;justify-content:space-between;align-items:center;gap:16px;border-top:1px solid #eef2f7;margin-top:16px;padding-top:12px;font-size:12px;font-weight:500}.ews-change{color:#64748b}.ews-change.improved{color:#15803d}.ews-change.worsened{color:#b91c1c}.ews-trend-chip{display:inline-flex;border-radius:999px;padding:5px 9px;font-size:11px;font-weight:650;white-space:nowrap;background:#f8fafc;color:#64748b}.ews-trend-chip.improved{background:#f0fdf4;color:#15803d}.ews-trend-chip.worsened{background:#fef2f2;color:#b91c1c}.central-ews-note{font-size:11px;color:#64748b;border-top:1px solid #e2e8f0;padding-top:12px;margin-top:24px}
    @media(max-width:800px){.central-ews-kpis{grid-template-columns:repeat(2,1fr)}}@media(max-width:640px){.ews-premium-card{padding:18px}.ews-premium-header{grid-template-columns:1fr}.ews-status-badge{margin-left:42px;width:max-content}.ews-opd{max-width:none}.ews-metrics{display:grid;grid-template-columns:1fr 1fr}.ews-mini-metric{min-width:0}.ews-footer{align-items:flex-start;flex-direction:column;gap:8px}}@media(max-width:420px){.ews-metrics{grid-template-columns:1fr}}
    </style>
    """, unsafe_allow_html=True)
    st.markdown("<h1 class='central-ews-title'>Early Warning System</h1><div class='central-ews-subtitle'>Identifikasi pegawai berdasarkan status EWS terpusat dan perubahan TK antarperiode.</div>", unsafe_allow_html=True)
    filters = st.columns([1, 1, 1.7, 1.35, 1.5])
    today = date.today()
    default_year = max(years)
    with filters[0]: year = st.selectbox("Tahun", years, index=years.index(default_year), key="central_ews_year")
    year_data = data[pd.to_numeric(data["Tahun"], errors="coerce").eq(int(year))]
    available_months = [month_name for month_name in ANALYTICS_MONTHS if month_name in set(year_data["Bulan"].astype(str))]
    default_month = available_months[-1] if available_months else ANALYTICS_MONTHS[-1]
    with filters[1]: month = st.selectbox("Bulan", ANALYTICS_MONTHS, index=ANALYTICS_MONTHS.index(default_month), key="central_ews_month_uploaded_default")
    with filters[2]: opd = st.selectbox("OPD", ["Semua OPD"] + opds, key="central_ews_opd")
    with filters[3]: employee_type = st.selectbox("Jenis Pegawai", ["Semua Jenis Pegawai"] + employee_types, key="central_ews_type")
    with filters[4]: status_filter = st.selectbox("Status Early Warning", ["Semua Status"] + WARNING_STATUSES, key="central_ews_status")
    scoped_all_years = data.copy()
    if opd != "Semua OPD": scoped_all_years = scoped_all_years[scoped_all_years["Unit Kerja"].eq(opd)]
    if employee_type != "Semua Jenis Pegawai": scoped_all_years = scoped_all_years[scoped_all_years["Jenis Pegawai"].eq(employee_type)]
    period = ews_period_context(int(year), month)
    previous_month = str(period["previous_month"])
    scoped = scoped_all_years[pd.to_numeric(scoped_all_years["Tahun"], errors="coerce").eq(int(year))]
    current_data = scoped[scoped["Bulan"].eq(month)].copy()
    if current_data.empty:
        st.info(f"Data {month} {year} belum tersedia. Pilih bulan lain yang sudah memiliki data presensi.")
        return
    previous_data = scoped_all_years[
        pd.to_numeric(scoped_all_years["Tahun"], errors="coerce").eq(int(period["previous_year"]))
        & scoped_all_years["Bulan"].eq(previous_month)
    ].copy()
    daily_rule_source = _load_excel_daily_data(_excel_source_signature())
    if opd != "Semua OPD": daily_rule_source = daily_rule_source[daily_rule_source["Unit Kerja"].eq(opd)]
    if employee_type != "Semua Jenis Pegawai": daily_rule_source = daily_rule_source[daily_rule_source["Jenis Pegawai"].eq(employee_type)]
    daily_rule_source = daily_rule_source.copy()
    daily_rule_source["_ews_date"] = pd.to_datetime(daily_rule_source["Tanggal"], errors="coerce")

    def with_daily_monthly_tk(monthly_rows: pd.DataFrame, period_year: int, period_month: str) -> pd.DataFrame:
        result = monthly_rows.copy()
        if result.empty:
            return result
        month_number = ANALYTICS_MONTHS.index(period_month) + 1
        daily_month = daily_rule_source[
            daily_rule_source["_ews_date"].dt.year.eq(int(period_year))
            & daily_rule_source["_ews_date"].dt.month.eq(month_number)
            & daily_rule_source["wajib_presensi"].fillna(False).astype(bool)
            & daily_rule_source["eligible_tk"].fillna(False).astype(bool)
        ]
        monthly_values = (
            daily_month.groupby(["NIP", "_ews_date"], dropna=False)["TK"].max()
            .groupby(level="NIP").sum().astype(int)
        )
        mapped = result["NIP"].astype(str).map(monthly_values)
        result.loc[mapped.notna(), "TK"] = mapped[mapped.notna()].astype(int)
        return result

    current_data = with_daily_monthly_tk(current_data, int(year), month)
    previous_data = with_daily_monthly_tk(previous_data, int(period["previous_year"]), previous_month)
    current_daily = daily_rule_source[
        daily_rule_source["_ews_date"].dt.year.eq(int(year))
        & daily_rule_source["_ews_date"].dt.month.eq(int(period["month_number"]))
    ]
    max_date = pd.to_datetime(current_daily["Tanggal"], errors="coerce").max()
    if pd.notna(max_date):
        st.caption(f"Bulan Aktif: {month} {year} • Data s.d. {max_date.strftime('%d %b %Y')}")
    ews = employee_warning_summary(current_data, previous_data if not previous_data.empty else None)
    if status_filter != "Semua Status": ews = ews[ews["Status Early Warning"].eq(status_filter)]
    if ews.empty:
        st.info("Tidak terdapat data EWS pada kombinasi filter yang dipilih.")
        return
    warning = ews[ews["Status Early Warning"].ne("Normal")].copy()
    comparable = not previous_data.empty
    kpis = [
        (warning["NIP"].nunique(), "Pegawai Warning"),
        (int(ews["Warning Baru"].sum()) if comparable else "Belum tersedia", "Warning Baru"),
        (int(ews["Tren"].eq("Memburuk").sum()) if comparable else "Belum tersedia", "Kondisi Memburuk"),
        (int(ews["Tren"].eq("Membaik").sum()) if comparable else "Belum tersedia", "Kondisi Membaik"),
    ]
    st.markdown("<div class='central-ews-kpis'>" + "".join(f"<div class='central-ews-card'><div class='central-ews-value'>{value}</div><div class='central-ews-label'>{label}</div></div>" for value, label in kpis) + "</div>", unsafe_allow_html=True)
    st.markdown("<div class='section-title'>Daftar Prioritas</div>", unsafe_allow_html=True)
    priorities = warning.sort_values(["Prioritas Status", "TK", "Nama Pegawai"], ascending=[False, False, True])
    if priorities.empty:
        st.success("Tidak terdapat pegawai dengan status warning pada scope aktif.")
    for rank, (_, employee) in enumerate(priorities.head(10).iterrows(), 1):
        delta = employee["Delta TK"]
        monthly_tk_days = int(employee["TK"])
        previous_month_tk_days = None if pd.isna(employee["TK Sebelumnya"]) else int(employee["TK Sebelumnya"])
        employee_history = scoped[scoped["NIP"].astype(str).eq(str(employee["NIP"]))]
        employee_type_value = str(employee_history["Jenis Pegawai"].iloc[0]) if "Jenis Pegawai" in employee_history and not employee_history.empty else "Belum Diketahui"
        daily_employee = daily_rule_source[daily_rule_source["NIP"].astype(str).eq(str(employee["NIP"]))]
        annual_tk_days = calculate_ytd_tk(daily_employee, int(year), month)
        consecutive_tk_days = calculate_consecutive_unexcused_days(daily_employee, int(year), month)
        monitoring = evaluate_attendance_monitoring_rule(
            annual_tk_days, consecutive_tk_days, consecutive_tk_days is not None,
            nip=str(employee["NIP"]), employee_type=employee_type_value,
            period_end=f"{month} {year}", data_granularity="DAILY",
        )
        trend = str(employee["Tren"])
        trend_class = "improved" if trend == "Membaik" else "worsened" if trend == "Memburuk" else "stable"
        if pd.isna(delta):
            change_text = "Data pembanding belum tersedia"
        elif int(delta) < 0:
            change_text = f"↓ {abs(int(delta))} hari TK dibanding {previous_month}"
        elif int(delta) > 0:
            change_text = f"↑ {abs(int(delta))} hari TK dibanding {previous_month}"
        else:
            change_text = f"→ Stabil dibanding {previous_month}"
        if bool(employee["Warning Baru"]): change_text = f"Warning Baru · {change_text}"
        risk_class = {"Normal": "ews-risk-normal", "Perlu Perhatian": "ews-risk-attention", "Perlu Verifikasi": "ews-risk-verification", "Prioritas Tindak Lanjut": "ews-risk-priority"}.get(str(employee["Status Early Warning"]), "ews-risk-attention")
        monthly_label = "TK BULAN INI" if int(year) == today.year and int(period["month_number"]) == today.month else f"TK {month.upper()}"
        st.markdown(f"<article class='ews-premium-card {risk_class}'><span class='ews-risk-accent' aria-hidden='true'></span><header class='ews-premium-header'><div><div class='ews-identity'><span class='ews-rank'>{rank:02d}</span><div class='ews-name'>{escape(str(employee['Nama Pegawai']))}</div></div><div class='ews-opd'>{escape(str(employee['Unit Kerja']).title())}</div></div><span class='ews-status-badge'>{escape(str(employee['Status Early Warning']))}</span></header><div class='ews-metrics'><div class='ews-mini-metric'><div class='ews-metric-label'>{monthly_label}</div><div class='ews-metric-value'>{monthly_tk_days} hari</div></div><div class='ews-mini-metric'><div class='ews-metric-label'>TERLAMBAT</div><div class='ews-metric-value'>{int(employee['Terlambat'])} kali</div></div></div><div class='ews-rule-summary'><span><strong>Status Monitoring Tahunan:</strong> {escape(str(monitoring['reference_status']))}</span><span><strong>Ambang:</strong> {escape(str(monitoring['reference_band']))}</span></div><footer class='ews-footer'><span class='ews-change {trend_class}'>{escape(change_text)}</span><span class='ews-trend-chip {trend_class}'>{escape(trend)}</span></footer></article>", unsafe_allow_html=True)
        with st.expander("Lihat Dasar Penilaian Rule-Based"):
            consecutive_label = f"{consecutive_tk_days} hari kerja" if consecutive_tk_days is not None else "Data belum mendukung pemeriksaan ketidakhadiran 10 hari kerja berturut-turut."
            st.markdown("**DASAR PENILAIAN**")
            st.write(f"Jenis Pegawai: {employee_type_value}")
            st.write(f"TK Bulan Aktif: {monthly_tk_days} hari")
            st.write(f"TK Bulan Sebelumnya: {previous_month_tk_days if previous_month_tk_days is not None else 'Data belum tersedia'}")
            st.write(f"Akumulasi TK Tahun Berjalan: {annual_tk_days} hari")
            st.write(f"TK Berturut-turut: {consecutive_label}")
            st.write(f"Ambang Referensi: {monitoring['reference_band']}")
            st.write(f"Status Monitoring: {monitoring['reference_status']}")
            st.write(f"Dasar Monitoring: {monitoring['regulation']}")
            st.write(f"Referensi: {monitoring['reference_article'] or 'Monitoring awal'}")
            st.write(f"Catatan: {monitoring['monitoring_note']}")
            st.caption(MONITORING_DISCLAIMER)
        if st.button(f"Lihat Detail {employee['Nama Pegawai']}", key=f"central_ews_detail_{employee['NIP']}"):
            st.session_state["employee_detail_opd"] = str(employee["Unit Kerja"])
            st.session_state["employee_detail_employee"] = next((label for label in [f"{employee['Nama Pegawai']} — {employee['NIP']}"]), "")
            st.session_state["navigate_to_page"] = "Detail Pegawai"
            st.rerun()
    st.markdown(f"<div class='central-ews-note'>{escape(MONITORING_DISCLAIMER)} Keterlambatan tetap ditampilkan sebagai indikator tambahan dan tidak dikonversi menjadi hari ketidakhadiran.</div>", unsafe_allow_html=True)


def show_tk_report_page() -> None:
    """Pusat preview dan ekspor laporan administratif TK (read-only)."""
    inject_dashboard_css()
    data = load_employee_data().copy()
    data["NIP"] = data["NIP"].astype(str)
    years = sorted(pd.to_numeric(data["Tahun"], errors="coerce").dropna().astype(int).unique(), reverse=True)
    if not years:
        st.warning("Data tahun laporan tidak tersedia.")
        return
    default_year = int(years[0])
    latest_months = data[pd.to_numeric(data["Tahun"], errors="coerce").eq(default_year)]["Bulan"].map(
        {name: int(number) for number, name in MONTH_NAMES.items()}
    ).dropna()
    latest_month = int(latest_months.max()) if not latest_months.empty else 1
    default_period = f"TW {('I', 'II', 'III', 'IV')[(latest_month - 1) // 3]}"
    period_codes = {"TW I": "TRIWULAN_I", "TW II": "TRIWULAN_II", "TW III": "TRIWULAN_III", "TW IV": "TRIWULAN_IV", "Tahunan": "TAHUNAN"}

    defaults = {
        "tk_report_year": default_year, "tk_report_period": default_period,
        "tk_report_opd": "Semua OPD", "tk_report_employee_type": "Semua Jenis Pegawai",
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)

    st.markdown("""
    <style>
      .report-page{color:#0f172a;max-width:1480px;margin:0 auto}.report-header{margin:0 0 24px}.report-header h1{font-size:28px;margin:0;font-weight:700;color:#173b63}.report-header p{font-size:14px;color:#64748b;margin:6px 0 0;max-width:760px;line-height:1.55}
      .report-parameter-card,.report-preview-card,.report-export-card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;box-shadow:0 3px 12px rgba(15,23,42,.04);padding:18px 20px}.report-parameter-card{margin-bottom:24px}.report-section-title{font-size:17px;font-weight:750;color:#173b63;margin:0 0 12px;letter-spacing:.02em}
      .report-summary-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:12px 0}.report-summary-card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:17px 19px}.report-summary-value{font-size:31px;font-weight:750;color:#173b63;line-height:1}.report-summary-label{font-size:13px;color:#64748b;margin-top:7px}.report-meta{font-size:12px;font-weight:650;color:#64748b;margin:10px 0 24px;text-transform:uppercase;letter-spacing:.04em}
      .report-preview-card{max-height:560px;overflow:auto;padding:28px;margin-bottom:8px}.report-document-title{text-align:center;font-size:14px;font-weight:750;line-height:1.45;color:#0f172a}.report-document-period{text-align:center;font-size:12px;font-weight:700;margin:10px 0 18px}.report-table{border-collapse:collapse;width:100%;min-width:820px;font-size:11px}.report-table th,.report-table td{border:1px solid #cbd5e1;padding:7px 8px;vertical-align:middle}.report-table th{background:#eaf0f7;color:#173b63;text-align:center}.report-table .opd-row td{background:#f1f5f9;font-weight:750;color:#173b63}.report-preview-note{font-size:12px;color:#64748b;margin:8px 0 24px}.report-export-card{margin-top:24px}.report-export-title{font-size:17px;font-weight:750;color:#173b63}.report-export-meta{font-size:12px;color:#64748b;margin-top:5px}
      [class*="st-key-tk_page_excel"] button,[class*="st-key-tk_page_pdf"] button{border-radius:9px;border:1px solid #bfdbfe;background:#fff;color:#173b63;transition:.18s;min-height:38px}[class*="st-key-tk_page_excel"] button:hover,[class*="st-key-tk_page_pdf"] button:hover{transform:translateY(-1px);border-color:#2563eb;box-shadow:0 5px 12px rgba(37,99,235,.10)}
      [class*="st-key-tk_report_parameter_card"]{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:16px 20px;box-shadow:0 3px 12px rgba(15,23,42,.04);margin-bottom:24px}
      [class*="st-key-tk_report_period"] [data-testid="stBaseButton-segmented_controlActive"]{background:#173b63;color:#fff;border-color:#173b63}
      @media(max-width:700px){.report-summary-grid{grid-template-columns:1fr}.report-preview-card{padding:15px}.report-header h1{font-size:26px}}
    </style><main class='report-page'><header class='report-header'><h1>Laporan Ketidakhadiran</h1><p>Menyajikan rekapitulasi ketidakhadiran pegawai berdasarkan OPD dan periode untuk kebutuhan monitoring, evaluasi, dan administrasi.</p></header></main>
    """, unsafe_allow_html=True)

    def reset_report_parameters() -> None:
        for key, value in defaults.items(): st.session_state[key] = value

    with st.container(border=True, key="tk_report_parameter_card"):
        st.markdown("<div class='report-section-title'>Parameter Laporan</div>", unsafe_allow_html=True)
        filters = st.columns([1, 2, 1.35])
        with filters[0]:
            chosen_year = st.selectbox("Tahun", years, key="tk_report_year")
        year_scope = data[pd.to_numeric(data["Tahun"], errors="coerce").eq(chosen_year)]
        with filters[1]:
            chosen_opd = st.selectbox("OPD", ["Semua OPD"] + sorted(year_scope["Unit Kerja"].dropna().astype(str).unique()), key="tk_report_opd")
        present_types = set(year_scope.get("Jenis Pegawai", pd.Series(dtype=str)).dropna().astype(str))
        employee_types = [item for item in ["PNS", "PPPK", "Belum Diketahui"] if item in present_types]
        with filters[2]:
            chosen_type = st.selectbox("Jenis Pegawai", ["Semua Jenis Pegawai"] + employee_types, key="tk_report_employee_type")
        period_row = st.columns([5, .7])
        with period_row[0]:
            chosen_period = st.segmented_control("Periode", list(period_codes), key="tk_report_period")
        with period_row[1]:
            st.write("")
            st.button("Reset", key="tk_report_reset", on_click=reset_report_parameters, width="stretch")

    report_source = apply_filters(
        data,
        year=chosen_year,
        opd=chosen_opd,
        employee_type=chosen_type,
    )
    try:
        report_data = prepare_tk_report_data(report_source, chosen_type, period_codes[chosen_period])
    except Exception:
        LOGGER.exception("Gagal menyiapkan laporan TK")
        st.error("Laporan belum dapat ditampilkan. Silakan coba kembali.")
        return
    month_names = [MONTH_NAMES[f"{month:02d}"] for month in report_data["active_months"]]
    range_short = f"{month_names[0][:3]}–{month_names[-1][:3]} {chosen_year}"
    period_meta = f"{report_data['period_name']} • {range_short}"
    if report_data["grand_total_tk"] <= 0:
        st.success("✓ Tidak terdapat data ketidakhadiran TK pada parameter laporan yang dipilih.\n\nCoba pilih periode atau OPD lainnya.")

    st.markdown("<div class='report-section-title'>Preview Laporan</div>", unsafe_allow_html=True)
    preview_limit, shown = 25, 0
    all_report_months = report_data["months"]
    header_months = "".join(f"<th>{MONTH_SHORT[month - 1]}</th>" for month in all_report_months)
    rows = []
    for opd_index, opd_item in enumerate(report_data["opds"], 1):
        available = preview_limit - shown
        selected_employees = opd_item["employees"][:max(available, 0)]
        colspan = 6 + len(all_report_months)
        rows.append(f"<tr class='opd-row'><td>{opd_index}</td><td colspan='{colspan - 1}'>{escape(opd_item['opd_name'].upper())} • TOTAL TK {opd_item['total_tk']}</td></tr>")
        for number, employee in enumerate(selected_employees, 1):
            values = "".join(f"<td style='text-align:center'>{employee['monthly_tk'][month] or '-'}</td>" for month in all_report_months)
            rows.append(f"<tr><td style='text-align:center'>{number}</td><td>{escape(opd_item['opd_name'])}</td><td>{escape(employee['nama'])}</td><td>{escape(employee['nip'])}</td>{values}<td style='text-align:center'>{employee['total_tk']}</td><td>{escape(employee['keterangan'])}</td></tr>")
        shown += len(selected_employees)
    st.markdown(f"<section class='report-preview-card'><div style='font-size:11px;line-height:1.5;margin-bottom:18px'>Lampiran Surat Sekretaris Daerah Provinsi Kalimantan Barat<br>Nomor&nbsp;&nbsp;: __________________<br>Tanggal: __________________</div><div class='report-document-title'>{escape(report_data['title']).replace(chr(10), '<br>')}</div><div class='report-document-period'>{escape(report_data['period_label'])}<br>{escape(report_data['period_text'])}</div><table class='report-table'><thead><tr><th rowspan='3'>NO</th><th rowspan='3'>NAMA UNIT KERJA</th><th rowspan='3'>NAMA</th><th rowspan='3'>NIP</th><th colspan='12'>JUMLAH KETIDAKHADIRAN MASUK KERJA<br>TANPA KETERANGAN (TK)</th><th rowspan='3'>TOTAL</th><th rowspan='3'>KETERANGAN</th></tr><tr><th colspan='3'>TRIWULAN I</th><th colspan='3'>TRIWULAN II</th><th colspan='3'>TRIWULAN III</th><th colspan='3'>TRIWULAN IV</th></tr><tr>{header_months}</tr></thead><tbody>{''.join(rows)}</tbody></table></section><div class='report-preview-note'>Preview menampilkan {shown} dari {report_data['total_employees']} pegawai. File unduhan memuat seluruh data.</div>", unsafe_allow_html=True)

    try:
        excel_bytes = generate_tk_excel(report_data)
        pdf_bytes = generate_tk_pdf(report_data)
    except Exception:
        LOGGER.exception("Gagal membuat file ekspor laporan TK")
        st.error("File laporan belum dapat dibuat. Silakan coba kembali.")
        return
    st.markdown(f"<section class='report-export-card'><div class='report-export-title'>Siap Mengunduh Laporan</div><div class='report-export-meta'>{escape(period_meta)}<br>{report_data['total_opd']} OPD • {report_data['total_employees']} Pegawai • {report_data['grand_total_tk']} Hari TK</div></section>", unsafe_allow_html=True)
    downloads = st.columns(2)
    with downloads[0]:
        st.download_button("⬇ Unduh Excel", excel_bytes, generate_tk_filename(report_data, "xlsx", chosen_opd), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="tk_page_excel", width="stretch", on_click=_record_report_download, args=("EXCEL", report_data["period_name"] + " " + str(chosen_year), chosen_opd))
    with downloads[1]:
        st.download_button("📄 Unduh PDF", pdf_bytes, generate_tk_filename(report_data, "pdf", chosen_opd), "application/pdf", key="tk_page_pdf", width="stretch", on_click=_record_report_download, args=("PDF", report_data["period_name"] + " " + str(chosen_year), chosen_opd))


def show_work_calendar_page() -> None:
    """Tampilan read-only master kalender dan hasil validasi source presensi."""
    inject_dashboard_css()
    data = _load_excel_daily_data(_excel_source_signature())
    years = sorted(pd.to_datetime(data.get("Tanggal"), errors="coerce").dropna().dt.year.unique(), reverse=True)
    default_year = int(years[0]) if years else datetime.now().year
    st.markdown("<h1 style='font-size:28px;font-weight:700;color:#173b63;margin:0'>Master Kalender Kerja</h1><div style='font-size:14px;color:#64748b;margin:6px 0 20px'>Validasi hari kerja, kewajiban presensi, dan kelayakan TK berdasarkan kalender resmi instansi.</div>", unsafe_allow_html=True)
    selected_year = st.selectbox("Tahun", years or [default_year], key="work_calendar_year")
    try:
        calendar = build_work_calendar(int(selected_year))
        overrides = load_calendar_overrides(int(selected_year))
    except Exception:
        LOGGER.exception("Master kalender kerja tidak valid")
        st.error("Master kalender belum dapat ditampilkan. Periksa format dan duplikasi tanggal pada file kalender.")
        return
    if not overrides.empty:
        holiday_count = int(overrides["jenis_hari"].eq("LIBUR_NASIONAL").sum())
        collective_count = int(overrides["jenis_hari"].eq("CUTI_BERSAMA").sum())
        source_path = str(overrides.attrs.get("source_path", ""))
        log_system_activity(
            "CALENDAR_LOAD_SUCCESS", "Kalender kerja berhasil dimuat",
            f"Tahun {int(selected_year)} • {holiday_count} Libur Nasional • {collective_count} Cuti Bersama",
            {"year": int(selected_year), "national_holidays": holiday_count, "collective_leave": collective_count},
            module="kalender", dedupe_key=f"calendar-load:{selected_year}:{source_path}:{len(overrides)}",
        )
    if calendar.attrs.get("warning"):
        st.warning(calendar.attrs["warning"])
    elif not overrides.empty:
        st.success(
            f"✓ Kalender Kerja {int(selected_year)} berhasil dimuat\n\n"
            f"{int(overrides['jenis_hari'].eq('LIBUR_NASIONAL').sum())} Libur Nasional • "
            f"{int(overrides['jenis_hari'].eq('CUTI_BERSAMA').sum())} Cuti Bersama\n\n"
            f"Sumber: {overrides.iloc[0].get('source', '-') or '-'}"
        )
    audit = overrides.attrs.get("extraction_audit", {})
    if audit:
        st.caption(
            f"Dokumen: {audit.get('document', '-')} • Halaman: {audit.get('page_count', '-')} • "
            f"Text layer: {'ADA' if audit.get('has_text_layer') else 'TIDAK ADA'} • "
            f"OCR digunakan: {'YA' if audit.get('ocr_used') else 'TIDAK'} • "
            f"Gagal dipahami: {audit.get('unresolved_count', 0)} • "
            f"Duplicate: {audit.get('duplicate_count', 0)} • Conflict: {audit.get('conflict_count', 0)}"
        )
    metrics = [
        (int(calendar["is_hari_kerja"].sum()), "Hari Kerja Kalender"),
        (int(calendar["wajib_presensi"].sum()), "Hari Wajib Presensi"),
        (int(calendar["jenis_hari"].eq("LIBUR_NASIONAL").sum()), "Libur Nasional"),
        (int(calendar["jenis_hari"].eq("CUTI_BERSAMA").sum()), "Cuti Bersama"),
    ]
    for column, (value, label) in zip(st.columns(4), metrics): column.metric(label, value)
    st.caption("Cuti Bersama tetap termasuk Hari Kerja Kalender, tetapi tidak wajib presensi dan tidak eligible TK.")
    st.markdown("<div class='section-title'>Kalender Hari Khusus</div>", unsafe_allow_html=True)
    if overrides.empty:
        st.info("Tanggal Libur Nasional dan Cuti Bersama belum diisi. Gunakan data resmi SE, tanpa menambahkan tanggal contoh.")
    else:
        special = overrides[["tanggal", "jenis_hari", "keterangan", "is_hari_kerja", "wajib_presensi", "eligible_tk", "dasar_hukum"]].copy()
        special["tanggal"] = special["tanggal"].dt.strftime("%d-%m-%Y")
        st.dataframe(special, hide_index=True, width="stretch")
    year_data = data[pd.to_datetime(data["Tanggal"], errors="coerce").dt.year.eq(int(selected_year))]
    conflicts = year_data[year_data.get("anomali_kalender_presensi", pd.Series(False, index=year_data.index)).fillna(False)]
    st.markdown("<div class='section-title'>Validasi Kalender Presensi</div>", unsafe_allow_html=True)
    if conflicts.empty:
        st.success("Tidak ditemukan konflik status TK resmi dengan kalender yang tersedia.")
    else:
        st.warning(f"{len(conflicts)} record berstatus TK pada tanggal yang tidak eligible TK. Data resmi tidak diubah dan perlu ditinjau.")
        detail = conflicts[["Tanggal", "NIP", "Nama", "Unit Kerja", "Status", "jenis_hari", "nama_libur"]].copy()
        st.dataframe(detail, hide_index=True, width="stretch")


initialize_session()
if not st.session_state.is_logged_in:
    show_login_page()
    st.stop()

inject_dashboard_css()

navigation_target = st.session_state.pop("navigate_to_page", None)
if navigation_target:
    st.session_state["page_navigation"] = navigation_target

with st.sidebar:
    st.markdown(
        "<div class='sidebar-brand'><div class='brand-icon'>🛡️</div><div><div class='brand-name'>EWS PRESENSI</div><div class='brand-subtitle'>Early Warning System Kehadiran Pegawai</div></div></div>",
        unsafe_allow_html=True,
    )
    st.markdown("<div class='sidebar-nav-title'>Menu Utama</div>", unsafe_allow_html=True)
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"] [data-testid="stRadioOption"] > div > div:first-child {
            display: none !important;
        }
        [data-testid="stSidebar"] [data-testid="stRadioOption"] > div {
            gap: 0 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    page_options = {
        "🏠 Executive Dashboard": "Executive Dashboard",
        "🚨 Early Warning System": "Early Warning System",
        "📊 Analisis Presensi": "Analisis Presensi",
        "🏢 Analisis OPD": "Analisis OPD",
        "👤 Detail Pegawai": "Detail Pegawai",
        "📄 Laporan Ketidakhadiran": "Laporan Ketidakhadiran",
        "📅 Master Kalender Kerja": "Master Kalender Kerja",
        "🏛️ Master OPD / Dinas": "Master OPD",
        "👥 Master Data Pegawai": "Master Pegawai",
        "📋 Data Presensi & Periode": "Data Presensi",
        "✅ Action Center": "Action Center",
        "🕒 Audit Trail": "Audit Trail",
        "🔐 Manajemen Pengguna": "Manajemen Pengguna",
    }
    if navigation_target:
        navigation_target = next((label for label, value in page_options.items() if value == navigation_target), navigation_target)
        st.session_state["page_navigation"] = navigation_target
    selected_page_label = st.radio(
        "Pilih halaman",
        list(page_options),
        label_visibility="collapsed",
        key="page_navigation",
    )
selected_page = page_options[selected_page_label]

if selected_page == "Executive Dashboard":
    show_dashboard_focus()
elif selected_page == "Early Warning System":
    show_early_warning_page_centralized()
elif selected_page == "Analisis Presensi":
    show_attendance_analysis_page()
elif selected_page == "Analisis OPD":
    show_opd_analysis_page()
elif selected_page == "Detail Pegawai":
    show_employee_detail_page()
elif selected_page == "Laporan Ketidakhadiran":
    show_tk_report_page()
elif selected_page == "Master Kalender Kerja":
    show_work_calendar_page()
elif selected_page == "Master OPD":
    show_opd_management_page(get_engine())
elif selected_page == "Master Pegawai":
    show_pegawai_management_page(get_engine())
elif selected_page == "Data Presensi":
    show_presensi_data_page(get_engine())
elif selected_page == "Action Center":
    show_action_center_page_focus()
elif selected_page == "Manajemen Pengguna":
    show_user_management_page(get_engine())
else:
    show_audit_trail_page()
