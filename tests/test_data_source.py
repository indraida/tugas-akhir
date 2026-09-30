from datetime import date, time

import pandas as pd

from modules.excel_parser import build_dashboard_dataframe
from services.data_source import active_source_name, postgres_to_canonical


def test_default_source_is_postgres(monkeypatch):
    monkeypatch.delenv("DATA_SOURCE", raising=False)
    assert active_source_name() == "PostgreSQL"


def test_explicit_excel_source(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "excel")
    assert active_source_name() == "Excel"


def test_postgres_adapter_produces_canonical_status_and_kpi():
    db = pd.DataFrame([{
        "nip": "001", "nama_pegawai": "A", "opd": "OPD A", "tanggal": date(2026, 1, 2),
        "jam_masuk": time(7, 30), "jam_pulang": time(16, 0), "status_presensi": "TK",
        "keterlambatan_menit": 0, "periode_bulan": 1, "periode_tahun": 2026,
        "sumber_file": "db",
    }])
    daily = postgres_to_canonical(db)
    assert bool(daily.iloc[0]["TK"])
    dashboard = build_dashboard_dataframe(daily)
    assert int(dashboard.iloc[0]["TK"]) == 1
    assert int(dashboard.iloc[0]["Hari Kerja"]) == 1
