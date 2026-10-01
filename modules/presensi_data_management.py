"""Modul antarmuka dan pengelolaan Data Presensi & Master Periode berbasis PostgreSQL."""

from __future__ import annotations

import io
import logging
from datetime import date, datetime
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy.engine import Engine

from database.opd import list_opds
from database.pegawai import list_pegawai
from database.periode import (
    MONTH_NAMES_ID,
    create_periode,
    delete_periode,
    get_or_create_periode,
    list_periode,
    update_periode,
)
from database.presensi import (
    create_presensi_manual,
    delete_presensi_record,
    list_presensi_data,
    save_presensi_dataframe_to_db,
)
from database.repository import list_available_attendance_periods, save_daily_attendance_to_db
from modules.attendance_import import build_import_preview
from services.activity_log import log_system_activity
from services.data_source import load_daily_data

LOGGER = logging.getLogger(__name__)

STATUS_BADGE_ICONS = {
    "Hadir": "🟢 Hadir",
    "Terlambat": "🟡 Terlambat",
    "TK": "🔴 TK",
    "Cuti": "🔵 Cuti",
    "WFH": "🟣 WFH",
    "WFA": "🟣 WFA",
    "DL": "🟠 Dinas Luar",
    "Libur": "⚪ Libur",
}


def _period_labels(periods: list[tuple[int, int]]) -> list[str]:
    return [f"{MONTH_NAMES_ID.get(month, month)} {year}" for year, month in periods]


def _format_count_id(value: int) -> str:
    """Format bilangan bulat dengan pemisah ribuan Indonesia."""
    return f"{int(value):,}".replace(",", ".")


def _import_flash_message(payload: dict[str, int]) -> str:
    parts = [
        f"{_format_count_id(payload.get('validated', 0))} data divalidasi",
        f"{_format_count_id(payload.get('processed', 0))} berhasil diproses",
    ]
    inserted = int(payload.get("inserted", 0))
    updated = int(payload.get("updated", 0))
    skipped = int(payload.get("skipped", 0))
    failed = int(payload.get("failed", 0))
    if inserted:
        parts.append(f"{_format_count_id(inserted)} data baru")
    if updated:
        parts.append(f"{_format_count_id(updated)} diperbarui")
    if skipped:
        parts.append(f"{_format_count_id(skipped)} duplikat/invalid dilewati")
    if failed:
        parts.append(f"{_format_count_id(failed)} gagal")
    return "Import data presensi berhasil — " + " • ".join(parts)


