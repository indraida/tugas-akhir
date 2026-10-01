"""Pembaca rekap presensi Excel dalam format laporan BKD.

Modul ini tidak bergantung pada Streamlit agar parsing dapat diuji dan dipakai
ulang.  Data yang dihasilkan ``load_semua_presensi`` berbentuk long format;
``build_dashboard_dataframe`` adalah adapter untuk kontrak DataFrame dashboard
yang sudah ada.
"""

from __future__ import annotations

import logging
import re
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

import pandas as pd

from modules.employee_type import add_employee_type_columns


LOGGER = logging.getLogger(__name__)
if not LOGGER.handlers:
    _terminal_handler = logging.StreamHandler()
    _terminal_handler.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.addHandler(_terminal_handler)
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False

NAMA_BULAN = {
    "Januari": 1,
    "Februari": 2,
    "Maret": 3,
    "April": 4,
    "Mei": 5,
    "Juni": 6,
    "Juli": 7,
    "Agustus": 8,
    "September": 9,
    "Oktober": 10,
    "November": 11,
    "Desember": 12,
}
NAMA_HARI = {
    0: "Senin", 1: "Selasa", 2: "Rabu", 3: "Kamis", 4: "Jumat",
    5: "Sabtu", 6: "Minggu",
}
KONTRAK_LONG = [
    "NIP", "Nama", "OPD", "Unit Kerja", "Tanggal", "Jam_Masuk", "Jam_Pulang",
    "Menit_Terlambat", "Menit_Awal_Pulang", "Sumber_Datang",
    "Sumber_Pulang", "Status", "Tahun", "Bulan", "Nama_Bulan", "Hari",
    "Terlambat", "Pulang_Awal", "TK", "Presensi_Tidak_Lengkap",
    "Tidak_Absen_Masuk", "Tidak_Absen_Pulang", "Sumber_File",
]


def _empty_long_dataframe() -> pd.DataFrame:
    return pd.DataFrame(columns=KONTRAK_LONG)


