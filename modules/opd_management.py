"""Modul antarmuka dan pengelolaan Master Organisasi Perangkat Daerah (OPD / Dinas) berbasis PostgreSQL."""

from __future__ import annotations

import logging
from html import escape
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy.engine import Engine

from database.opd import (
    OPD_JENIS_CHOICES,
    create_opd,
    delete_opd,
    get_opd_by_id,
    list_opds,
    restore_opd,
    toggle_opd_status,
    update_opd,
)
from services.activity_log import log_system_activity
from services.rbac import EDIT_MASTER, can_access_page, has_permission

LOGGER = logging.getLogger(__name__)

JENIS_ICONS = {
    "DINAS": "🏛️",
    "BADAN": "🏢",
    "SEKRETARIAT": "🏛️",
    "INSPEKTORAT": "⚖️",
    "KECAMATAN": "🏘️",
    "RSUD": "🏥",
    "BAGIAN": "📁",
    "UPTD": "📍",
    "LAINNYA": "📌",
}


def render_opd_kpis(all_opds: list[dict[str, Any]]) -> None:
    """Tampilkan kartu metrik ringkasan OPD."""
    total_opds = len(all_opds)
    active_opds = sum(1 for o in all_opds if o.get("aktif") and not o.get("deleted_at"))
    inactive_opds = sum(1 for o in all_opds if not o.get("aktif") and not o.get("deleted_at"))
    deleted_opds = sum(1 for o in all_opds if o.get("deleted_at"))

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total OPD Terdaftar", f"{total_opds} Unit")
    with col2:
        st.metric("OPD Aktif", f"{active_opds} Unit")
    with col3:
        st.metric("OPD Nonaktif", f"{inactive_opds} Unit")
    with col4:
        st.metric("Di Tempat Sampah", f"{deleted_opds} Unit")


