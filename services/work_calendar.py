"""Logika kalender kerja dengan master tanggal khusus dari PostgreSQL."""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
import shutil

import pandas as pd
from sqlalchemy.engine import Engine

from database.connection import get_engine
from database.work_calendar import list_work_calendar, replace_work_calendar

LOGGER = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OFFICIAL_DOC_DIR = PROJECT_ROOT / "data" / "dokumen_resmi"
CALENDAR_DIR = PROJECT_ROOT / "data" / "kalender"
OCR_CACHE_DIR = CALENDAR_DIR / "cache"
MIN_TEXT_LAYER_LENGTH = 100

DAY_RULES = {
    "HARI_KERJA": (True, True, True),
    "CUTI_BERSAMA": (True, False, False),
    "LIBUR_NASIONAL": (False, False, False),
    "AKHIR_PEKAN": (False, False, False),
}
CALENDAR_COLUMNS = [
    "tanggal", "jenis_hari", "keterangan", "is_hari_kerja", "wajib_presensi",
    "eligible_tk", "dasar_hukum", "nomor_dokumen", "tahun", "source",
    "source_url", "retrieved_at",
]
MONTH_NUMBERS = {
    name: number for number, name in enumerate(
        ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
         "Agustus", "September", "Oktober", "November", "Desember"], 1
    )
}


def _official_document_path(year: int, data_dir: str | Path | None = None) -> Path | None:
    directory = OFFICIAL_DOC_DIR if data_dir is None else Path(data_dir) / "dokumen_resmi"
    year_directory = directory / str(int(year))
    if not year_directory.is_dir():
        return None
    candidates = sorted(year_directory.glob("*.pdf"))
    return candidates[0] if candidates else None


def _empty_calendar() -> pd.DataFrame:
    return pd.DataFrame(columns=CALENDAR_COLUMNS)


def _pdf_fingerprint(path: Path) -> dict[str, object]:
    stat = path.stat()
    resolved = path.resolve()
    portable_path = str(resolved.relative_to(PROJECT_ROOT)) if resolved.is_relative_to(PROJECT_ROOT) else path.name
    return {"path": portable_path, "modified_time_ns": stat.st_mtime_ns, "size": stat.st_size}


def _ocr_cache_paths(year: int, data_dir: str | Path | None = None) -> tuple[Path, Path]:
    directory = OCR_CACHE_DIR if data_dir is None else Path(data_dir) / "cache"
    return directory / f"se_{int(year)}_extracted.txt", directory / f"se_{int(year)}_audit.json"


