"""Modul antarmuka dan pengelolaan Master Data Pegawai berbasis PostgreSQL & Excel Import."""

from __future__ import annotations

import io
import logging
from html import escape
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy.engine import Engine

from database.opd import list_opds
from database.pegawai import (
    JENIS_KELAMIN_CHOICES,
    STATUS_PEGAWAI_CHOICES,
    create_pegawai,
    delete_pegawai,
    generate_pegawai_excel_template,
    get_pegawai_by_id,
    import_pegawai_from_excel,
    list_pegawai,
    restore_pegawai,
    toggle_pegawai_status,
    update_pegawai,
)
from services.activity_log import log_system_activity

LOGGER = logging.getLogger(__name__)


def render_pegawai_kpis(all_pegawai: list[dict[str, Any]]) -> None:
    """Tampilkan kartu metrik ringkasan pegawai."""
    total = len(all_pegawai)
    active = sum(1 for p in all_pegawai if p.get("aktif") and not p.get("deleted_at"))
    pns_count = sum(1 for p in all_pegawai if p.get("status_pegawai") == "PNS" and not p.get("deleted_at"))
    pppk_count = sum(1 for p in all_pegawai if p.get("status_pegawai") == "PPPK" and not p.get("deleted_at"))
    non_asn = sum(1 for p in all_pegawai if p.get("status_pegawai") not in {"PNS", "PPPK"} and not p.get("deleted_at"))
    
    male_count = sum(1 for p in all_pegawai if str(p.get("jenis_kelamin")).lower().startswith("l") and not p.get("deleted_at"))
    female_count = sum(1 for p in all_pegawai if str(p.get("jenis_kelamin")).lower().startswith("p") and not p.get("deleted_at"))

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Pegawai Terdaftar", f"{total} Orang")
    with col2:
        st.metric("Pegawai Aktif", f"{active} Orang")
    with col3:
        st.metric("Komposisi Status", f"PNS: {pns_count} | PPPK: {pppk_count} | Lain: {non_asn}")
    with col4:
        st.metric("Rasio Gender", f"👨 L: {male_count} | 👩 P: {female_count}")


