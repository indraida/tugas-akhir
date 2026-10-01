from io import BytesIO
from pathlib import Path

import pandas as pd

from modules.attendance_import import build_import_preview
from modules.excel_parser import parse_file_presensi, read_attendance_excel
from modules.presensi_data_management import _format_count_id, _import_flash_message


class Upload(BytesIO):
    def __init__(self, payload: bytes, name: str):
        super().__init__(payload)
        self.name = name


def _sample_path() -> Path:
    return sorted(Path("data").glob("Rekap_Bulanan_FormatPDF_*.xlsx"))[0]


def test_uploaded_xlsx_reuses_existing_parser_and_preserves_nip():
    path = _sample_path()
    expected = parse_file_presensi(path)
    actual = read_attendance_excel(path.read_bytes(), path.name)
    pd.testing.assert_frame_equal(actual, expected)
    assert actual["NIP"].map(type).eq(str).all()


def test_multiple_same_files_are_reported_as_duplicates_not_valid_rows():
    path = _sample_path()
    payload = path.read_bytes()
    preview = build_import_preview([Upload(payload, path.name), Upload(payload, path.name)])
    assert preview.duplicate_count == len(preview.data)
    assert preview.valid_data.empty
    assert len(preview.invalid_data) == len(preview.data)


def test_xls_is_rejected_safely():
    try:
        read_attendance_excel(b"not-an-excel", "presensi.xls")
    except ValueError as exc:
        assert "Gunakan file .xlsx" in str(exc)
    else:
        raise AssertionError(".xls seharusnya ditolak")


def test_success_flash_uses_indonesian_counts_and_actual_result_fields():
    message = _import_flash_message({
        "validated": 2400,
        "processed": 2350,
        "inserted": 2200,
        "updated": 150,
        "skipped": 50,
        "failed": 0,
    })
    assert _format_count_id(2400) == "2.400"
    assert "2.400 data divalidasi" in message
    assert "2.350 berhasil diproses" in message
    assert "2.200 data baru" in message
    assert "150 diperbarui" in message
    assert "50 duplikat/invalid dilewati" in message
    assert "gagal" not in message