def _read_audit(path: Path) -> dict[str, object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def _tesseract_command() -> str:
    configured = os.getenv("TESSERACT_CMD")
    discovered = shutil.which("tesseract")
    candidates = [configured, discovered]
    program_files = os.getenv("ProgramFiles")
    if program_files:
        candidates.append(str(Path(program_files) / "Tesseract-OCR" / "tesseract.exe"))
    command = next((item for item in candidates if item and Path(item).is_file()), None)
    if command is None:
        raise RuntimeError("Tesseract OCR belum terpasang atau TESSERACT_CMD belum dikonfigurasi")
    return command


def _database_calendar(year: int, engine: Engine | None = None) -> pd.DataFrame:
    """Baca dan validasi tanggal khusus dari tabel kalender_kerja."""
    source = list_work_calendar(engine or get_engine(), int(year))
    if source.empty:
        return _empty_calendar()
    source["tanggal"] = pd.to_datetime(source["tanggal"], errors="coerce").dt.normalize()
    if source["tanggal"].isna().any():
        raise ValueError("Master kalender memuat tanggal yang tidak valid")
    source["jenis_hari"] = source["jenis_hari"].str.strip().str.upper()
    invalid = sorted(set(source["jenis_hari"]).difference(DAY_RULES))
    if invalid:
        raise ValueError(f"Jenis hari tidak dikenal: {', '.join(invalid)}")
    if source["tanggal"].duplicated().any():
        raise ValueError("Konflik tanggal duplicate pada master kalender")
    if not source["tanggal"].dt.year.eq(int(year)).all():
        raise ValueError(f"Master kalender {year} memuat tanggal dari tahun lain")
    for field in CALENDAR_COLUMNS:
        if field not in source:
            source[field] = False if field in {"is_hari_kerja", "wajib_presensi", "eligible_tk"} else ""
    for kind, values in DAY_RULES.items():
        mask = source["jenis_hari"].eq(kind)
        source.loc[mask, "is_hari_kerja"] = values[0]
        source.loc[mask, "wajib_presensi"] = values[1]
        source.loc[mask, "eligible_tk"] = values[2]
    source["tahun"] = int(year)
    source["source"] = "PostgreSQL"
    return source[CALENDAR_COLUMNS]


def _extract_pdf_text(path: Path) -> tuple[str, int]:
    """Baca text layer PDF dan jumlah halamannya."""
    from pypdf import PdfReader

    reader = PdfReader(path)
    return "\n".join(page.extract_text() or "" for page in reader.pages), len(reader.pages)


def _ocr_pdf(path: Path) -> str:
    """Render tiap halaman hanya ketika text layer tidak memadai, lalu jalankan OCR."""
    import pymupdf
    import pytesseract
    from PIL import Image

    pytesseract.pytesseract.tesseract_cmd = _tesseract_command()
    document = pymupdf.open(path)
    try:
        return "\n".join(
            pytesseract.image_to_string(
                Image.open(BytesIO(page.get_pixmap(dpi=300).tobytes("png"))),
                config="--psm 4",
            )
            for page in document
        )
    finally:
        document.close()


def _document_text(
    path: Path, year: int, data_dir: str | Path | None = None, refresh: bool = False,
) -> tuple[str, dict[str, object]]:
    """Pilih text layer atau OCR cache berdasarkan fingerprint dokumen."""
    text_cache, audit_cache = _ocr_cache_paths(year, data_dir)
    fingerprint = _pdf_fingerprint(path)
    cached_audit = _read_audit(audit_cache)
    if (
        not refresh
        and cached_audit.get("fingerprint") == fingerprint
        and text_cache.exists()
    ):
        text = text_cache.read_text(encoding="utf-8")
        return text, cached_audit

    text_layer, page_count = _extract_pdf_text(path)
    has_text_layer = len(text_layer.strip()) >= MIN_TEXT_LAYER_LENGTH
    text = text_layer if has_text_layer else _ocr_pdf(path)
    audit = {
        "fingerprint": fingerprint,
        "document": path.name,
        "page_count": page_count,
        "text_length": len(text_layer.strip()),
        "has_text_layer": has_text_layer,
        "ocr_used": not has_text_layer,
        "ocr_engine": "Tesseract OCR via pytesseract" if not has_text_layer else "",
    }
    text_cache.parent.mkdir(parents=True, exist_ok=True)
    text_cache.write_text(text, encoding="utf-8")
    audit_cache.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    return text, audit


def _expand_dates(date_text: str, month_text: str, year: int) -> list[pd.Timestamp]:
    numbers = [int(value) for value in re.findall(r"\d{1,2}", date_text)]
    if ("-" in date_text or "–" in date_text) and len(numbers) == 2:
        numbers = list(range(numbers[0], numbers[1] + 1))
    month = next(value for name, value in MONTH_NUMBERS.items() if name.lower() == month_text.lower())
    return [pd.Timestamp(year=int(year), month=month, day=day) for day in numbers]


def extract_calendar_from_pdf(
    path: Path, year: int, data_dir: str | Path | None = None, refresh: bool = False,
) -> pd.DataFrame:
    """Ekstrak hanya LIBUR_NASIONAL dan CUTI_BERSAMA dari text layer PDF."""
    text, audit = _document_text(path, year, data_dir, refresh)
    if not text.strip():
        raise ValueError("OCR tidak menghasilkan teks yang dapat diproses")
    months = "|".join(MONTH_NUMBERS)
    weekday = r"(?:Senin|Selasa|Rabu|Kamis|Jumat|Sabtu|Minggu)"
    row_pattern = re.compile(
        rf"^\s*[|]?\s*(?:\d+\.?\s*[|]?\s*)?([\d\s,–-]+?(?:dan\s+\d{{1,2}})?)\s+({months})\s*[|]?\s+"
        rf"(?:{weekday}(?:(?:\s*[-,]\s*(?:dan\s+)?|\s+dan\s+){weekday})*\s*[|]?\s+)?(.+?)\s*[|]?\s*$",
        re.IGNORECASE,
    )
    retrieved = datetime.now(timezone.utc).isoformat()
    source_path = str(path.relative_to(PROJECT_ROOT)) if path.is_relative_to(PROJECT_ROOT) else str(path)
    rows: list[dict[str, object]] = []
    unresolved: list[str] = []
    kind: str | None = None
    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        upper = line.upper()
        if re.search(r"\bA\.\s*HARI LIBUR NASIONAL TAHUN", upper):
            kind = "LIBUR_NASIONAL"
            continue
        if re.search(r"\bB\.\s*CUTI BERSAMA TAHUN", upper):
            kind = "CUTI_BERSAMA"
            continue
        if kind is None:
            continue
        match = row_pattern.match(line)
        if not match:
            if re.search(rf"\b(?:{months})\b", line, re.IGNORECASE) and re.search(r"\d", line):
                unresolved.append(line)
            continue
        date_text, month_text, description = match.groups()
        for date in _expand_dates(date_text, month_text, int(year)):
            workday, required, eligible = DAY_RULES[kind]
            rows.append({
                "tanggal": date, "jenis_hari": kind, "keterangan": description.strip(),
                "is_hari_kerja": workday, "wajib_presensi": required,
                "eligible_tk": eligible, "dasar_hukum": path.stem,
                "nomor_dokumen": path.stem, "tahun": int(year), "source": path.name,
                "source_url": source_path, "retrieved_at": retrieved,
            })
    result = pd.DataFrame(rows, columns=CALENDAR_COLUMNS)
    if result.empty:
        raise ValueError("Tabel Libur Nasional/Cuti Bersama tidak dapat dibaca dari PDF")
    duplicate_count = int(result.duplicated(["tanggal", "jenis_hari"]).sum())
    conflict_dates = result.groupby("tanggal")["jenis_hari"].nunique()
    conflict_count = int(conflict_dates.gt(1).sum())
    if conflict_count:
        raise ValueError(f"Ditemukan {conflict_count} konflik kategori pada tanggal yang sama")
    result = result.drop_duplicates(["tanggal", "jenis_hari"]).sort_values("tanggal").reset_index(drop=True)
    if not result["tanggal"].dt.year.eq(int(year)).all() or not result["tanggal"].is_unique:
        raise ValueError("Hasil OCR gagal validasi tahun atau keunikan tanggal")
    audit.update({
        "libur_nasional_count": int(result["jenis_hari"].eq("LIBUR_NASIONAL").sum()),
        "cuti_bersama_count": int(result["jenis_hari"].eq("CUTI_BERSAMA").sum()),
        "unresolved_calendar_rows": unresolved,
        "unresolved_count": len(unresolved),
        "duplicate_count": duplicate_count,
        "conflict_count": conflict_count,
    })
    _, audit_cache = _ocr_cache_paths(year, data_dir)
    audit_cache.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    result.attrs["extraction_audit"] = audit
    LOGGER.info("Audit ekstraksi kalender %s: %s", year, audit)
    return result


def load_calendar_overrides(
    year: int, data_dir: str | Path | None = None, refresh: bool = False,
    allow_pdf_extraction: bool = True, engine: Engine | None = None,
) -> pd.DataFrame:
    """Baca master dari PostgreSQL; PDF hanya menjadi sumber pembaruan opsional."""
    document_path = _official_document_path(year, data_dir)
    database_engine = engine or get_engine()
    database_warning = ""
    try:
        existing = _database_calendar(year, database_engine)
    except Exception as exc:
        if engine is not None:
            raise
        LOGGER.warning("Kalender PostgreSQL tidak dapat dimuat untuk tahun %s: %s", year, exc)
        existing = _empty_calendar()
        database_warning = f"Kalender PostgreSQL tahun {int(year)} belum dapat dimuat."
    _, audit_path = _ocr_cache_paths(year, data_dir)
    cached_audit = _read_audit(audit_path)
    fingerprint_matches = bool(
        document_path
        and cached_audit.get("fingerprint") == _pdf_fingerprint(document_path)
    )
    if not existing.empty and (not allow_pdf_extraction or not refresh or fingerprint_matches):
        existing.attrs.update({
            "official_available": True, "warning": "",
            "source_path": "PostgreSQL:kalender_kerja",
            "document_path": str(document_path) if document_path else "",
            "extraction_audit": cached_audit,
        })
        return existing
    if document_path is None or not allow_pdf_extraction:
        result = _empty_calendar()
        warning = database_warning or (
            f"Dokumen SE tahun {int(year)} belum tersedia."
            if document_path is None
            else f"Kalender PostgreSQL tahun {int(year)} belum tersedia."
        )
        result.attrs.update({
            "official_available": False,
            "warning": warning,
            "source_path": "PostgreSQL:kalender_kerja", "document_path": "",
        })
        return result
    try:
        result = extract_calendar_from_pdf(document_path, year, data_dir, refresh)
        audit = result.attrs.get("extraction_audit", {})
        # Jangan ganti kalender tervalidasi dengan hasil OCR yang jelas lebih
        # sedikit. Hasil mentah tetap tersimpan di cache untuk audit.
        if not existing.empty and len(result) < len(existing):
            audit["candidate_rejected"] = True
            audit["candidate_count"] = len(result)
            audit["retained_calendar_count"] = len(existing)
            audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
            result = existing
        replace_work_calendar(database_engine, result)
        result = _database_calendar(year, database_engine)
        result.attrs["extraction_audit"] = audit
        warning = ""
    except Exception as exc:
        result = existing if not existing.empty else _empty_calendar()
        warning = "" if not existing.empty else "Dokumen SE ditemukan, tetapi tanggal resmi belum berhasil diekstrak."
        result.attrs["extraction_error"] = str(exc)
    result.attrs.update({
        "official_available": not result.empty, "warning": warning,
        "source_path": "PostgreSQL:kalender_kerja", "document_path": str(document_path),
    })
    return result


def build_work_calendar(
    year: int, data_dir: str | Path | None = None, allow_pdf_extraction: bool = True,
    engine: Engine | None = None,
) -> pd.DataFrame:
    """Bangun setahun kalender; tanggal khusus PostgreSQL mengalahkan pola mingguan."""
    dates = pd.date_range(f"{int(year)}-01-01", f"{int(year)}-12-31", freq="D")
    result = pd.DataFrame({"tanggal": dates, "jenis_hari": "HARI_KERJA"})
    result.loc[dates.dayofweek >= 5, "jenis_hari"] = "AKHIR_PEKAN"
    result["keterangan"] = ""
    result["dasar_hukum"] = ""
    result["nomor_dokumen"] = ""
    result["tahun"] = int(year)
    result["source"] = "DEFAULT_WEEK_PATTERN"
    result["source_url"] = ""
    result["retrieved_at"] = ""
    overrides = load_calendar_overrides(
        year, data_dir, allow_pdf_extraction=allow_pdf_extraction, engine=engine
    )
    if not overrides.empty:
        override_map = overrides.set_index("tanggal")
        for column in ["jenis_hari", "keterangan", "dasar_hukum", "nomor_dokumen", "source", "source_url", "retrieved_at"]:
            result[column] = result["tanggal"].map(override_map[column]).fillna(result[column])
    rules = result["jenis_hari"].map(DAY_RULES)
    result[["is_hari_kerja", "wajib_presensi", "eligible_tk"]] = pd.DataFrame(rules.tolist(), index=result.index)
    result = result[CALENDAR_COLUMNS]
    result.attrs.update(overrides.attrs)
    return result


def summarize_work_calendar_period(
    year_month_pairs, data_dir: str | Path | None = None, engine: Engine | None = None,
) -> dict[str, int]:
    """Hitung tanggal kalender unik untuk pasangan tahun-bulan terpilih."""
    periods = sorted({(int(year), int(month)) for year, month in year_month_pairs})
    empty = {"workdays": 0, "required_days": 0, "collective_leave": 0, "national_holidays": 0, "weekends": 0}
    if not periods:
        return empty
    calendars = [
        build_work_calendar(year, data_dir, allow_pdf_extraction=False, engine=engine)
        for year in sorted({year for year, _ in periods})
    ]
    calendar = pd.concat(calendars, ignore_index=True)
    calendar["tanggal"] = pd.to_datetime(calendar["tanggal"], errors="coerce").dt.normalize()
    calendar_periods = pd.MultiIndex.from_arrays([
        calendar["tanggal"].dt.year, calendar["tanggal"].dt.month,
    ])
    selected = calendar[calendar_periods.isin(periods)].drop_duplicates("tanggal")
    return {
        "workdays": int(selected.loc[selected["is_hari_kerja"].fillna(False).astype(bool), "tanggal"].nunique()),
        "required_days": int(selected.loc[selected["wajib_presensi"].fillna(False).astype(bool), "tanggal"].nunique()),
        "collective_leave": int(selected.loc[selected["jenis_hari"].eq("CUTI_BERSAMA"), "tanggal"].nunique()),
        "national_holidays": int(selected.loc[selected["jenis_hari"].eq("LIBUR_NASIONAL"), "tanggal"].nunique()),
        "weekends": int(selected.loc[selected["jenis_hari"].eq("AKHIR_PEKAN"), "tanggal"].nunique()),
    }


def get_calendar_day(value: object, data_dir: str | Path | None = None, engine: Engine | None = None) -> dict[str, object]:
    date = pd.Timestamp(value).normalize()
    return build_work_calendar(date.year, data_dir, engine=engine).set_index("tanggal").loc[date].to_dict()


def is_workday(value: object, data_dir: str | Path | None = None, engine: Engine | None = None) -> bool:
    return bool(get_calendar_day(value, data_dir, engine)["is_hari_kerja"])


def is_attendance_required(value: object, data_dir: str | Path | None = None, engine: Engine | None = None) -> bool:
    return bool(get_calendar_day(value, data_dir, engine)["wajib_presensi"])


def is_tk_eligible(value: object, data_dir: str | Path | None = None, engine: Engine | None = None) -> bool:
    day = get_calendar_day(value, data_dir, engine)
    return bool(day["is_hari_kerja"] and day["wajib_presensi"] and day["eligible_tk"])


def apply_work_calendar(frame: pd.DataFrame, data_dir: str | Path | None = None, engine: Engine | None = None) -> pd.DataFrame:
    """Anotasi data resmi tanpa mengubah Status/TK presensi mentah."""
    result = frame.copy()
    if result.empty or "Tanggal" not in result:
        return result
    dates = pd.to_datetime(result["Tanggal"], errors="coerce").dt.normalize()
    # Jalur pemuatan presensi hanya membaca PostgreSQL. Ekstraksi PDF merupakan urusan
    # halaman Master Kalender Kerja dan tidak dijalankan pada rerun dashboard.
    calendars = [
        build_work_calendar(int(year), data_dir, allow_pdf_extraction=False, engine=engine)
        for year in sorted(dates.dropna().dt.year.unique())
    ]
    calendar = pd.concat(calendars, ignore_index=True) if calendars else _empty_calendar()
    lookup = calendar.set_index("tanggal")
    mappings = [("jenis_hari", "jenis_hari"), ("is_hari_kerja", "is_hari_kerja"),
                ("wajib_presensi", "wajib_presensi"), ("eligible_tk", "eligible_tk"),
                ("nama_libur", "keterangan"), ("dasar_hukum_kalender", "dasar_hukum")]
    for target, source in mappings:
        result[target] = dates.map(lookup[source])
    official_tk = result.get("TK", pd.Series(False, index=result.index)).fillna(False).astype(bool)
    result["anomali_kalender_presensi"] = official_tk & ~result["eligible_tk"].fillna(False).astype(bool)
    result.attrs.update(frame.attrs)
    result.attrs["calendar_warnings"] = sorted({item.attrs.get("warning", "") for item in calendars if item.attrs.get("warning")})
    return result