def show_pegawai_management_page(engine: Engine) -> None:
    """Tampilan utama modul Master Data Pegawai."""
    st.markdown(
        """
        <div style="margin-bottom: 1.2rem;">
            <h1 style="font-size: 1.8rem; font-weight: 700; color: #1e293b; margin-bottom: 0.2rem;">
                👥 Master Data Pegawai
            </h1>
            <p style="color: #64748b; font-size: 0.95rem; margin-top: 0;">
                Kelola basis data pegawai, NIP, unit kerja OPD, jabatan, jenis kelamin, dan status kepegawaian berbasis database PostgreSQL serta fitur Import Excel.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    current_role = st.session_state.get("user_role", "admin").lower()
    current_username = st.session_state.get("username", "")

    try:
        all_pegawai_with_trash = list_pegawai(engine, include_deleted=True)
        active_pegawai = [p for p in all_pegawai_with_trash if not p.get("deleted_at")]
        opd_list = list_opds(engine, aktif_only=True)
    except Exception as exc:
        st.error(f"❌ Gagal memuat Master Pegawai dari database PostgreSQL: {exc}")
        return

    render_pegawai_kpis(all_pegawai_with_trash)
    st.markdown("---")

    tab_list, tab_import, tab_add, tab_edit, tab_trash = st.tabs([
        "📋 Direktori & Data Pegawai",
        "📥 Import Data Excel",
        "➕ Tambah Pegawai Baru",
        "✏️ Edit & Kelola Pegawai",
        "🗑️ Tempat Sampah / Pemulihan",
    ])

    # ========================================================================
    # TAB 1: DIREKTORI & DATA PEGAWAI
    # ========================================================================
    with tab_list:
        col_s, col_opd_f, col_st_f, col_jk_f = st.columns([2, 1.5, 1, 1])
        with col_s:
            search_query = st.text_input(
                "🔍 Cari Pegawai",
                placeholder="Ketik NIP, Nama, atau Jabatan...",
                key="pegawai_search_input",
            )
        with col_opd_f:
            opd_filter_options = ["Semua OPD"] + [o["nama"] for o in opd_list]
            filter_opd = st.selectbox("Filter OPD", opd_filter_options, key="pegawai_opd_filter")
        with col_st_f:
            filter_status_pegawai = st.selectbox(
                "Status Pegawai",
                ["Semua Status"] + STATUS_PEGAWAI_CHOICES,
                key="pegawai_status_filter",
            )
        with col_jk_f:
            filter_jk = st.selectbox(
                "Jenis Kelamin",
                ["Semua", "Laki-laki", "Perempuan"],
                key="pegawai_jk_filter",
            )

        filtered = active_pegawai
        if search_query:
            q = search_query.strip().lower()
            filtered = [
                p for p in filtered
                if q in (p.get("nip") or "").lower()
                or q in (p.get("nama_pegawai") or "").lower()
                or q in (p.get("jabatan") or "").lower()
            ]
        if filter_opd != "Semua OPD":
            filtered = [p for p in filtered if p.get("nama_opd") == filter_opd]
        if filter_status_pegawai != "Semua Status":
            filtered = [p for p in filtered if p.get("status_pegawai") == filter_status_pegawai]
        if filter_jk != "Semua":
            filtered = [p for p in filtered if p.get("jenis_kelamin") == filter_jk]

        if not filtered:
            st.info("Tidak ada data pegawai yang sesuai dengan kriteria pencarian/filter.")
        else:
            table_rows = []
            for p in filtered:
                table_rows.append({
                    "ID": p["id_pegawai"],
                    "NIP": p["nip"],
                    "Nama Pegawai": p["nama_pegawai"],
                    "OPD / Unit Kerja": p["nama_opd"],
                    "Jabatan": p["jabatan"],
                    "Jenis Kelamin": p["jenis_kelamin"],
                    "Status": p["status_pegawai"],
                    "Keaktifan": "🟢 Aktif" if p["aktif"] else "🔴 Nonaktif",
                })

            df_table = pd.DataFrame(table_rows)
            st.dataframe(
                df_table,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "ID": st.column_config.NumberColumn("ID", width="small"),
                    "NIP": st.column_config.TextColumn("NIP Pegawai", width="medium"),
                    "Nama Pegawai": st.column_config.TextColumn("Nama Lengkap", width="large"),
                    "OPD / Unit Kerja": st.column_config.TextColumn("Unit Kerja (OPD)", width="large"),
                    "Jabatan": st.column_config.TextColumn("Jabatan", width="medium"),
                    "Jenis Kelamin": st.column_config.TextColumn("Gender", width="small"),
                    "Status": st.column_config.TextColumn("Status Pegawai", width="small"),
                    "Keaktifan": st.column_config.TextColumn("Keaktifan", width="small"),
                },
            )

            # Export Excel button
            excel_bytes = io.BytesIO()
            with pd.ExcelWriter(excel_bytes, engine="openpyxl") as writer:
                df_table.to_excel(writer, index=False, sheet_name="Data Pegawai")
            st.download_button(
                label="📥 Unduh Data Tabel Pegawai (.xlsx)",
                data=excel_bytes.getvalue(),
                file_name="master_data_pegawai.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="download_filtered_pegawai",
            )

        # Quick Toggle Status
        if active_pegawai:
            with st.expander("⚡ Aksi Cepat: Aktifkan / Nonaktifkan Status Pegawai"):
                toggle_map = {f"{p['nip']} — {p['nama_pegawai']} ({'🟢 Aktif' if p['aktif'] else '🔴 Nonaktif'})": p for p in active_pegawai}
                sel_toggle = st.selectbox("Pilih Pegawai:", list(toggle_map.keys()), key="pegawai_quick_toggle_select")
                target_toggle = toggle_map[sel_toggle]

                col_btn1, col_btn2 = st.columns(2)
                with col_btn1:
                    if st.button("🟢 Aktifkan Pegawai", use_container_width=True, disabled=target_toggle["aktif"], key="btn_act_pegawai"):
                        ok, msg = toggle_pegawai_status(engine, target_toggle["id_pegawai"], True)
                        if ok:
                            log_system_activity(
                                "pegawai_management", "Aktivasi Pegawai",
                                f"User '{current_username}' mengaktifkan pegawai '{target_toggle['nama_pegawai']}' ({target_toggle['nip']}).",
                                metadata={"nip": target_toggle["nip"]},
                            )
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)
                with col_btn2:
                    if st.button("🔴 Nonaktifkan Pegawai", use_container_width=True, disabled=not target_toggle["aktif"], key="btn_deact_pegawai"):
                        ok, msg = toggle_pegawai_status(engine, target_toggle["id_pegawai"], False)
                        if ok:
                            log_system_activity(
                                "pegawai_management", "Deaktivasi Pegawai",
                                f"User '{current_username}' menonaktifkan pegawai '{target_toggle['nama_pegawai']}' ({target_toggle['nip']}).",
                                metadata={"nip": target_toggle["nip"]},
                            )
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)

    # ========================================================================
    # TAB 2: IMPORT DATA DARI EXCEL
    # ========================================================================
    with tab_import:
        st.markdown("#### 📥 Import Massal Data Pegawai dari File Excel")
        st.markdown(
            "Anda dapat mengunggah file spreadsheet (`.xlsx`, `.xls`, `.csv`) untuk mendaftarkan atau memperbarui data master pegawai secara massal ke database PostgreSQL."
        )

        col_tmpl1, col_tmpl2 = st.columns([1, 2])
        with col_tmpl1:
            template_data = generate_pegawai_excel_template()
            st.download_button(
                label="📄 Unduh Template Excel Contoh",
                data=template_data,
                file_name="template_import_pegawai.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                help="Gunakan template ini untuk mengisi data pegawai sesuai format standar.",
                use_container_width=True,
            )
        with col_tmpl2:
            st.caption("ℹ️ Sistem menggunakan metode **UPSERT** berdasarkan NIP. Jika NIP sudah ada, data nama, OPD, jabatan, dan status akan diperbarui otomatis.")

        st.markdown("---")
        uploaded_file = st.file_uploader(
            "Pilih File Excel Pegawai (.xlsx, .xls, .csv)",
            type=["xlsx", "xls", "csv"],
            key="excel_pegawai_uploader",
        )

        if uploaded_file is not None:
            try:
                if uploaded_file.name.endswith(".csv"):
                    preview_df = pd.read_csv(uploaded_file, dtype=str)
                else:
                    preview_df = pd.read_excel(uploaded_file, dtype=str)

                st.markdown(f"**Pratinjau File: `{uploaded_file.name}` ({len(preview_df)} baris data)**")
                st.dataframe(preview_df.head(10), use_container_width=True)

                if st.button("🚀 Proses & Simpan ke PostgreSQL", type="primary", use_container_width=True, key="btn_execute_import"):
                    with st.spinner("Sedang memproses import data ke PostgreSQL..."):
                        uploaded_file.seek(0)
                        result = import_pegawai_from_excel(engine, uploaded_file)
                        
                        log_system_activity(
                            "pegawai_management", "Import Pegawai Excel",
                            f"User '{current_username}' mengimport {result['total_rows']} baris (Insert: {result['inserted']}, Update: {result['updated']}, Gagal: {result['failed']}).",
                            metadata={"file_name": uploaded_file.name, "result": result},
                        )

                        st.success(
                            f"🎉 Import Selesai!\n\n"
                            f"- **Total Baris**: {result['total_rows']}\n"
                            f"- **Pegawai Baru (Insert)**: {result['inserted']}\n"
                            f"- **Pegawai Diperbarui (Update)**: {result['updated']}\n"
                            f"- **Gagal / Dilewati**: {result['failed']}"
                        )
                        if result["errors"]:
                            with st.expander("⚠️ Catatan / Error Baris Tertentu:"):
                                for err in result["errors"]:
                                    st.write(f"- {err}")
                        st.rerun()

            except Exception as e_import:
                st.error(f"Gagal membaca file: {e_import}")

    # ========================================================================
    # TAB 3: TAMBAH PEGAWAI BARU
    # ========================================================================
    with tab_add:
        st.markdown("#### Form Pendaftaran Pegawai Baru")
        st.caption("Data pegawai akan disimpan ke database PostgreSQL pada tabel `pegawai`.")

        with st.form("create_pegawai_form", clear_on_submit=True):
            col_p1, col_p2 = st.columns(2)
            with col_p1:
                new_nip = st.text_input("NIP Pegawai *", placeholder="contoh: 198501152010011012")
                new_nama = st.text_input("Nama Lengkap Pegawai *", placeholder="contoh: Dr. H. Ahmad Fauzi, M.Si")
                new_jk = st.selectbox("Jenis Kelamin", JENIS_KELAMIN_CHOICES)
            with col_p2:
                # Dropdown OPD
                opd_options = {"- Pilih OPD / Unit Kerja -": None}
                for o in opd_list:
                    opd_options[f"[{o['kode'] or '-'}] {o['nama']}"] = o["id"]

                sel_opd_label = st.selectbox("Unit Kerja (OPD)", list(opd_options.keys()))
                new_id_opd = opd_options[sel_opd_label]

                new_jabatan = st.text_input("Jabatan", placeholder="contoh: Analis Kepegawaian Ahli Muda")
                new_status = st.selectbox("Status Pegawai", STATUS_PEGAWAI_CHOICES)

            new_aktif = st.checkbox("Status Langsung Aktif", value=True)
            submit_create = st.form_submit_button("💾 Simpan Data Pegawai", use_container_width=True)

        if submit_create:
            if not new_nip.strip():
                st.error("NIP Pegawai wajib diisi.")
            elif not new_nama.strip():
                st.error("Nama Lengkap Pegawai wajib diisi.")
            else:
                try:
                    payload = {
                        "nip": new_nip.strip(),
                        "nama_pegawai": new_nama.strip(),
                        "id_opd": new_id_opd,
                        "jabatan": new_jabatan.strip(),
                        "jenis_kelamin": new_jk,
                        "status_pegawai": new_status,
                        "aktif": new_aktif,
                    }
                    created = create_pegawai(engine, payload)
                    log_system_activity(
                        "pegawai_management", "Tambah Pegawai Baru",
                        f"User '{current_username}' menambahkan pegawai baru '{new_nama.strip()}' (NIP: {new_nip.strip()}).",
                        metadata={"nip": new_nip.strip(), "nama": new_nama.strip()},
                    )
                    st.success(f"✓ Berhasil menambahkan pegawai '{new_nama.strip()}' ke PostgreSQL!")
                    st.rerun()
                except ValueError as v_err:
                    st.error(f"Gagal validasi: {v_err}")
                except Exception as err:
                    st.error(f"Terjadi kesalahan saat menyimpan ke database: {err}")

    # ========================================================================
    # TAB 4: EDIT & KELOLA PEGAWAI
    # ========================================================================
    with tab_edit:
        if not active_pegawai:
            st.info("Belum ada data pegawai yang dapat diedit.")
        else:
            st.markdown("#### Edit Informasi Pegawai")
            
            edit_pegawai_map = {f"[{p['nip']}] {p['nama_pegawai']} — {p['nama_opd']}": p for p in active_pegawai}
            selected_edit_key = st.selectbox("Pilih Pegawai yang Ingin Diperbarui", list(edit_pegawai_map.keys()), key="edit_pegawai_select")
            target_edit = edit_pegawai_map[selected_edit_key]

            with st.form("edit_pegawai_form"):
                col_ep1, col_ep2 = st.columns(2)
                with col_ep1:
                    e_nip = st.text_input("NIP Pegawai *", value=target_edit["nip"])
                    e_nama = st.text_input("Nama Lengkap Pegawai *", value=target_edit["nama_pegawai"])
                    jk_idx = JENIS_KELAMIN_CHOICES.index(target_edit["jenis_kelamin"]) if target_edit["jenis_kelamin"] in JENIS_KELAMIN_CHOICES else 0
                    e_jk = st.selectbox("Jenis Kelamin", JENIS_KELAMIN_CHOICES, index=jk_idx)
                with col_ep2:
                    # Dropdown OPD with selection
                    opd_edit_opts = {"- Tidak Ada / Belum Ditentukan -": None}
                    opd_labels = ["- Tidak Ada / Belum Ditentukan -"]
                    cur_opd_idx = 0
                    for o in opd_list:
                        lbl = f"[{o['kode'] or '-'}] {o['nama']}"
                        opd_edit_opts[lbl] = o["id"]
                        opd_labels.append(lbl)
                        if target_edit["id_opd"] == o["id"]:
                            cur_opd_idx = len(opd_labels) - 1

                    e_opd_label = st.selectbox("Unit Kerja (OPD)", opd_labels, index=cur_opd_idx)
                    e_id_opd = opd_edit_opts[e_opd_label]

                    e_jabatan = st.text_input("Jabatan", value=target_edit["jabatan"] if target_edit["jabatan"] != "-" else "")
                    st_idx = STATUS_PEGAWAI_CHOICES.index(target_edit["status_pegawai"]) if target_edit["status_pegawai"] in STATUS_PEGAWAI_CHOICES else 0
                    e_status = st.selectbox("Status Pegawai", STATUS_PEGAWAI_CHOICES, index=st_idx)

                e_aktif = st.checkbox("Status Aktif", value=bool(target_edit["aktif"]))
                submit_edit_pegawai = st.form_submit_button("💾 Perbarui Data Pegawai", use_container_width=True)

            if submit_edit_pegawai:
                if not e_nip.strip():
                    st.error("NIP Pegawai wajib diisi.")
                elif not e_nama.strip():
                    st.error("Nama Pegawai wajib diisi.")
                else:
                    payload_update = {
                        "nip": e_nip.strip(),
                        "nama_pegawai": e_nama.strip(),
                        "id_opd": e_id_opd,
                        "jabatan": e_jabatan.strip(),
                        "jenis_kelamin": e_jk,
                        "status_pegawai": e_status,
                        "aktif": e_aktif,
                    }
                    ok_up, msg_up = update_pegawai(engine, target_edit["id_pegawai"], payload_update)
                    if ok_up:
                        log_system_activity(
                            "pegawai_management", "Update Data Pegawai",
                            f"User '{current_username}' memperbarui data pegawai '{e_nama.strip()}' ({e_nip.strip()}).",
                            metadata={"id_pegawai": target_edit["id_pegawai"], "nip": e_nip.strip()},
                        )
                        st.success(f"✓ {msg_up}")
                        st.rerun()
                    else:
                        st.error(msg_up)

            st.markdown("---")
            st.markdown("#### 🗑️ Hapus Pegawai (Pindahkan ke Tempat Sampah)")
            st.caption("Data pegawai akan dinonaktifkan dan dipindahkan ke tempat sampah (soft delete), dan dapat dipulihkan sewaktu-waktu.")

            with st.expander(f"⚠️ Konfirmasi Hapus Pegawai: {target_edit['nama_pegawai']}"):
                st.write(f"Apakah Anda yakin ingin memindahkan pegawai **{target_edit['nama_pegawai']}** (NIP: `{target_edit['nip']}`) ke tempat sampah?")
                if st.button("🚨 Ya, Pindahkan ke Tempat Sampah", type="primary", key="btn_soft_delete_pegawai"):
                    ok_del, msg_del = delete_pegawai(engine, target_edit["id_pegawai"], hard_delete=False)
                    if ok_del:
                        log_system_activity(
                            "pegawai_management", "Soft Delete Pegawai",
                            f"User '{current_username}' memindahkan pegawai '{target_edit['nama_pegawai']}' ke tempat sampah.",
                            metadata={"id_pegawai": target_edit["id_pegawai"], "nip": target_edit["nip"]},
                        )
                        st.success(msg_del)
                        st.rerun()
                    else:
                        st.error(msg_del)

    # ========================================================================
    # TAB 5: TEMPAT SAMPAH & PEMULIHAN
    # ========================================================================
    with tab_trash:
        deleted_list = [p for p in all_pegawai_with_trash if p.get("deleted_at")]
        if not deleted_list:
            st.info("Tempat sampah kosong. Tidak ada data pegawai yang dihapus.")
        else:
            st.markdown("#### Daftar Pegawai yang Terhapus (Soft Delete)")
            st.caption("Data di bawah ini dapat dipulihkan kembali ke daftar aktif atau dihapus secara permanen.")

            trash_rows = []
            for p in deleted_list:
                del_time = p["deleted_at"].strftime("%d-%m-%Y %H:%M") if hasattr(p["deleted_at"], "strftime") else str(p["deleted_at"])
                trash_rows.append({
                    "ID": p["id_pegawai"],
                    "NIP": p["nip"],
                    "Nama Pegawai": p["nama_pegawai"],
                    "OPD": p["nama_opd"],
                    "Status": p["status_pegawai"],
                    "Waktu Dihapus": del_time,
                })

            st.dataframe(pd.DataFrame(trash_rows), hide_index=True, use_container_width=True)

            trash_dict = {f"[{p['nip']}] {p['nama_pegawai']}": p for p in deleted_list}
            selected_trash = st.selectbox("Pilih Pegawai:", list(trash_dict.keys()), key="trash_pegawai_select")
            target_trash = trash_dict[selected_trash]

            col_t1, col_t2 = st.columns(2)
            with col_t1:
                if st.button("♻️ Pulihkan Pegawai (Restore)", use_container_width=True, key="btn_restore_pegawai"):
                    ok_rest, msg_rest = restore_pegawai(engine, target_trash["id_pegawai"])
                    if ok_rest:
                        log_system_activity(
                            "pegawai_management", "Restore Pegawai",
                            f"User '{current_username}' memulihkan pegawai '{target_trash['nama_pegawai']}'.",
                            metadata={"id_pegawai": target_trash["id_pegawai"], "nip": target_trash["nip"]},
                        )
                        st.success(msg_rest)
                        st.rerun()
                    else:
                        st.error(msg_rest)
            with col_t2:
                if st.button("🚨 Hapus Permanen dari Database", type="primary", use_container_width=True, key="btn_hard_delete_pegawai"):
                    ok_hard, msg_hard = delete_pegawai(engine, target_trash["id_pegawai"], hard_delete=True)
                    if ok_hard:
                        log_system_activity(
                            "pegawai_management", "Hard Delete Pegawai",
                            f"User '{current_username}' menghapus permanen pegawai '{target_trash['nama_pegawai']}' dari PostgreSQL.",
                            metadata={"id_pegawai": target_trash["id_pegawai"], "nip": target_trash["nip"]},
                        )
                        st.success(msg_hard)
                        st.rerun()
                    else:
                        st.error(msg_hard)
