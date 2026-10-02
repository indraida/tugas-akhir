"""Role-based access control murni; tidak bergantung pada data presensi."""

from __future__ import annotations

from typing import Final

ADMIN: Final = "ADMIN"
OPERATOR: Final = "OPERATOR"
PIMPINAN: Final = "PIMPINAN"

VIEW_PAGE: Final = "VIEW_PAGE"
IMPORT_ATTENDANCE: Final = "IMPORT_ATTENDANCE"
EDIT_MASTER: Final = "EDIT_MASTER"
UPDATE_ACTION_CENTER: Final = "UPDATE_ACTION_CENTER"
DOWNLOAD_REPORT: Final = "DOWNLOAD_REPORT"
VIEW_AUDIT: Final = "VIEW_AUDIT"
MANAGE_USERS: Final = "MANAGE_USERS"

ALL_PAGES: Final[tuple[str, ...]] = (
    "Executive Dashboard", "Early Warning System", "Analisis Presensi",
    "Analisis OPD", "Detail Pegawai", "Laporan Ketidakhadiran",
    "Master Kalender Kerja", "Master OPD", "Master Pegawai",
    "Data Presensi", "Action Center", "Audit Trail", "Manajemen Pengguna",
)

MONITORING_PAGES: Final[tuple[str, ...]] = (
    "Executive Dashboard", "Early Warning System", "Analisis Presensi",
    "Analisis OPD", "Detail Pegawai", "Laporan Ketidakhadiran", "Action Center",
)

ROLE_CONFIG: Final = {
    ADMIN: {
        "pages": ALL_PAGES,
        "permissions": frozenset({
            VIEW_PAGE, IMPORT_ATTENDANCE, EDIT_MASTER, UPDATE_ACTION_CENTER,
            DOWNLOAD_REPORT, VIEW_AUDIT, MANAGE_USERS,
        }),
    },
    OPERATOR: {
        "pages": tuple(page for page in ALL_PAGES if page != "Manajemen Pengguna"),
        "permissions": frozenset({
            VIEW_PAGE, IMPORT_ATTENDANCE, UPDATE_ACTION_CENTER,
            DOWNLOAD_REPORT, VIEW_AUDIT,
        }),
    },
    PIMPINAN: {
        "pages": MONITORING_PAGES,
        "permissions": frozenset({VIEW_PAGE, DOWNLOAD_REPORT}),
    },
}


def normalize_role(role: object) -> str | None:
    value = "" if role is None else str(role).strip().upper()
    return value if value in ROLE_CONFIG else None


def get_allowed_pages(role: object) -> tuple[str, ...]:
    canonical = normalize_role(role)
    return tuple(ROLE_CONFIG[canonical]["pages"]) if canonical else ()


def can_access_page(role: object, page_name: str) -> bool:
    return str(page_name) in get_allowed_pages(role)


def has_permission(role: object, permission: str) -> bool:
    canonical = normalize_role(role)
    return bool(canonical and permission in ROLE_CONFIG[canonical]["permissions"])


def require_page_access(role: object, page_name: str) -> None:
    if not can_access_page(role, page_name):
        raise PermissionError("Akses tidak tersedia untuk peran pengguna Anda.")


def require_permission(role: object, permission: str) -> None:
    if not has_permission(role, permission):
        raise PermissionError("Aksi tidak tersedia untuk peran pengguna Anda.")