def show_opd_management_page(engine: Engine) -> None:
    """Tampilan utama modul Manajemen Master OPD / Dinas."""
    if not can_access_page(st.session_state.get("user_role"), "Master OPD"):
        st.error("Akses tidak tersedia untuk peran pengguna Anda.")
        return
    st.markdown(
        """
        <div style="margin-bottom: 1.2rem;">
            <h1 style="font-size: 1.8rem; font-weight: 700; color: #1e293b; margin-bottom: 0.2rem;">
                🏢 Master OPD & Dinas (Organisasi Perangkat Daerah)
            </h1>
            <p style="color: #64748b; font-size: 0.95rem; margin-top: 0;">
                Kelola data induk dinas, badan, unit kerja, struktur hierarki, dan kontak pimpinan yang tersimpan di database PostgreSQL.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    current_role = st.session_state.get("user_role")
    current_username = st.session_state.get("username", "")

    try:
        all_opds_with_trash = list_opds(engine, include_deleted=True)
        active_opds = [o for o in all_opds_with_trash if not o.get("deleted_at")]
    except Exception as exc:
        st.error(f"❌ Gagal memuat data Master OPD dari database PostgreSQL: {exc}")
        return

    render_opd_kpis(all_opds_with_trash)
    st.markdown("---")

    if not has_permission(current_role, EDIT_MASTER):
        rows = [{
            "Kode": opd.get("kode") or "-",
            "Nama OPD": opd.get("nama") or "-",
            "Singkatan": opd.get("singkatan") or "-",
            "Jenis": opd.get("jenis") or "-",
            "Kepala OPD": opd.get("kepala_nama") or "-",
            "Status": "Aktif" if opd.get("aktif") else "Nonaktif",
        } for opd in active_opds]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        return

    tab_list, tab_add, tab_edit, tab_trash = st.tabs([
        "🏢 Direktori & Daftar OPD",
        "➕ Tambah OPD Baru",
        "✏️ Edit & Kelola OPD",
        "🗑️ Tempat Sampah / Pemulihan",
    ])

    # ========================================================================
    # TAB 1: DIREKTORI & DAFTAR OPD
    # ========================================================================
    with tab_list:
        col_search, col_jenis_filter, col_status_filter = st.columns([2, 1, 1])
        with col_search:
            search_query = st.text_input(
                "🔍 Cari OPD",
                placeholder="Cari berdasarkan nama, kode, singkatan, atau kepala dinas...",
                key="opd_search_input",
            )
        with col_jenis_filter:
            filter_jenis = st.selectbox(
                "Filter Kategori / Jenis",
                ["Semua Kategori"] + OPD_JENIS_CHOICES,
                key="opd_jenis_filter",
            )
        with col_status_filter:
            filter_status = st.selectbox(
                "Filter Status",
                ["Semua Status", "Aktif", "Nonaktif"],
                key="opd_status_filter",
            )

        filtered = active_opds
        if search_query:
            q = search_query.strip().lower()
            filtered = [
                o for o in filtered
                if q in (o.get("nama") or "").lower()
                or q in (o.get("kode") or "").lower()
                or q in (o.get("kode_sipd") or "").lower()
                or q in (o.get("singkatan") or "").lower()
                or q in (o.get("kepala_nama") or "").lower()
            ]
        if filter_jenis != "Semua Kategori":
            filtered = [o for o in filtered if o.get("jenis") == filter_jenis]
        if filter_status == "Aktif":
            filtered = [o for o in filtered if o.get("aktif")]
        elif filter_status == "Nonaktif":
            filtered = [o for o in filtered if not o.get("aktif")]

        if not filtered:
            st.info("Tidak ada OPD yang sesuai dengan kriteria pencarian/filter.")
        else:
            table_rows = []
            for o in filtered:
                icon = JENIS_ICONS.get(o["jenis"], "📌")
                table_rows.append({
                    "ID": o["id"],
                    "Kode": o["kode"] or "-",
                    "Kode SIPD": o["kode_sipd"] or "-",
                    "Nama OPD": o["nama"],
                    "Singkatan": o["singkatan"] or "-",
                    "Jenis": f"{icon} {o['jenis']}",
                    "Induk (Parent)": o["parent_nama"],
                    "Kepala OPD": o["kepala_nama"] or "-",
                    "NIP Kepala": o["kepala_nip"] or "-",
                    "Telepon": o["telepon"] or "-",
                    "Email": o["email"] or "-",
                    "Status": "🟢 Aktif" if o["aktif"] else "🔴 Nonaktif",
                })

            df_table = pd.DataFrame(table_rows)
            st.dataframe(
                df_table,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "ID": st.column_config.NumberColumn("ID", width="small"),
                    "Kode": st.column_config.TextColumn("Kode OPD", width="small"),
                    "Kode SIPD": st.column_config.TextColumn("Kode SIPD", width="small"),
                    "Nama OPD": st.column_config.TextColumn("Nama Lengkap OPD", width="large"),
                    "Singkatan": st.column_config.TextColumn("Singkatan", width="small"),
                    "Jenis": st.column_config.TextColumn("Kategori", width="medium"),
                    "Induk (Parent)": st.column_config.TextColumn("Unit Induk", width="medium"),
                    "Kepala OPD": st.column_config.TextColumn("Kepala OPD", width="medium"),
                    "NIP Kepala": st.column_config.TextColumn("NIP Kepala", width="medium"),
                    "Telepon": st.column_config.TextColumn("Telepon", width="small"),
                    "Email": st.column_config.TextColumn("Email", width="medium"),
                    "Status": st.column_config.TextColumn("Status", width="small"),
                },
            )

        # Detail Viewer
        if active_opds:
            with st.expander("🔍 Lihat Profil Lengkap & Kontak OPD"):
                opd_dict = {f"[{o['kode'] or '-'}] {o['nama']} ({o['singkatan'] or o['jenis']})": o for o in active_opds}
                selected_view_label = st.selectbox("Pilih OPD untuk ditinjau:", list(opd_dict.keys()), key="opd_detail_select")
                target = opd_dict[selected_view_label]

                col_d1, col_d2 = st.columns(2)
                with col_d1:
                    st.markdown(f"**Nama Lengkap:** {escape(target['nama'])}")
                    st.markdown(f"**Kode OPD:** `{target['kode'] or '-'}` | **Kode SIPD:** `{target['kode_sipd'] or '-'}`")
                    st.markdown(f"**Kategori / Jenis:** {JENIS_ICONS.get(target['jenis'], '📌')} {target['jenis']}")
                    st.markdown(f"**Unit Induk (Parent):** {escape(target['parent_nama'])}")
                    st.markdown(f"**Alamat Kantor:** {escape(target['alamat']) if target['alamat'] else '-'}")
                with col_d2:
                    st.markdown(f"**Kepala OPD:** {escape(target['kepala_nama']) if target['kepala_nama'] else '-'}")
                    st.markdown(f"**NIP Kepala:** `{target['kepala_nip'] or '-'}`")
                    st.markdown(f"**Telepon:** {escape(target['telepon']) if target['telepon'] else '-'}")
                    st.markdown(f"**Email:** {escape(target['email']) if target['email'] else '-'}")
                    st.markdown(f"**Website:** {escape(target['website']) if target['website'] else '-'}")

        # Quick Toggle Status
        if active_opds:
            with st.expander("⚡ Aksi Cepat: Aktifkan / Nonaktifkan Status OPD"):
                toggle_map = {f"[{o['id']}] {o['nama']} — {'🟢 Aktif' if o['aktif'] else '🔴 Nonaktif'}": o for o in active_opds}
                sel_toggle = st.selectbox("Pilih OPD:", list(toggle_map.keys()), key="opd_quick_toggle_select")
                target_toggle = toggle_map[sel_toggle]

                c_btn1, c_btn2 = st.columns(2)
                with c_btn1:
                    if st.button("🟢 Aktifkan OPD", use_container_width=True, disabled=target_toggle["aktif"], key="btn_activate_opd"):
                        ok, msg = toggle_opd_status(engine, target_toggle["id"], True)
                        if ok:
                            log_system_activity(
                                "opd_management", "Aktivasi OPD",
                                f"User '{current_username}' mengaktifkan OPD '{target_toggle['nama']}'.",
                                metadata={"opd_id": target_toggle["id"], "opd_nama": target_toggle["nama"]},
                            )
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)
                with c_btn2:
                    if st.button("🔴 Nonaktifkan OPD", use_container_width=True, disabled=not target_toggle["aktif"], key="btn_deactivate_opd"):
                        ok, msg = toggle_opd_status(engine, target_toggle["id"], False)
                        if ok:
                            log_system_activity(
                                "opd_management", "Deaktivasi OPD",
                                f"User '{current_username}' menonaktifkan status OPD '{target_toggle['nama']}'.",
                                metadata={"opd_id": target_toggle["id"], "opd_nama": target_toggle["nama"]},
                            )
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)

    # ========================================================================
    # TAB 2: TAMBAH OPD BARU
    # ========================================================================
    with tab_add:
        st.markdown("#### Form Pendaftaran OPD / Dinas Baru")
        st.caption("Data OPD baru akan langsung disimpan ke database PostgreSQL pada tabel `opd`.")

        with st.form("create_opd_form", clear_on_submit=True):
            col_f1, col_f2 = st.columns(2)
            with col_f1:
                new_kode = st.text_input("Kode OPD", placeholder="contoh: 1.07.01")
                new_nama = st.text_input("Nama Lengkap OPD / Dinas *", placeholder="contoh: Dinas Lingkungan Hidup")
                new_jenis = st.selectbox("Kategori / Jenis OPD *", OPD_JENIS_CHOICES)
                new_kepala_nama = st.text_input("Nama Kepala OPD", placeholder="contoh: Drs. Budi Santoso, M.Si")
                new_telepon = st.text_input("Nomor Telepon", placeholder="contoh: (021) 555-0199")
                new_website = st.text_input("Website", placeholder="contoh: https://dlh.pemda.go.id")
            with col_f2:
                new_kode_sipd = st.text_input("Kode SIPD", placeholder="contoh: 1.07.01.01")
                new_singkatan = st.text_input("Singkatan OPD", placeholder="contoh: DLH")

                # Dropdown parent OPD
                parent_options = {"- Tidak Ada (OPD Utama) -": None}
                for o in active_opds:
                    parent_options[f"[{o['kode'] or '-'}] {o['nama']}"] = o["id"]

                new_parent_label = st.selectbox("OPD Induk (Parent)", list(parent_options.keys()))
                new_parent_id = parent_options[new_parent_label]

                new_kepala_nip = st.text_input("NIP Kepala OPD", placeholder="contoh: 198001012005011002")
                new_email = st.text_input("Email Resmi OPD", placeholder="contoh: dlh@pemda.go.id")
                new_aktif = st.checkbox("Status Langsung Aktif", value=True)

            new_alamat = st.text_area("Alamat Lengkap Kantor", placeholder="contoh: Jl. Pemuda No. 12, Kompleks Perkantoran")
            submit_create_opd = st.form_submit_button("💾 Simpan Data OPD", use_container_width=True)

        if submit_create_opd:
            if not new_nama.strip():
                st.error("Nama Lengkap OPD wajib diisi.")
            else:
                try:
                    payload = {
                        "kode": new_kode.strip(),
                        "kode_sipd": new_kode_sipd.strip(),
                        "nama": new_nama.strip(),
                        "singkatan": new_singkatan.strip(),
                        "jenis": new_jenis,
                        "parent_id": new_parent_id,
                        "alamat": new_alamat.strip(),
                        "telepon": new_telepon.strip(),
                        "email": new_email.strip(),
                        "website": new_website.strip(),
                        "kepala_nama": new_kepala_nama.strip(),
                        "kepala_nip": new_kepala_nip.strip(),
                        "aktif": new_aktif,
                    }
                    created = create_opd(engine, payload)
                    log_system_activity(
                        "opd_management", "Tambah OPD Baru",
                        f"User '{current_username}' mendaftarkan OPD baru '{new_nama.strip()}'.",
                        metadata={"opd_nama": new_nama.strip(), "kode": new_kode.strip()},
                    )
                    st.success(f"✓ Berhasil menambahkan OPD '{new_nama.strip()}' ke PostgreSQL!")
                    st.rerun()
                except ValueError as v_err:
                    st.error(f"Gagal validasi: {v_err}")
                except Exception as err:
                    st.error(f"Terjadi kesalahan saat menyimpan ke database: {err}")

    # ========================================================================
    # TAB 3: EDIT & KELOLA OPD
    # ========================================================================
    with tab_edit:
        if not active_opds:
            st.info("Belum ada data OPD yang dapat diedit.")
        else:
            st.markdown("#### Edit Informasi OPD")
            
            edit_opd_map = {f"[{o['id']}] {o['kode'] or '-'} - {o['nama']}": o for o in active_opds}
            selected_edit_key = st.selectbox("Pilih OPD yang Ingin Diperbarui", list(edit_opd_map.keys()), key="edit_opd_select")
            target_edit = edit_opd_map[selected_edit_key]

            with st.form("edit_opd_form"):
                col_e1, col_e2 = st.columns(2)
                with col_e1:
                    e_kode = st.text_input("Kode OPD", value=target_edit.get("kode") or "")
                    e_nama = st.text_input("Nama Lengkap OPD *", value=target_edit.get("nama") or "")
                    jenis_idx = OPD_JENIS_CHOICES.index(target_edit["jenis"]) if target_edit["jenis"] in OPD_JENIS_CHOICES else 0
                    e_jenis = st.selectbox("Kategori / Jenis OPD", OPD_JENIS_CHOICES, index=jenis_idx)
                    e_kepala_nama = st.text_input("Nama Kepala OPD", value=target_edit.get("kepala_nama") or "")
                    e_telepon = st.text_input("Nomor Telepon", value=target_edit.get("telepon") or "")
                    e_website = st.text_input("Website", value=target_edit.get("website") or "")
                with col_e2:
                    e_kode_sipd = st.text_input("Kode SIPD", value=target_edit.get("kode_sipd") or "")
                    e_singkatan = st.text_input("Singkatan OPD", value=target_edit.get("singkatan") or "")

                    # Edit parent dropdown
                    edit_parents = {"- Tidak Ada (OPD Utama) -": None}
                    parent_keys = ["- Tidak Ada (OPD Utama) -"]
                    selected_p_idx = 0
                    for p_opd in active_opds:
                        if p_opd["id"] != target_edit["id"]:
                            label = f"[{p_opd['kode'] or '-'}] {p_opd['nama']}"
                            edit_parents[label] = p_opd["id"]
                            parent_keys.append(label)
                            if target_edit["parent_id"] == p_opd["id"]:
                                selected_p_idx = len(parent_keys) - 1

                    e_parent_label = st.selectbox("OPD Induk (Parent)", parent_keys, index=selected_p_idx)
                    e_parent_id = edit_parents[e_parent_label]

                    e_kepala_nip = st.text_input("NIP Kepala OPD", value=target_edit.get("kepala_nip") or "")
                    e_email = st.text_input("Email Resmi OPD", value=target_edit.get("email") or "")
                    e_aktif = st.checkbox("Status Aktif", value=bool(target_edit.get("aktif")))

                e_alamat = st.text_area("Alamat Kantor", value=target_edit.get("alamat") or "")
                submit_edit_opd = st.form_submit_button("💾 Perbarui Data OPD", use_container_width=True)

            if submit_edit_opd:
                if not e_nama.strip():
                    st.error("Nama OPD wajib diisi.")
                else:
                    payload_update = {
                        "kode": e_kode.strip(),
                        "kode_sipd": e_kode_sipd.strip(),
                        "nama": e_nama.strip(),
                        "singkatan": e_singkatan.strip(),
                        "jenis": e_jenis,
                        "parent_id": e_parent_id,
                        "alamat": e_alamat.strip(),
                        "telepon": e_telepon.strip(),
                        "email": e_email.strip(),
                        "website": e_website.strip(),
                        "kepala_nama": e_kepala_nama.strip(),
                        "kepala_nip": e_kepala_nip.strip(),
                        "aktif": e_aktif,
                    }
                    ok_up, msg_up = update_opd(engine, target_edit["id"], payload_update)
                    if ok_up:
                        log_system_activity(
                            "opd_management", "Update Data OPD",
                            f"User '{current_username}' memperbarui data OPD '{e_nama.strip()}'.",
                            metadata={"opd_id": target_edit["id"], "opd_nama": e_nama.strip()},
                        )
                        st.success(f"✓ {msg_up}")
                        st.rerun()
                    else:
                        st.error(msg_up)

            st.markdown("---")
            st.markdown("#### 🗑️ Hapus OPD (Pindahkan ke Tempat Sampah)")
            st.caption("Data OPD akan dinonaktifkan dan dipindahkan ke tempat sampah (soft delete), dan dapat dipulihkan sewaktu-waktu.")

            with st.expander(f"⚠️ Konfirmasi Hapus OPD: {target_edit['nama']}"):
                st.write(f"Apakah Anda yakin ingin memindahkan OPD **{target_edit['nama']}** ke tempat sampah?")
                if st.button("🚨 Ya, Pindahkan ke Tempat Sampah", type="primary", key="btn_soft_delete_opd"):
                    ok_del, msg_del = delete_opd(engine, target_edit["id"], hard_delete=False)
                    if ok_del:
                        log_system_activity(
                            "opd_management", "Soft Delete OPD",
                            f"User '{current_username}' memindahkan OPD '{target_edit['nama']}' ke tempat sampah.",
                            metadata={"opd_id": target_edit["id"], "opd_nama": target_edit["nama"]},
                        )
                        st.success(msg_del)
                        st.rerun()
                    else:
                        st.error(msg_del)

    # ========================================================================
    # TAB 4: TEMPAT SAMPAH & PEMULIHAN
    # ========================================================================
    with tab_trash:
        deleted_list = [o for o in all_opds_with_trash if o.get("deleted_at")]
        if not deleted_list:
            st.info("Tempat sampah kosong. Tidak ada data OPD yang dihapus.")
        else:
            st.markdown("#### Daftar OPD yang Terhapus (Soft Delete)")
            st.caption("Data di bawah ini dapat dipulihkan kembali ke daftar aktif atau dihapus secara permanen.")

            trash_rows = []
            for o in deleted_list:
                del_time = o["deleted_at"].strftime("%d-%m-%Y %H:%M") if hasattr(o["deleted_at"], "strftime") else str(o["deleted_at"])
                trash_rows.append({
                    "ID": o["id"],
                    "Kode": o["kode"] or "-",
                    "Nama OPD": o["nama"],
                    "Jenis": o["jenis"],
                    "Waktu Dihapus": del_time,
                })

            st.dataframe(pd.DataFrame(trash_rows), hide_index=True, use_container_width=True)

            trash_dict = {f"[{o['id']}] {o['nama']} (Kode: {o['kode'] or '-'})": o for o in deleted_list}
            selected_trash = st.selectbox("Pilih OPD:", list(trash_dict.keys()), key="trash_opd_select")
            target_trash = trash_dict[selected_trash]

            col_t1, col_t2 = st.columns(2)
            with col_t1:
                if st.button("♻️ Pulihkan OPD (Restore)", use_container_width=True, key="btn_restore_opd"):
                    ok_rest, msg_rest = restore_opd(engine, target_trash["id"])
                    if ok_rest:
                        log_system_activity(
                            "opd_management", "Restore OPD",
                            f"User '{current_username}' memulihkan OPD '{target_trash['nama']}'.",
                            metadata={"opd_id": target_trash["id"], "opd_nama": target_trash["nama"]},
                        )
                        st.success(msg_rest)
                        st.rerun()
                    else:
                        st.error(msg_rest)
            with col_t2:
                if st.button("🚨 Hapus Permanen dari Database", type="primary", use_container_width=True, key="btn_hard_delete_opd"):
                    ok_hard, msg_hard = delete_opd(engine, target_trash["id"], hard_delete=True)
                    if ok_hard:
                        log_system_activity(
                            "opd_management", "Hard Delete OPD",
                            f"User '{current_username}' menghapus permanen OPD '{target_trash['nama']}' dari PostgreSQL.",
                            metadata={"opd_id": target_trash["id"], "opd_nama": target_trash["nama"]},
                        )
                        st.success(msg_hard)
                        st.rerun()
                    else:
                        st.error(msg_hard)
