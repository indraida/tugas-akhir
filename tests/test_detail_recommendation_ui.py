from streamlit.testing.v1 import AppTest

from modules.excel_parser import build_dashboard_dataframe, load_semua_presensi


def _page_text(app: AppTest) -> str:
    elements = list(app.warning) + list(app.info) + list(app.markdown) + list(app.caption)
    return " ".join(str(element.value) for element in elements)


def test_detail_recommendation_uses_same_employee_type_as_badge():
    employees = build_dashboard_dataframe(load_semua_presensi()).drop_duplicates("NIP")
    employees_by_type = {
        kind: (str(row["Unit Kerja"]), f"{row['Nama Pegawai']} — {row['NIP']}")
        for kind in ("PNS", "PPPK")
        for _, row in employees[employees["Jenis Pegawai"].eq(kind)].head(1).iterrows()
    }
    assert set(employees_by_type) == {"PNS", "PPPK"}

    app = AppTest.from_file("app.py", default_timeout=30)
    app.session_state["is_logged_in"] = True
    app.session_state["username"] = "test"
    app.run()
    next(item for item in app.radio if item.label == "Pilih halaman").set_value("👤 Detail Pegawai").run()

    pns_opd, pns_label = employees_by_type["PNS"]
    next(item for item in app.selectbox if item.label == "OPD").select(pns_opd).run()
    next(item for item in app.selectbox if item.label == "Pegawai").select(pns_label).run()
    period = next(item for item in app.selectbox if item.label == "Periode")
    period.select(period.options[0]).run()
    pns_text = _page_text(app)
    assert "Status kepegawaian PNS/non-PNS tidak tersedia" not in pns_text
    assert "employee-type-badge pns" in pns_text

    pppk_opd, pppk_label = employees_by_type["PPPK"]
    next(item for item in app.selectbox if item.label == "OPD").select(pppk_opd).run()
    next(item for item in app.selectbox if item.label == "Pegawai").select(pppk_label).run()
    period = next(item for item in app.selectbox if item.label == "Periode")
    period.select(period.options[0]).run()
    pppk_text = _page_text(app)
    assert "Status kepegawaian PNS/non-PNS tidak tersedia" not in pppk_text
    assert "Jenis pegawai terdeteksi sebagai PPPK" in pppk_text
    assert "employee-type-badge pppk" in pppk_text
    assert not app.exception


def test_detail_page_uses_one_master_month_year_period():
    app = AppTest.from_file("app.py", default_timeout=30)
    app.session_state["is_logged_in"] = True
    app.session_state["username"] = "test"
    app.run()
    navigation = next(item for item in app.radio if item.label == "Pilih halaman")
    detail_option = next(option for option in navigation.options if "Detail Pegawai" in option)
    navigation.set_value(detail_option).run()

    opd_filter = next(item for item in app.selectbox if item.label == "OPD")
    employee_filter = next(item for item in app.selectbox if item.label == "Pegawai")
    period_filters = [item for item in app.selectbox if item.label == "Periode"]
    assert "Semua OPD" not in opd_filter.options
    assert opd_filter.value is None
    assert employee_filter.value is None
    assert employee_filter.disabled
    assert len(period_filters) == 1
    assert period_filters[0].value is None
    assert period_filters[0].disabled

    opd_filter.select(opd_filter.options[0]).run()
    employee_filter = next(item for item in app.selectbox if item.label == "Pegawai")
    employee_filter.select(employee_filter.options[0]).run()
    period_filters = [item for item in app.selectbox if item.label == "Periode"]
    assert period_filters[0].options
    assert all(option.rsplit(" ", 1)[-1].isdigit() for option in period_filters[0].options)

    chosen_period = period_filters[0].options[0]
    period_filters[0].select(chosen_period).run()
    page_text = _page_text(app)
    assert f"Riwayat presensi pegawai pada {chosen_period}." in page_text
    assert len([item for item in app.selectbox if item.label == "Periode"]) == 1
    assert any(item.label == "Status" for item in app.selectbox)
    assert not app.exception
