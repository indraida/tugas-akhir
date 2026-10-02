import pandas as pd
import pytest

from services import work_calendar
from services.work_calendar import apply_work_calendar, build_work_calendar, is_tk_eligible, load_calendar_overrides, summarize_work_calendar_period
from database.work_calendar import list_work_calendar, upsert_work_calendar


def write_calendar(engine, rows):
    frame = pd.DataFrame(rows, columns=["tanggal", "jenis_hari", "keterangan", "dasar_hukum"])
    frame["tanggal"] = pd.to_datetime(frame["tanggal"])
    frame["tahun"] = frame["tanggal"].dt.year
    frame["is_hari_kerja"] = frame["jenis_hari"].eq("CUTI_BERSAMA")
    upsert_work_calendar(engine, frame)


def test_calendar_rule_matrix_and_official_override(postgres_engine):
    write_calendar(postgres_engine, [
        ["2026-01-02", "CUTI_BERSAMA", "Cuti bersama", "SE Uji"],
        ["2026-08-17", "LIBUR_NASIONAL", "Hari nasional", "SE Uji"],
    ])
    calendar = build_work_calendar(2026, allow_pdf_extraction=False, engine=postgres_engine).set_index("tanggal")
    weekday = calendar.loc[pd.Timestamp("2026-01-05")]
    weekend = calendar.loc[pd.Timestamp("2026-01-03")]
    collective = calendar.loc[pd.Timestamp("2026-01-02")]
    holiday = calendar.loc[pd.Timestamp("2026-08-17")]
    assert (weekday["jenis_hari"], weekday["is_hari_kerja"], weekday["wajib_presensi"], weekday["eligible_tk"]) == ("HARI_KERJA", True, True, True)
    assert (weekend["jenis_hari"], weekend["is_hari_kerja"], weekend["wajib_presensi"], weekend["eligible_tk"]) == ("AKHIR_PEKAN", False, False, False)
    assert (collective["is_hari_kerja"], collective["wajib_presensi"], collective["eligible_tk"]) == (True, False, False)
    assert (holiday["is_hari_kerja"], holiday["wajib_presensi"], holiday["eligible_tk"]) == (False, False, False)
    assert not is_tk_eligible("2026-01-02", engine=postgres_engine)


def test_period_summary_counts_unique_calendar_dates_not_employee_days(postgres_engine):
    write_calendar(postgres_engine, [
        ["2026-01-02", "CUTI_BERSAMA", "Cuti bersama", "SE Uji"],
        ["2026-08-17", "LIBUR_NASIONAL", "Hari nasional", "SE Uji"],
    ])
    result = summarize_work_calendar_period([(2026, 1), (2026, 1)], engine=postgres_engine)
    assert result["workdays"] == 22
    assert result["required_days"] == 21
    assert result["collective_leave"] == 1
    assert result["weekends"] == 9
    assert result["required_days"] <= result["workdays"]


def test_official_tk_conflict_is_flagged_without_overwrite(postgres_engine):
    write_calendar(postgres_engine, [["2026-01-02", "CUTI_BERSAMA", "Cuti bersama", "SE Uji"]])
    source = pd.DataFrame({"Tanggal": [pd.Timestamp("2026-01-02")], "TK": [True], "Status": ["TK"]})
    result = apply_work_calendar(source, engine=postgres_engine)
    assert bool(result.loc[0, "anomali_kalender_presensi"])
    assert bool(result.loc[0, "TK"])
    assert result.loc[0, "Status"] == "TK"


def test_duplicate_official_date_is_rejected(postgres_engine):
    frame = pd.DataFrame([
        ["2026-01-02", "CUTI_BERSAMA", "A", "SE"],
        ["2026-01-02", "LIBUR_NASIONAL", "B", "SE"],
    ], columns=["tanggal", "jenis_hari", "keterangan", "dasar_hukum"])
    frame["is_hari_kerja"] = False
    frame["tahun"] = 2026
    with pytest.raises(ValueError, match="duplicate"):
        upsert_work_calendar(postgres_engine, frame)


def test_missing_pdf_has_specific_warning_and_does_not_crash(tmp_path, postgres_engine):
    result = load_calendar_overrides(2026, tmp_path, engine=postgres_engine)

    assert result.empty
    assert result.attrs["warning"] == "Dokumen SE tahun 2026 belum tersedia."


def test_existing_database_rows_are_used_without_reading_pdf(tmp_path, monkeypatch, postgres_engine):
    write_calendar(postgres_engine, [["2026-01-02", "CUTI_BERSAMA", "Cuti bersama", "SE Uji"]])
    document_dir = tmp_path / "dokumen_resmi" / "2026"
    document_dir.mkdir(parents=True)
    (document_dir / "SE_2026.pdf").write_bytes(b"not read")

    def fail_if_called(path):
        raise AssertionError(f"PDF seharusnya tidak dibaca: {path}")

    monkeypatch.setattr(work_calendar, "_extract_pdf_text", fail_if_called)
    result = load_calendar_overrides(2026, tmp_path, engine=postgres_engine)

    assert len(result) == 1
    assert result.iloc[0]["jenis_hari"] == "CUTI_BERSAMA"


def test_pdf_text_is_extracted_and_saved_to_database(tmp_path, monkeypatch, postgres_engine):
    document_dir = tmp_path / "dokumen_resmi" / "2026"
    document_dir.mkdir(parents=True)
    document = document_dir / "SE_2026.pdf"
    document.write_bytes(b"mock")
    text = """
    A. HARI LIBUR NASIONAL TAHUN 2026
    1. 1 Januari Kamis Tahun Baru 2026 Masehi
    B. CUTI BERSAMA TAHUN 2026
    1. 20, 23, dan 24 Maret Jumat, Senin, dan Selasa Idul Fitri 1447 Hijriah
    """
    monkeypatch.setattr(work_calendar, "_extract_pdf_text", lambda path: (text, 1))

    result = load_calendar_overrides(2026, tmp_path, engine=postgres_engine)

    assert len(result) == 4
    assert len(list_work_calendar(postgres_engine, 2026)) == 4
    assert set(result["jenis_hari"]) == {"LIBUR_NASIONAL", "CUTI_BERSAMA"}


def test_scanned_pdf_uses_ocr_then_reuses_database(tmp_path, monkeypatch, postgres_engine):
    document_dir = tmp_path / "dokumen_resmi" / "2026"
    document_dir.mkdir(parents=True)
    (document_dir / "SE_scan_2026.pdf").write_bytes(b"scanned")
    ocr_text = """
    A. HARI LIBUR NASIONAL TAHUN 2026
    1. 1 Januari Kamis Tahun Baru 2026 Masehi
    B. CUTI BERSAMA TAHUN 2026
    1. 16 Februari Senin Tahun Baru Imlek 2577 Kongzili
    """
    calls = {"ocr": 0}
    monkeypatch.setattr(work_calendar, "_extract_pdf_text", lambda path: ("", 4))

    def fake_ocr(path):
        calls["ocr"] += 1
        return ocr_text

    monkeypatch.setattr(work_calendar, "_ocr_pdf", fake_ocr)
    first = load_calendar_overrides(2026, tmp_path, engine=postgres_engine)
    second = load_calendar_overrides(2026, tmp_path, engine=postgres_engine)

    assert calls["ocr"] == 1
    assert len(first) == len(second) == 2
    assert first.attrs["extraction_audit"]["has_text_layer"] is False
    assert first.attrs["extraction_audit"]["ocr_used"] is True
    assert first["tanggal"].is_unique
