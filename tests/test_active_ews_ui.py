from streamlit.testing.v1 import AppTest


def _navigate(app: AppTest, page_name: str) -> AppTest:
    navigation = next(item for item in app.radio if item.label == "Pilih halaman")
    option = next(value for value in navigation.options if page_name in value)
    return navigation.set_value(option).run()


def test_active_ews_and_detail_do_not_expose_legacy_risk_score():
    app = AppTest.from_file("app.py", default_timeout=30)
    app.session_state["is_logged_in"] = True
    app.session_state["username"] = "test"
    app.run()

    _navigate(app, "Early Warning System")
    assert next(item for item in app.selectbox if item.label == "Bulan").value == "Agustus"
    ews_html = " ".join(str(item.value) for item in app.markdown)
    assert "Pegawai Warning" in ews_html
    assert "Kondisi Memburuk" in ews_html
    assert "Kondisi Membaik" in ews_html
    assert "Risk Score" not in ews_html
    for forbidden in ["Teguran Lisan", "Teguran Tertulis", "Pemotongan tunjangan", "Pemberhentian dengan hormat", "Indikasi Ringan"]:
        assert forbidden not in ews_html
    assert not app.exception

    next(item for item in app.selectbox if item.label == "Bulan").set_value("September").run()
    empty_month_text = " ".join(str(item.value) for item in [*app.markdown, *app.info])
    assert "Data September 2026 belum tersedia" in empty_month_text
    assert "TK SEPTEMBER" not in empty_month_text
    assert not app.exception

    _navigate(app, "Detail Pegawai")
    opd = next(item for item in app.selectbox if item.label == "OPD")
    opd.select(opd.options[0]).run()
    employee = next(item for item in app.selectbox if item.label == "Pegawai")
    employee.select(employee.options[0]).run()
    period = next(item for item in app.selectbox if item.label == "Periode")
    period.select(period.options[0]).run()
    detail_html = " ".join(str(item.value) for item in app.markdown)
    assert "Dasar Early Warning" in detail_html
    assert "Risk Score" not in detail_html
    for forbidden in ["Teguran Lisan", "Teguran Tertulis", "Pemotongan tunjangan", "Pemberhentian dengan hormat", "Indikasi Ringan"]:
        assert forbidden not in detail_html
    assert not app.exception
