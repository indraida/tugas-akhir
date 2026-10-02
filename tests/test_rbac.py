import pytest
import pandas as pd

from modules.excel_parser import build_dashboard_dataframe

from services.rbac import (
    ADMIN, OPERATOR, PIMPINAN, ALL_PAGES, EDIT_MASTER, IMPORT_ATTENDANCE,
    MANAGE_USERS, MONITORING_PAGES, UPDATE_ACTION_CENTER, VIEW_AUDIT,
    can_access_page, get_allowed_pages, has_permission, normalize_role,
    require_page_access, require_permission,
)


def test_roles_are_normalized_without_admin_fallback():
    assert normalize_role(" admin ") == ADMIN
    assert normalize_role("Operator") == OPERATOR
    assert normalize_role("PIMPINAN") == PIMPINAN
    assert normalize_role(None) is None
    assert normalize_role("") is None
    assert normalize_role("UNKNOWN") is None


def test_admin_operator_and_pimpinan_menu_matrix():
    assert get_allowed_pages(ADMIN) == ALL_PAGES
    assert get_allowed_pages(OPERATOR) == tuple(
        page for page in ALL_PAGES if page != "Manajemen Pengguna"
    )
    assert get_allowed_pages(PIMPINAN) == MONITORING_PAGES


def test_action_permission_matrix():
    assert has_permission(ADMIN, IMPORT_ATTENDANCE)
    assert has_permission(ADMIN, EDIT_MASTER)
    assert has_permission(ADMIN, MANAGE_USERS)
    assert has_permission(OPERATOR, IMPORT_ATTENDANCE)
    assert has_permission(OPERATOR, UPDATE_ACTION_CENTER)
    assert has_permission(OPERATOR, VIEW_AUDIT)
    assert not has_permission(OPERATOR, EDIT_MASTER)
    assert not has_permission(OPERATOR, MANAGE_USERS)
    assert not has_permission(PIMPINAN, IMPORT_ATTENDANCE)
    assert not has_permission(PIMPINAN, UPDATE_ACTION_CENTER)
    assert not has_permission(PIMPINAN, VIEW_AUDIT)


def test_direct_access_and_unknown_role_are_denied():
    assert not can_access_page(PIMPINAN, "Data Presensi")
    assert not can_access_page(PIMPINAN, "Audit Trail")
    assert not can_access_page(PIMPINAN, "Manajemen Pengguna")
    assert not can_access_page(OPERATOR, "Manajemen Pengguna")
    assert get_allowed_pages("UNKNOWN") == ()
    with pytest.raises(PermissionError):
        require_page_access(PIMPINAN, "Data Presensi")
    with pytest.raises(PermissionError):
        require_permission(PIMPINAN, IMPORT_ATTENDANCE)


def test_role_does_not_change_attendance_result():
    daily = pd.DataFrame([{
        "NIP": "001", "Nama": "Pegawai", "Unit Kerja": "OPD A",
        "Tahun": 2026, "Bulan": 9, "Nama_Bulan": "September",
        "Sumber_Datang": "TK", "Sumber_Pulang": "TK", "Status": "TK",
        "TK": True, "Terlambat": False, "Jam_Masuk": "",
        "Hari": "Senin", "Jenis Pegawai": "PNS",
        "Jenis Pegawai Source": "NIP",
    }])
    baseline = build_dashboard_dataframe(daily)
    for role in (ADMIN, OPERATOR, PIMPINAN):
        assert can_access_page(role, "Executive Dashboard")
        pd.testing.assert_frame_equal(build_dashboard_dataframe(daily), baseline)