def _clean(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def durasi_ke_menit(value: object) -> int:
    """Mengonversi ``HH:MM`` menjadi jumlah menit; nilai tidak valid menjadi 0."""
    text = _clean(value)
    match = re.fullmatch(r"(\d{1,3}):(\d{2})", text)
    if not match:
        return 0
    jam, menit = (int(part) for part in match.groups())
    return jam * 60 + menit if 0 <= menit < 60 else 0


def _periode_dari_laporan(raw: pd.DataFrame) -> tuple[int, int, str]:
    for value in raw.iloc[:6].fillna("").astype(str).to_numpy().ravel():
        match = re.search(r"Bulan\s+(\w+)\s+Tahun\s+(\d{4})", value, re.IGNORECASE)
        if match:
            nama_bulan = match.group(1).capitalize()
            if nama_bulan in NAMA_BULAN:
                return int(match.group(2)), NAMA_BULAN[nama_bulan], nama_bulan
    raise ValueError("Informasi Bulan/Tahun tidak ditemukan pada laporan Excel")


def _unit_kerja_dari_laporan(raw: pd.DataFrame) -> str:
    """Ambil nama instansi yang dicantumkan pada header laporan bila tersedia."""
    if len(raw) > 1:
        unit_kerja = _clean(raw.iat[1, 0])
        if unit_kerja:
            return unit_kerja
    return "-"


def _opd_dari_nama_file(path: Path) -> str:
    """Ambil kode OPD dari nama laporan bila header instansi tidak tersedia."""
    match = re.search(
        r"^Rekap_Bulanan_FormatPDF_(.+?)_\d{1,2}_\d{4}$",
        path.stem,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else path.stem


def _parse_sel_presensi(value: object) -> dict[str, object]:
    lines = [line.strip() for line in _clean(value).splitlines() if line.strip()]
    if not lines:
        return {
            "Jam_Masuk": "", "Jam_Pulang": "", "Menit_Terlambat": 0,
            "Menit_Awal_Pulang": 0, "Sumber_Datang": "", "Sumber_Pulang": "",
            "Status": "",
        }
    if len(lines) == 1 and lines[0].upper() == "LIBUR":
        return {
            "Jam_Masuk": "", "Jam_Pulang": "", "Menit_Terlambat": 0,
            "Menit_Awal_Pulang": 0, "Sumber_Datang": "LIBUR",
            "Sumber_Pulang": "LIBUR", "Status": "LIBUR",
        }
    if len(lines) < 6:
        # Kode tunggal tetap disimpan apa adanya, tanpa menganggapnya TK.
        kode = lines[-1].upper()
        return {
            "Jam_Masuk": "", "Jam_Pulang": "", "Menit_Terlambat": 0,
            "Menit_Awal_Pulang": 0, "Sumber_Datang": kode,
            "Sumber_Pulang": kode, "Status": kode,
        }

    sumber_datang, sumber_pulang = lines[4].upper(), lines[5].upper()
    return {
        "Jam_Masuk": lines[0],
        "Jam_Pulang": lines[1],
        "Menit_Terlambat": durasi_ke_menit(lines[2]),
        "Menit_Awal_Pulang": durasi_ke_menit(lines[3]),
        "Sumber_Datang": sumber_datang,
        "Sumber_Pulang": sumber_pulang,
        "Status": f"{sumber_datang}/{sumber_pulang}",
    }


def _parse_raw_presensi(raw: pd.DataFrame, source_name: str) -> pd.DataFrame:
    """Terapkan parser laporan yang sama pada DataFrame hasil baca Excel."""
    path = Path(source_name)
    tahun, bulan, nama_bulan = _periode_dari_laporan(raw)
    unit_kerja = _unit_kerja_dari_laporan(raw)
    opd = unit_kerja if unit_kerja != "-" else _opd_dari_nama_file(path)

    # Indeks DataFrame berbasis nol: baris Excel ke-7 dan ke-8.
    hari_header = raw.iloc[6] if len(raw) > 6 else pd.Series(dtype=object)
    tanggal_header = raw.iloc[7] if len(raw) > 7 else pd.Series(dtype=object)
    kolom_tanggal: list[tuple[int, int]] = []
    for kolom in range(2, raw.shape[1]):
        try:
            tanggal = int(float(_clean(tanggal_header.iloc[kolom])))
            pd.Timestamp(year=tahun, month=bulan, day=tanggal)
        except (TypeError, ValueError):
            continue
        kolom_tanggal.append((kolom, tanggal))
    if not kolom_tanggal:
        raise ValueError("Header tanggal tidak ditemukan pada laporan Excel")

    records: list[dict[str, object]] = []
    for row_index in range(8, len(raw)):
        info = [_clean(item) for item in _clean(raw.iat[row_index, 1]).splitlines()]
        info = [item for item in info if item]
        nip_index = next(
            (index for index, item in enumerate(info) if re.fullmatch(r"\d{8,}", item)),
            None,
        )
        if nip_index is None or nip_index == 0:
            continue
        # Biasanya NIP berada di baris kedua. Beberapa pegawai mencantumkan
        # gelar pada baris tersebut, sehingga NIP numerik pertama dipakai
        # sebagai identitas dan seluruh baris sebelumnya tetap menjadi nama.
        nama, nip = " ".join(info[:nip_index]), info[nip_index]
        if not nip:
            LOGGER.warning("Baris %s pada %s tidak memiliki NIP", row_index + 1, path.name)
            continue

        for kolom, nomor_tanggal in kolom_tanggal:
            tanggal = pd.Timestamp(year=tahun, month=bulan, day=nomor_tanggal)
            presensi = _parse_sel_presensi(raw.iat[row_index, kolom])
            libur = presensi["Status"] == "LIBUR"
            sumber_datang = str(presensi["Sumber_Datang"])
            sumber_pulang = str(presensi["Sumber_Pulang"])
            records.append({
                "NIP": str(nip), "Nama": nama, "OPD": opd,
                "Unit Kerja": unit_kerja if unit_kerja != "-" else opd,
                "Tanggal": tanggal,
                **presensi,
                "Tahun": tahun, "Bulan": bulan, "Nama_Bulan": nama_bulan,
                "Hari": NAMA_HARI[tanggal.dayofweek],
                "Terlambat": bool(presensi["Menit_Terlambat"] > 0 and not libur),
                "Pulang_Awal": bool(presensi["Menit_Awal_Pulang"] > 0 and not libur),
                "TK": bool(sumber_datang == "TK" and sumber_pulang == "TK" and not libur),
                "Presensi_Tidak_Lengkap": bool((sumber_datang == "TK") ^ (sumber_pulang == "TK")) and not libur,
                "Tidak_Absen_Masuk": bool(sumber_datang == "TK" and sumber_pulang != "TK" and not libur),
                "Tidak_Absen_Pulang": bool(sumber_pulang == "TK" and sumber_datang != "TK" and not libur),
                "Sumber_File": path.name,
            })

    if not records:
        raise ValueError(f"Tidak ada data pegawai valid dalam {path.name}")
    return pd.DataFrame.from_records(records, columns=KONTRAK_LONG)


def parse_file_presensi(file_path: str | Path) -> pd.DataFrame:
    """Parse satu laporan BKD ke long format, tanpa menulis ke file sumber."""
    path = Path(file_path)
    raw = pd.read_excel(path, sheet_name="Rekap Bulanan", header=None, dtype=str)
    return _parse_raw_presensi(raw, path.name)


def read_attendance_excel(file_or_bytes: BinaryIO | bytes, filename: str) -> pd.DataFrame:
    """Baca upload XLSX dengan parser/standardisasi laporan BKD existing.

    Nama file hanya dipakai sebagai metadata/fallback OPD; periode selalu dibaca
    dari isi laporan dan tanggal hasil parsing.
    """
    if not str(filename).lower().endswith(".xlsx"):
        raise ValueError("Format file tidak didukung. Gunakan file .xlsx.")
    payload = BytesIO(file_or_bytes) if isinstance(file_or_bytes, bytes) else file_or_bytes
    if hasattr(payload, "seek"):
        payload.seek(0)
    raw = pd.read_excel(payload, sheet_name="Rekap Bulanan", header=None, dtype=str)
    return _parse_raw_presensi(raw, Path(filename).name)


def load_semua_presensi(data_dir: str | Path = "data") -> pd.DataFrame:
    """Baca semua laporan Excel; kegagalan satu file tidak menghentikan file lain."""
    files = sorted(Path(data_dir).rglob("*.xlsx"))
    LOGGER.info("Total file Excel ditemukan: %s", len(files))
    if files:
        LOGGER.info("Nama file ditemukan: %s", ", ".join(path.name for path in files))
    if not files:
        LOGGER.warning("Tidak ada file rekap Excel di %s", Path(data_dir))
        return _empty_long_dataframe()

    semua_data: list[pd.DataFrame] = []
    for path in files:
        LOGGER.info("Membaca: %s", path.name)
        try:
            parsed = parse_file_presensi(path)
            semua_data.append(parsed)
            LOGGER.info("Berhasil: %s (%s record)", path.name, len(parsed))
        except Exception as exc:  # dicatat, lalu file lain tetap diproses
            LOGGER.error("Gagal membaca %s: %s", path.name, exc)
    if not semua_data:
        return _empty_long_dataframe()

    result = pd.concat(semua_data, ignore_index=True)
    input_records = len(result)
    duplicated = result.duplicated(subset=["NIP", "Tanggal"], keep="first")
    if duplicated.any():
        LOGGER.warning("Dihapus %s baris duplikat NIP + Tanggal", int(duplicated.sum()))
    result = result.loc[~duplicated].reset_index(drop=True)
    result = add_employee_type_columns(result)
    result.attrs["etl_quality"] = {
        "files_processed": len(semua_data),
        "records_input": input_records,
        "records_valid": len(result),
        "duplicates": int(duplicated.sum()),
        "incomplete": int(result["Presensi_Tidak_Lengkap"].sum()),
        "needs_verification": int(result["TK"].sum() + result["Presensi_Tidak_Lengkap"].sum()),
    }
    LOGGER.info("Total OPD: %s", result["OPD"].nunique())
    LOGGER.info("Total pegawai: %s", result["NIP"].nunique())
    LOGGER.info("Total record: %s", len(result))
    employee_types = result.drop_duplicates("NIP")["Jenis Pegawai"].value_counts()
    LOGGER.info(
        "Jenis pegawai terdeteksi — PNS: %s, PPPK: %s, Belum Diketahui: %s",
        int(employee_types.get("PNS", 0)), int(employee_types.get("PPPK", 0)),
        int(employee_types.get("Belum Diketahui", 0)),
    )
    LOGGER.info(
        "Daftar OPD yang berhasil dibaca: %s",
        ", ".join(sorted(result["OPD"].dropna().unique())),
    )
    return result


def _jam_ke_desimal(value: object) -> float | None:
    text = _clean(value)
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        return None
    jam, menit = (int(part) for part in match.groups())
    return jam + menit / 60 if 0 <= jam < 24 and 0 <= menit < 60 else None


def build_dashboard_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    """Adapter long format Excel ke kolom agregat yang digunakan UI lama."""
    columns = [
        "NIP", "Nama Pegawai", "Unit Kerja", "Tahun", "Bulan", "TK", "Cuti",
        "Terlambat", "Hari Kerja", "Jam Datang", "Hari Dominan", "WFH",
        "DL", "Jabatan", "Pangkat/Golongan", "Jenis Pegawai", "Jenis Pegawai Source",
    ]
    if data.empty:
        return pd.DataFrame(columns=columns)

    work = data.copy()
    source = (work["Sumber_Datang"].fillna("") + "/" + work["Sumber_Pulang"].fillna(""))
    work["_cuti"] = source.str.contains("CUTI", case=False, na=False)
    work["_wfh"] = source.str.contains(r"\bWFH\b|\bWFA\b", case=False, regex=True, na=False)
    work["_dl"] = source.str.contains(r"\bDL\b", case=False, regex=True, na=False)
    work["_hari_kerja"] = work["Status"].ne("LIBUR")
    work["_jam_datang"] = work["Jam_Masuk"].map(_jam_ke_desimal)
    work.loc[work["Sumber_Datang"].ne("MESIN"), "_jam_datang"] = None

    result: list[dict[str, object]] = []
    for (nip, nama, unit_kerja, tahun, bulan, nama_bulan), group in work.groupby(
        ["NIP", "Nama", "Unit Kerja", "Tahun", "Bulan", "Nama_Bulan"], dropna=False
    ):
        late_days = group.loc[group["Terlambat"], "Hari"]
        dominant_day = late_days.mode().iloc[0] if not late_days.empty else "-"
        average_arrival = group["_jam_datang"].dropna().mean()
        result.append({
            "NIP": str(nip), "Nama Pegawai": nama,
            # Laporan sumber tidak memuat jabatan maupun pangkat; jangan mengarang nilai.
            "Unit Kerja": unit_kerja, "Tahun": int(tahun), "Bulan": nama_bulan,
            "TK": int(group["TK"].sum()), "Cuti": int(group["_cuti"].sum()),
            "Terlambat": int(group["Terlambat"].sum()),
            "Hari Kerja": int(group["_hari_kerja"].sum()),
            "Jam Datang": round(float(average_arrival), 2) if pd.notna(average_arrival) else 0.0,
            "Hari Dominan": dominant_day, "WFH": int(group["_wfh"].sum()),
            "DL": int(group["_dl"].sum()), "Jabatan": "-", "Pangkat/Golongan": "-",
            "Jenis Pegawai": group["Jenis Pegawai"].iloc[0],
            "Jenis Pegawai Source": group["Jenis Pegawai Source"].iloc[0],
        })
    return pd.DataFrame(result, columns=columns).sort_values(
        ["NIP", "Bulan"], kind="stable"
    ).reset_index(drop=True)
