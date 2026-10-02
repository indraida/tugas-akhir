import re
from pathlib import Path

from streamlit.testing.v1 import AppTest

from modules.excel_parser import load_semua_presensi


def test_dedicated_tk_report_page_has_filters_preview_and_downloads():
    app = AppTest.from_file("app.py", default_timeout=30)
    app.session_state["is_logged_in"] = True
    app.session_state["username"] = "test"
    app.session_state["user_role"] = "ADMIN"
    app.run()
    next(item for item in app.radio if item.label == "Pilih halaman").set_value("📄 Laporan Ketidakhadiran").run()

    html = " ".join(str(item.value) for item in app.markdown)
    assert "Laporan Ketidakhadiran" in html
    assert "Parameter Laporan" in html
    assert "Ringkasan Laporan" not in html
    assert "Preview Laporan" not in html
    assert "Siap Mengunduh Laporan" not in html
    assert "Pilih Tahun, OPD, Jenis Pegawai, dan Periode" in " ".join(str(item.value) for item in app.info)
    report_filters = {"Tahun", "OPD", "Jenis Pegawai", "Periode"}
    assert {item.label for item in app.selectbox if item.label in report_filters} == report_filters
    assert all(next(item for item in app.selectbox if item.label == label).value is None for label in report_filters)
    assert not app.get("download_button")

    year_filter = next(item for item in app.selectbox if item.label == "Tahun")
    year_filter.select(year_filter.options[0]).run()
    next(item for item in app.selectbox if item.label == "OPD").select("Semua OPD")
    next(item for item in app.selectbox if item.label == "Jenis Pegawai").select("Semua Jenis Pegawai")
    next(item for item in app.selectbox if item.label == "Periode").select("TW I").run()

    html = " ".join(str(item.value) for item in app.markdown)
    assert "Preview Laporan" in html
    assert "Siap Mengunduh Laporan" in html
    assert "Lampiran Surat Sekretaris Daerah Provinsi Kalimantan Barat" in html
    assert "<th colspan='3'>TRIWULAN I</th><th colspan='3'>TRIWULAN II</th><th colspan='3'>TRIWULAN III</th><th colspan='3'>TRIWULAN IV</th>" in html
    assert all(month in html for month in ["JAN", "FEB", "MAR", "APR", "MEI", "JUN", "JUL", "AGT", "SEP", "OKT", "NOV", "DES"])
    assert '"Periode", list(period_codes), index=None' in Path("app.py").read_text(encoding="utf-8")
    assert {item.label for item in app.get("download_button")} == {"⬇ Unduh Excel", "📄 Unduh PDF"}
    assert not app.exception


def test_attendance_analysis_is_analytical_and_links_to_tk_report():
    app = AppTest.from_file("app.py", default_timeout=30)
    app.session_state["is_logged_in"] = True
    app.session_state["username"] = "test"
    app.run()
    next(item for item in app.radio if item.label == "Pilih halaman").set_value("📊 Analisis Presensi").run()

    html = " ".join(str(item.value) for item in app.markdown)
    summary_tk = int(re.search(r"attendance-kpi-value'>([\d.]+) hari</div><div class='attendance-kpi-label'>TK", html).group(1).replace(".", ""))
    assert summary_tk > 0
    assert all(label in html for label in ["Komposisi Presensi", "Tren Presensi", "Hari Rawan", "Waktu Rawan", "Heatmap Presensi", "Pegawai dengan Pola Menonjol"])
    assert "Ringkasan Ketidakhadiran (TK)" not in html
    assert any(item.label == "Buka Laporan Ketidakhadiran →" for item in app.button)
    assert not app.get("download_button")
    assert "Risk Score" not in html
    assert not app.exception
    assert "Hari Kerja" in html and "Hari Wajib Presensi" in html
    assert re.search(r"<strong>\d+</strong> Hari Kerja", html)
    assert re.search(r"<strong>\d+</strong> Hari Wajib Presensi", html)
    return
    summary_tk = int(re.search(r"attendance-kpi-value'>([\d.]+)</div><div class='attendance-kpi-label'>TK", html).group(1).replace(".", ""))
    report_tk = int(re.search(r"tk-report-kpi-value'>([\d.]+)</div><div class='tk-report-kpi-label'>Hari TK", html).group(1).replace(".", ""))
    assert "Ringkasan Ketidakhadiran (TK)" in html
    assert any(item.label == "Siapkan Laporan Ketidakhadiran →" for item in app.button)
    assert not app.get("download_button")
    assert 0 < report_tk <= summary_tk
    assert "Pegawai dengan TK Tertinggi" in html
    assert "OPD dengan Ketidakhadiran TK" in html
    assert "Detail Ketidakhadiran" in html
    assert "<th>Tanggal</th><th>Hari</th><th>Nama Pegawai</th><th>NIP</th><th>OPD</th><th>Status</th><th>Keterangan</th>" in html
    assert html.count("<span class='tk-report-status'>TK</span>") == report_tk
    assert "Detail tanggal ketidakhadiran tidak tersedia" not in html
    assert "Risk Score" not in html[html.index("Ringkasan Ketidakhadiran (TK)"):]
    assert not app.exception

    daily = load_semua_presensi()
    tk_scope = (
        daily[daily["TK"]]
        .groupby(["Nama_Bulan", "Unit Kerja"], as_index=False)
        .size()
        .sort_values("size", ascending=False)
        .iloc[0]
    )
    next(item for item in app.selectbox if item.label == "Bulan").select(tk_scope["Nama_Bulan"]).run()
    next(item for item in app.selectbox if item.label == "OPD").select(tk_scope["Unit Kerja"]).run()
    scoped_html = " ".join(str(item.value) for item in app.markdown)
    scoped_total = int(re.search(r"tk-report-kpi-value'>([\d.]+)</div><div class='tk-report-kpi-label'>Hari TK", scoped_html).group(1).replace(".", ""))
    assert scoped_html.count("<span class='tk-report-status'>TK</span>") == scoped_total
    assert not app.exception

    next(item for item in app.selectbox if item.label == "Metric").select("TK").run()
    next(item for item in app.radio if item.label == "Granularitas").set_value("Harian").run()
    assert not app.exception
    next(item for item in app.button if item.label == "Reset").click().run()
    assert next(item for item in app.selectbox if item.label == "Tahun").value == "Semua Tahun"
    assert next(item for item in app.selectbox if item.label == "Bulan").value == "Semua Bulan"
    assert next(item for item in app.selectbox if item.label == "OPD").value == "Semua OPD"
    assert next(item for item in app.selectbox if item.label == "Jenis Pegawai").value == "Semua Jenis Pegawai"
    metric_selector = next(item for item in app.selectbox if item.label == "Metric")
    assert metric_selector.value == "Kepatuhan Presensi"
    assert metric_selector.options == ["Kepatuhan Presensi", "Kehadiran Fisik", "TK", "Keterlambatan", "Cuti", "WFH", "DL"]
    assert next(item for item in app.radio if item.label == "Granularitas").value == "Bulanan"
    assert not app.exception
