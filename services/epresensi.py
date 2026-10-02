"""Sumber data ePresensi; tidak berisi komponen tampilan dashboard."""

from datetime import datetime
from io import StringIO
import logging
import os

import pandas as pd
import requests
import streamlit as st

LOGGER = logging.getLogger(__name__)
BASE_URL = "https://epresensi.kalbarprov.go.id"
OPD_TARGET = (11022, 11252, 12172, 11092, 11412)
MONTH_NAMES = {"01": "Januari", "02": "Februari", "03": "Maret", "04": "April", "05": "Mei", "06": "Juni", "07": "Juli", "08": "Agustus", "09": "September", "10": "Oktober", "11": "November", "12": "Desember"}
OFFICER_COLUMNS = ("id", "pin_id", "name", "opd_id", "nip", "status_worker")


class EPresensiAuthenticationError(RuntimeError):
    """Endpoint daftar pegawai menolak sesi yang belum terautentikasi."""


def _headers() -> dict[str, str]:
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/javascript,text/html,*/*;q=0.01", "X-Requested-With": "XMLHttpRequest"}
    token = os.getenv("EPRESENSI_BEARER_TOKEN", "").strip()
    cookie = os.getenv("EPRESENSI_COOKIE", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _datatable_params(opd_id: int, start: int, length: int, draw: int) -> dict[str, object]:
    params: dict[str, object] = {"opd": opd_id, "start": start, "length": length, "draw": draw, "deleted": 0, "status_worker": "", "search[value]": "", "search[regex]": "false", "order[0][column]": 0, "order[0][dir]": "asc"}
    for index, column in enumerate(OFFICER_COLUMNS):
        params[f"columns[{index}][data]"] = column
        params[f"columns[{index}][name]"] = column
        params[f"columns[{index}][searchable]"] = "true"
        params[f"columns[{index}][orderable]"] = "true"
        params[f"columns[{index}][search][value]"] = ""
        params[f"columns[{index}][search][regex]"] = "false"
    return params


@st.cache_data(ttl=3600, show_spinner=False)
def ambil_daftar_pegawai(opd_targets: tuple[int, ...] = OPD_TARGET) -> pd.DataFrame:
    """Mengambil seluruh halaman DataTables untuk setiap OPD target."""
    officers, failures = [], []
    length = 100
    with requests.Session() as session:
        session.headers.update(_headers())
        for opd_id in opd_targets:
            start, draw = 0, 1
            while True:
                try:
                    response = session.get(f"{BASE_URL}/officers/data", params=_datatable_params(opd_id, start, length, draw), timeout=30)
                    if response.status_code == 401:
                        raise EPresensiAuthenticationError(
                            "Endpoint officers/data mengembalikan 401 Unauthenticated. "
                            "Browser kemungkinan dapat membukanya karena memiliki sesi login; "
                            "aplikasi Python memerlukan cookie atau token dari akses resmi."
                        )
                    response.raise_for_status()
                    payload = response.json()
                    batch = payload.get("data", [])
                    total = int(payload.get("recordsFiltered", 0) or 0)
                    officers.extend(batch)
                    if not batch or start + len(batch) >= total:
                        break
                    start += length
                    draw += 1
                except EPresensiAuthenticationError:
                    raise
                except Exception as exc:
                    LOGGER.exception("Gagal mengambil pegawai OPD %s", opd_id)
                    failures.append({"opd_id": opd_id, "error": str(exc)})
                    break
    if not officers and failures:
        raise RuntimeError("; ".join(item["error"] for item in failures))
    frame = pd.DataFrame(officers)
    if frame.empty:
        return pd.DataFrame(columns=["nip", "name", "opd_id"])
    for column in ("nip", "name", "opd_id"):
        if column not in frame.columns:
            raise ValueError(f"Field {column} tidak ditemukan pada response officers/data")
    frame["nip"] = frame["nip"].fillna("").astype(str).str.strip()
    return frame[frame["nip"].ne("")].drop_duplicates("nip").reset_index(drop=True)


def _request_presensi(bulan: str, tahun: str, nip: str) -> pd.DataFrame:
    with requests.Session() as session:
        response = session.get(f"{BASE_URL}/rekapsemua/{bulan}/{tahun}/{nip}", headers=_headers(), timeout=30)
    response.raise_for_status()
    if "application/json" in response.headers.get("Content-Type", "").lower():
        payload = response.json()
        if isinstance(payload, dict):
            payload = payload.get("data", payload)
        return pd.json_normalize(payload) if payload else pd.DataFrame()
    tables = pd.read_html(StringIO(response.text))
    if not tables:
        return pd.DataFrame()
    keywords = {"tanggal", "datang", "masuk", "pulang", "terlambat", "status"}
    candidates = [table for table in tables if {str(column).strip().lower() for column in table.columns}.intersection(keywords)]
    return candidates[0] if candidates else tables[0]


@st.cache_data(ttl=86400, show_spinner=False)
def _presensi_bulan_lalu(bulan: str, tahun: str, nip: str) -> pd.DataFrame:
    return _request_presensi(bulan, tahun, nip)


@st.cache_data(ttl=300, show_spinner=False)
def _presensi_bulan_berjalan(bulan: str, tahun: str, nip: str) -> pd.DataFrame:
    return _request_presensi(bulan, tahun, nip)


def ambil_data_epresensi(tahun: int | str = 2026) -> pd.DataFrame:
    """Mengambil lima OPD dari Januari sampai bulan berjalan dalam skema dashboard lama."""
    tahun = str(tahun)
    bulan_sekarang = datetime.now().month
    pegawai = ambil_daftar_pegawai(OPD_TARGET)
    normalized = []
    for officer in pegawai.itertuples(index=False):
        nip, nama, opd_id = str(officer.nip).strip(), str(officer.name), int(officer.opd_id)
        for bulan in range(1, bulan_sekarang + 1):
            bulan_str = f"{bulan:02d}"
            try:
                raw = _presensi_bulan_berjalan(bulan_str, tahun, nip) if bulan == bulan_sekarang else _presensi_bulan_lalu(bulan_str, tahun, nip)
                if not raw.empty:
                    result = _normalisasi_bulanan(raw, bulan_str, tahun, nip, nama, opd_id)
                    if not result.empty:
                        normalized.append(result)
            except Exception:
                LOGGER.exception("Gagal mengambil NIP %s periode %s/%s", nip, bulan_str, tahun)
                continue
    return pd.concat(normalized, ignore_index=True) if normalized else pd.DataFrame()


def _normalisasi_bulanan(raw: pd.DataFrame, bulan: str, tahun: str, nip: str, nama: str, opd_id: int) -> pd.DataFrame:
    source = raw.get("datang.sumber", pd.Series("", index=raw.index)).fillna("").astype(str).str.upper()
    arrival = pd.to_datetime(raw.get("datang.datang"), errors="coerce")
    departure = pd.to_datetime(raw.get("pulang.pulang"), errors="coerce")
    late_text = raw.get("datang.telat", pd.Series("00:00", index=raw.index)).fillna("00:00")
    late = late_text.map(_duration_is_positive)
    days = raw.get("hari", pd.Series("", index=raw.index)).map({"Sen": "Senin", "Sel": "Selasa", "Rab": "Rabu", "Kam": "Kamis", "Jum": "Jumat"}).fillna("-")
    is_wfh = source.str.contains("WFH", na=False)
    is_dl = source.str.contains(r"DINAS LUAR|\bDL\b", regex=True, na=False)
    is_leave = source.str.contains(r"CUTI|IZIN|SAKIT|\b(?:CLTN|TB|MPP)\b", regex=True, na=False)
    is_tk = source.str.contains(r"TANPA KETERANGAN|\bTK\b", regex=True, na=False) | (arrival.isna() & ~(is_wfh | is_dl | is_leave))
    actual_arrival = arrival[~(is_wfh | is_dl | is_leave | is_tk)]
    average_arrival = actual_arrival.dt.hour.add(actual_arrival.dt.minute.div(60)).mean() if not actual_arrival.empty else 0.0
    late_days = days[late]
    first = raw.iloc[0]
    opd_name = str(first.get("opd", "")).strip()
    if not opd_name:
        for field in ("opd.name", "opd_name", "nama_opd"):
            if field in raw.columns and pd.notna(first.get(field)):
                opd_name = str(first.get(field)).strip()
                break
    if not opd_name:
        raise ValueError(f"Nama OPD tidak tersedia untuk NIP {nip}")
    daily = pd.DataFrame({"NIP": str(nip), "Nama": nama, "OPD": opd_name, "OPD_ID": opd_id, "Bulan": bulan, "Tahun": tahun, "Tanggal": arrival.dt.date, "Jam Masuk": arrival, "Jam Pulang": departure, "Status": source, "Keterlambatan": late_text, "Hari": days})
    return pd.DataFrame([{"NIP": str(nip), "Nama Pegawai": nama, "Unit Kerja": opd_name, "Bulan": MONTH_NAMES[bulan], "TK": int(is_tk.sum()), "Cuti": int(is_leave.sum()), "Terlambat": int(late.sum()), "Hari Kerja": int(len(daily)), "Jam Datang": round(float(average_arrival), 2), "Hari Dominan": late_days.mode().iloc[0] if not late_days.empty else "-", "WFH": int(is_wfh.sum()), "DL": int(is_dl.sum()), "Jabatan": "-", "Pangkat/Golongan": "-"}])


def _duration_is_positive(value) -> bool:
    try:
        return any(int(float(part)) > 0 for part in str(value).split(":"))
    except (TypeError, ValueError):
        return False