def render_attendance_import(engine: Engine, current_username: str) -> None:
    """UI upload -> preview -> validasi -> explicit transactional UPSERT."""
    flash = st.session_state.pop("attendance_import_flash", None)
    if flash:
        st.toast(_import_flash_message(flash), icon="✅")

    uploader_version = int(st.session_state.get("attendance_import_version", 0))
    st.markdown("#### Import Data Presensi")
    st.caption("Unggah laporan rekap presensi BKD. Data hanya disimpan setelah tombol import ditekan.")
    uploads = st.file_uploader(
        "Upload file presensi Excel",
        type=["xlsx"],
        accept_multiple_files=True,
        key=f"attendance_excel_uploads_{uploader_version}",
    )
    preview = None
    if uploads:
        try:
            preview = build_import_preview(uploads, engine)
        except (ValueError, KeyError, OSError) as exc:
            message = str(exc)
            if "data pegawai valid" in message.lower():
                message = "Kolom wajib NIP tidak ditemukan atau tidak berisi NIP yang valid."
            st.error(f"File belum dapat diimport: {message}")
        except Exception:
            LOGGER.exception("Gagal membaca upload presensi")
            st.error("File belum dapat dibaca. Pastikan file .xlsx memakai format laporan presensi yang benar.")

    if preview is not None:
        dates = pd.to_datetime(preview.data["Tanggal"], errors="coerce")
        period_labels = _period_labels(preview.periods)
        period_text = ", ".join(period_labels) or "-"
        if len(period_labels) > 1:
            st.warning(f"File berisi beberapa periode: {period_text}")
        st.markdown("**File terpilih:** " + ", ".join(preview.files))
        st.markdown(f"**Periode terdeteksi:** {period_text}")
        info_cols = st.columns(4)
        info_cols[0].metric("Total Baris", f"{len(preview.data):,}")
        info_cols[1].metric("Pegawai Unik", f"{preview.data['NIP'].nunique():,}")
        info_cols[2].metric("Data Valid", f"{len(preview.valid_data):,}")
        info_cols[3].metric("Perlu Diperiksa", f"{len(preview.invalid_data):,}")
        st.caption(
            f"Tanggal: {dates.min().strftime('%d %B %Y') if dates.notna().any() else '-'} s.d. "
            f"{dates.max().strftime('%d %B %Y') if dates.notna().any() else '-'} • "
            f"OPD: {', '.join(sorted(preview.data['Unit Kerja'].dropna().astype(str).unique()))}"
        )

        st.markdown("##### Preview Data")
        preview_columns = ["NIP", "Nama", "Unit Kerja", "Tanggal", "Jam_Masuk", "Jam_Pulang", "Status", "Menit_Terlambat", "Sumber_File"]
        st.dataframe(preview.data[preview_columns].head(50), hide_index=True, use_container_width=True)
        st.markdown("##### Validasi")
        st.success("✓ Struktur file, NIP, tanggal, status, OPD, dan keterlambatan telah diperiksa.")
        st.write(f"Duplikat dalam file: **{preview.duplicate_count:,}**")
        st.write(f"Data existing yang akan diperbarui: **{preview.existing_count:,}**")
        st.write(f"Data baru: **{max(0, len(preview.valid_data) - preview.existing_count):,}**")
        if not preview.invalid_data.empty:
            st.markdown("##### Data Perlu Diperiksa")
            invalid_columns = [column for column in preview_columns + ["Alasan"] if column in preview.invalid_data]
            st.dataframe(preview.invalid_data[invalid_columns].head(50), hide_index=True, use_container_width=True)

        confirmed = st.checkbox(
            "Saya sudah memeriksa preview data",
            key=f"attendance_import_confirmed_{uploader_version}",
        )
        import_clicked = st.button(
            "Import ke Database",
            type="primary",
            disabled=not confirmed or preview.valid_data.empty,
            key=f"attendance_import_submit_{uploader_version}",
        )
        if import_clicked:
            with st.spinner("Mengimpor data presensi ke PostgreSQL..."):
                try:
                    result = save_daily_attendance_to_db(preview.valid_data, engine)
                    relational_result = save_presensi_dataframe_to_db(preview.valid_data, engine)
                    try:
                        log_system_activity(
                            "IMPORT_DATA_PRESENSI", "Import Data Presensi",
                            f"User '{current_username}' mengimpor {result['valid']} record presensi.",
                            metadata={
                                "files": preview.files,
                                "periods": period_labels,
                                "presensi_harian": result,
                                "presensi_relational": relational_result,
                            },
                            module="presensi",
                        )
                    except Exception:
                        LOGGER.exception("Audit trail import presensi gagal ditulis")
                    st.session_state["attendance_import_flash"] = {
                        "validated": len(preview.data),
                        "processed": int(result["valid"]),
                        "inserted": int(result["inserted"]),
                        "updated": int(result["updated"]),
                        "skipped": len(preview.invalid_data),
                        "failed": int(result["rejected"]),
                    }
                    st.session_state["attendance_import_version"] = uploader_version + 1
                    st.rerun()
                except Exception:
                    LOGGER.exception("Import data presensi gagal")
                    st.error("Import belum berhasil. Periksa format file atau koneksi database.")

    st.markdown("##### Periode Data Tersedia")
    try:
        available = _period_labels(list_available_attendance_periods(engine))
        st.write(", ".join(available) if available else "Belum ada periode presensi di database.")
    except Exception:
        LOGGER.exception("Gagal memuat periode presensi aktual")
        st.warning("Periode data tersedia belum dapat dimuat.")
    st.divider()


def render_presensi_kpis(df: pd.DataFrame) -> None:
    """Tampilkan kartu metrik ringkasan data presensi."""
    total = len(df)
    if total == 0:
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Presensi", "0 Data")
        col2.metric("Hadir Tepat Waktu", "0 Data")
        col3.metric("Terlambat", "0 Data")
        col4.metric("Tanpa Keterangan (TK)", "0 Data")
        return

    statuses = df["status_presensi"].fillna("").astype(str)
    hadir = int(statuses.eq("Hadir").sum())
    late = int(statuses.str.contains("Terlambat", case=False).sum())
    tk = int(statuses.eq("TK").sum())
    cuti_dl = int(statuses.str.contains("Cuti|DL|WFH|WFA", case=False).sum())

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Log Presensi", f"{total:,} Record")
    with col2:
        st.metric("Hadir Tepat Waktu", f"{hadir:,} ({hadir/total*100:.1f}%)" if total > 0 else "0")
    with col3:
        st.metric("Terlambat", f"{late:,} ({late/total*100:.1f}%)" if total > 0 else "0")
    with col4:
        st.metric("Tanpa Keterangan (TK)", f"{tk:,} ({tk/total*100:.1f}%)" if total > 0 else "0")


