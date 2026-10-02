from datetime import date, time

import pandas as pd

from database.repository import prepare_daily_records


def clean_frame() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "NIP": " 001234 ", "Nama": "Pegawai A", "Unit Kerja": "OPD ASLI",
            "Tanggal": "2026-05-04", "Jam_Masuk": "07:42", "Jam_Pulang": "16:05",
            "Status": "MESIN/MESIN", "Sumber_Datang": "MESIN", "Sumber_Pulang": "MESIN",
            "Menit_Terlambat": 12, "TK": False, "Terlambat": True,
            "Sumber_File": "rekap.xlsx",
        },
        {
            "NIP": "001234", "Nama": "Pegawai A", "Unit Kerja": "OPD ASLI",
            "Tanggal": "2026-05-05", "Jam_Masuk": "", "Jam_Pulang": "",
            "Status": "CUTI/CUTI", "Sumber_Datang": "CUTI", "Sumber_Pulang": "CUTI",
            "Menit_Terlambat": 0, "TK": False, "Terlambat": False,
            "Sumber_File": "rekap.xlsx",
        },
    ])


def test_prepare_records_preserves_nip_and_existing_attendance_fields():
    records, report = prepare_daily_records(clean_frame())
    assert report == {
        "processed": 2, "valid": 2, "rejected": 0,
        "rejection_reasons": {
            "nip_kosong": 0, "tanggal_invalid": 0,
            "keterlambatan_invalid": 0, "duplicate_nip_tanggal": 0,
        },
    }
    assert records[0]["nip"] == "001234"
    assert records[0]["tanggal"] == date(2026, 5, 4)
    assert records[0]["jam_masuk"] == time(7, 42)
    assert records[0]["status_presensi"] == "Terlambat"
    assert records[1]["status_presensi"] == "Cuti"
    assert records[1]["jam_masuk"] is None


def test_cltn_tb_and_mpp_are_stored_as_leave():
    frames = []
    for offset, code in enumerate(["CLTN", "TB", "MPP"], start=1):
        row = clean_frame().iloc[[1]].copy()
        row["Tanggal"] = f"2026-06-{offset:02d}"
        row["Status"] = f"{code}/{code}"
        row["Sumber_Datang"] = code
        row["Sumber_Pulang"] = code
        frames.append(row)

    records, report = prepare_daily_records(pd.concat(frames, ignore_index=True))

    assert report["rejected"] == 0
    assert [record["status_presensi"] for record in records] == ["Cuti", "Cuti", "Cuti"]


def test_invalid_and_duplicate_rows_are_counted_not_silently_dropped():
    frame = pd.concat([clean_frame(), clean_frame().iloc[[0]]], ignore_index=True)
    frame.loc[1, "Tanggal"] = "bukan-tanggal"
    records, report = prepare_daily_records(frame)
    assert records == []
    assert report["processed"] == 3
    assert report["rejected"] == 3
    assert report["rejection_reasons"]["tanggal_invalid"] == 1
    assert report["rejection_reasons"]["duplicate_nip_tanggal"] == 2