def show_presensi_data_page(engine: Engine) -> None:
    """Tampilan utama modul Data Presensi & Periode."""
    st.markdown(
        """
        <div style="margin-bottom: 1.2rem;">
            <h1 style="font-size: 1.8rem; font-weight: 700; color: #1e293b; margin-bottom: 0.2rem;">
                📋 Data Presensi & Master Periode
            </h1>
            <p style="color: #64748b; font-size: 0.95rem; margin-top: 0;">
                Kelola dan pantau catatan presensi harian pegawai, keterlambatan, jam kerja, serta konfigurasi master periode yang tersimpan di database PostgreSQL.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    current_role = st.session_state.get("user_role", "admin").lower()
    current_username = st.session_state.get("username", "")

    render_attendance_import(engine, current_username)

    try:
        periode_list = list_periode(engine)
        opd_list = list_opds(engine, aktif_only=True)
        pegawai_list = list_pegawai(engine, aktif_only=True)
    except Exception as exc:
        st.error(f"❌ Gagal memuat referensi dari database PostgreSQL: {exc}")
        return

    tab_data, tab_periode, tab_entri, tab_sync = st.tabs([
        "📊 Log Data Presensi",
        "📅 Master Periode",
        "➕ Entri Presensi Manual",
        "⚡ Sinkronisasi Database",
    ])

    # ========================================================================
    # TAB 1: LOG DATA PRESENSI
    # ========================================================================
    with tab_data:
        col_p, col_o, col_st, col_l = st.columns([1.5, 1.5, 1, 1])
        with col_p:
            periode_options = {"Semua Periode": None}
            for pr in periode_list:
                periode_options[f"{pr['nama_bulan']} {pr['tahun']} ({pr['keterangan'] or '-'})"] = pr["id_periode"]
            sel_periode_label = st.selectbox("Pilih Periode", list(periode_options.keys()), key="presensi_filter_periode")
            sel_id_periode = periode_options[sel_periode_label]

        with col_o:
            opd_options = {"Semua OPD": None}
            for op in opd_list:
                opd_options[f"[{op['kode'] or '-'}] {op['nama']}"] = op["id"]
            sel_opd_label = st.selectbox("Pilih OPD", list(opd_options.keys()), key="presensi_filter_opd")
            sel_id_opd = opd_options[sel_opd_label]

        with col_st:
            status_opts = ["Semua Status", "Hadir", "Terlambat", "TK", "Cuti", "WFH", "DL", "Libur"]
            sel_status = st.selectbox("Status Presensi", status_opts, key="presensi_filter_status")

        with col_l:
            limit_opt = st.selectbox("Batas Data", [500, 1000, 2000, 5000], index=1, key="presensi_filter_limit")

        with st.spinner("Memuat data presensi dari PostgreSQL..."):
            df_presensi = list_presensi_data(
                engine,
                id_periode=sel_id_periode,
                id_opd=sel_id_opd,
                status=sel_status if sel_status != "Semua Status" else None,
                limit=limit_opt,
            )

        render_presensi_kpis(df_presensi)
        st.markdown("---")

        if df_presensi.empty:
            st.info("Belum ada data presensi yang sesuai dengan filter yang dipilih.")
        else:
            display_df = df_presensi.copy()
            display_df["tanggal_presensi"] = pd.to_datetime(display_df["tanggal_presensi"]).dt.strftime("%d-%m-%Y")
            display_df["jam_masuk"] = display_df["jam_masuk"].map(lambda v: v.strftime("%H:%M") if hasattr(v, "strftime") else (str(v) if v else "-"))
            display_df["jam_pulang"] = display_df["jam_pulang"].map(lambda v: v.strftime("%H:%M") if hasattr(v, "strftime") else (str(v) if v else "-"))
            display_df["status_display"] = display_df["status_presensi"].map(lambda s: STATUS_BADGE_ICONS.get(s, s))

            cols_show = [
                "id_presensi", "nip", "nama_pegawai", "nama_opd", "tanggal_presensi",
                "jam_masuk", "jam_pulang", "status_display", "keterlambatan_menit",
                "sumber_data", "waktu_update",
            ]
            cols_available = [c for c in cols_show if c in display_df.columns]

            st.dataframe(
                display_df[cols_available],
                hide_index=True,
                use_container_width=True,
                column_config={
                    "id_presensi": st.column_config.NumberColumn("ID", width="small"),
                    "nip": st.column_config.TextColumn("NIP Pegawai", width="medium"),
                    "nama_pegawai": st.column_config.TextColumn("Nama Pegawai", width="large"),
                    "nama_opd": st.column_config.TextColumn("Unit Kerja (OPD)", width="medium"),
                    "tanggal_presensi": st.column_config.TextColumn("Tanggal", width="small"),
                    "jam_masuk": st.column_config.TextColumn("Masuk", width="small"),
                    "jam_pulang": st.column_config.TextColumn("Pulang", width="small"),
                    "status_display": st.column_config.TextColumn("Status", width="medium"),
                    "keterlambatan_menit": st.column_config.NumberColumn("Terlambat (Menit)", width="small"),
                    "sumber_data": st.column_config.TextColumn("Sumber Data", width="small"),
                    "waktu_update": st.column_config.DatetimeColumn("Waktu Update", format="DD-MM-YYYY HH:mm", width="medium"),
                },
            )

            # Export Excel button
            excel_bytes = io.BytesIO()
            with pd.ExcelWriter(excel_bytes, engine="openpyxl") as writer:
                display_df.to_excel(writer, index=False, sheet_name="Data Presensi")
            st.download_button(
                label="📥 Unduh Data Presensi (.xlsx)",
                data=excel_bytes.getvalue(),
                file_name="data_presensi_pegawai.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="download_presensi_xlsx",
            )

    # ========================================================================
    # TAB 2: MASTER PERIODE
    # ========================================================================
    with tab_periode:
        st.markdown("#### 📅 Master Data Periode Presensi")
        st.caption("Data periode menentukan rentang tanggal bulanan untuk agregasi kehadiran dan kepatuhan.")

        col_tbl_p, col_form_p = st.columns([2, 1])
        with col_tbl_p:
            if not periode_list:
                st.info("Belum ada periode yang terdaftar di database.")
            else:
                p_rows = []
                for pr in periode_list:
                    p_rows.append({
                        "ID": pr["id_periode"],
                        "Bulan": pr["nama_bulan"],
                        "Tahun": pr["tahun"],
                        "Tanggal Mulai": pr["tanggal_mulai"].strftime("%d-%m-%Y") if hasattr(pr["tanggal_mulai"], "strftime") else str(pr["tanggal_mulai"]),
                        "Tanggal Selesai": pr["tanggal_selesai"].strftime("%d-%m-%Y") if hasattr(pr["tanggal_selesai"], "strftime") else str(pr["tanggal_selesai"]),
                        "Keterangan": pr["keterangan"],
                    })
                st.dataframe(pd.DataFrame(p_rows), hide_index=True, use_container_width=True)

        with col_form_p:
            st.markdown("##### ➕ Tambah Periode Baru")
            with st.form("create_periode_form", clear_on_submit=True):
                cur_year = datetime.now().year
                p_bulan = st.selectbox("Bulan *", list(range(1, 13)), format_func=lambda b: MONTH_NAMES_ID.get(b, str(b)))
                p_tahun = st.number_input("Tahun *", min_value=2020, max_value=2030, value=cur_year)
                p_ket = st.text_input("Keterangan", placeholder="misal: Periode Ganjil 2026")
                submit_periode = st.form_submit_button("💾 Simpan Periode", use_container_width=True)

            if submit_periode:
                try:
                    res_p = create_periode(engine, {"bulan": p_bulan, "tahun": p_tahun, "keterangan": p_ket})
                    log_system_activity(
                        "periode_management", "Tambah Periode",
                        f"User '{current_username}' membuat periode {MONTH_NAMES_ID.get(p_bulan, p_bulan)} {p_tahun}.",
                        metadata={"bulan": p_bulan, "tahun": p_tahun},
                    )
                    st.success(f"✓ Periode {MONTH_NAMES_ID.get(p_bulan, p_bulan)} {p_tahun} berhasil disimpan!")
                    st.rerun()
                except ValueError as v_err:
                    st.error(str(v_err))
                except Exception as err:
                    st.error(f"Gagal menyimpan periode: {err}")

    # ========================================================================
    # TAB 3: ENTRI PRESENSI MANUAL
    # ========================================================================
    with tab_entri:
        st.markdown("#### ➕ Entri Catatan Presensi Manual Pegawai")
        st.caption("Gunakan form ini untuk mencatat presensi khusus atau penyesuaian individual.")

        with st.form("create_presensi_manual_form"):
            col_en1, col_en2 = st.columns(2)
            with col_en1:
                peg_map = {f"{p['nip']} — {p['nama_pegawai']} ({p['nama_opd']})": p["id_pegawai"] for p in pegawai_list}
                if not peg_map:
                    st.warning("Belum ada data pegawai di Master Pegawai.")
                    sel_peg_label = None
                    sel_peg_id = None
                else:
                    sel_peg_label = st.selectbox("Pilih Pegawai *", list(peg_map.keys()))
                    sel_peg_id = peg_map[sel_peg_label]

                tgl_entry = st.date_input("Tanggal Presensi *", value=date.today())
                st_entry = st.selectbox("Status Presensi *", ["Hadir", "Terlambat", "TK", "Cuti", "WFH", "DL", "Libur"])

            with col_en2:
                col_t1, col_t2 = st.columns(2)
                with col_t1:
                    jam_m_entry = st.text_input("Jam Masuk (HH:MM)", value="07:30")
                with col_t2:
                    jam_p_entry = st.text_input("Jam Pulang (HH:MM)", value="16:00")

                late_entry = st.number_input("Keterlambatan (Menit)", min_value=0, max_value=480, value=0)
                sumber_entry = st.text_input("Sumber Data", value="Manual Input")

            submit_manual_presensi = st.form_submit_button("💾 Simpan Presensi Pegawai", use_container_width=True)

        if submit_manual_presensi:
            if not sel_peg_id:
                st.error("Pegawai wajib dipilih.")
            else:
                try:
                    payload = {
                        "id_pegawai": sel_peg_id,
                        "tanggal_presensi": tgl_entry,
                        "jam_masuk": jam_m_entry.strip() if jam_m_entry.strip() else None,
                        "jam_pulang": jam_p_entry.strip() if jam_p_entry.strip() else None,
                        "status_presensi": st_entry,
                        "keterlambatan_menit": late_entry,
                        "sumber_data": sumber_entry.strip() or "Manual Input",
                    }
                    res_m = create_presensi_manual(engine, payload)
                    log_system_activity(
                        "presensi_management", "Entri Presensi Manual",
                        f"User '{current_username}' mencatat presensi {st_entry} tanggal {tgl_entry} untuk ID Pegawai {sel_peg_id}.",
                        metadata={"id_pegawai": sel_peg_id, "tanggal": str(tgl_entry), "status": st_entry},
                    )
                    st.success("✓ Data presensi berhasil disimpan ke PostgreSQL!")
                    st.rerun()
                except Exception as e_man:
                    st.error(f"Gagal menyimpan data presensi: {e_man}")

    # ========================================================================
    # TAB 4: SINKRONISASI DATABASE
    # ========================================================================
    with tab_sync:
        st.markdown("#### ⚡ Sinkronisasi & Migrasi Data Presensi Harian ke PostgreSQL")
        st.markdown(
            "Fitur ini membaca data presensi harian yang tersedia, memvalidasi NIP ke Master Pegawai, mengelompokkan ke Master Periode, dan melakukan **UPSERT** secara otomatis ke tabel `presensi` di PostgreSQL."
        )

        if st.button("🚀 Mulai Sinkronisasi ke PostgreSQL", type="primary", use_container_width=True, key="btn_sync_presensi"):
            with st.spinner("Sedang memproses sinkronisasi data presensi..."):
                try:
                    daily_data = load_daily_data()
                    if daily_data.empty:
                        st.warning("Tidak ada data presensi yang ditemukan untuk disinkronkan.")
                    else:
                        sync_res = save_presensi_dataframe_to_db(daily_data, engine)
                        log_system_activity(
                            "presensi_management", "Sinkronisasi Presensi",
                            f"User '{current_username}' melakukan sinkronisasi {sync_res['processed']} record presensi ke PostgreSQL.",
                            metadata={"result": sync_res},
                        )
                        st.success(
                            f"🎉 Sinkronisasi Selesai!\n\n"
                            f"- **Total Record Diproses**: {sync_res['processed']:,}\n"
                            f"- **Insert Baru**: {sync_res['inserted']:,}\n"
                            f"- **Update Data**: {sync_res['updated']:,}\n"
                            f"- **Ditolak / Invalid**: {sync_res['rejected']:,}"
                        )
                        st.rerun()
                except Exception as e_sync:
                    st.error(f"Gagal melakukan sinkronisasi: {e_sync}")
