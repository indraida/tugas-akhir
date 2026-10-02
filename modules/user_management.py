"""Modul UI & logika Manajemen Pengguna berbasis PostgreSQL."""

from __future__ import annotations

import logging
from html import escape
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy.engine import Engine

from database.auth import (
    create_user,
    delete_user,
    get_user_by_id,
    list_users,
    toggle_user_status,
    update_user,
)
from services.activity_log import log_system_activity
from services.rbac import MANAGE_USERS, has_permission

LOGGER = logging.getLogger(__name__)

ROLE_LABELS = {
    "admin": "🛡️ Administrator",
    "operator": "📋 Operator",
    "pimpinan": "👔 Pimpinan",
}

ROLE_BADGE_COLORS = {
    "admin": "#ef4444",
    "operator": "#3b82f6",
    "pimpinan": "#10b981",
}


def render_user_kpis(users_list: list[dict[str, Any]]) -> None:
    """Tampilkan kartu ringkasan metrik pengguna."""
    total_users = len(users_list)
    active_users = sum(1 for u in users_list if u.get("is_active"))
    inactive_users = total_users - active_users
    
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Pengguna", f"{total_users} User")
    with col2:
        st.metric("Pengguna Aktif", f"{active_users} User")
    with col3:
        st.metric("Pengguna Nonaktif", f"{inactive_users} User")
    with col4:
        admin_count = sum(1 for u in users_list if u.get("role") == "admin")
        operator_count = sum(1 for u in users_list if u.get("role") == "operator")
        pimpinan_count = sum(1 for u in users_list if u.get("role") == "pimpinan")
        st.metric("Distribusi Role", f"A:{admin_count} | O:{operator_count} | P:{pimpinan_count}")


def show_user_management_page(engine: Engine) -> None:
    """Tampilan utama modul Manajemen Pengguna."""
    if not has_permission(st.session_state.get("user_role"), MANAGE_USERS):
        st.error("Akses tidak tersedia untuk peran pengguna Anda.")
        return
    st.markdown(
        """
        <div style="margin-bottom: 1.2rem;">
            <h1 style="font-size: 1.8rem; font-weight: 700; color: #1e293b; margin-bottom: 0.2rem;">
                👥 Manajemen Pengguna Sistem
            </h1>
            <p style="color: #64748b; font-size: 0.95rem; margin-top: 0;">
                Kelola akun pengguna, hak akses peran (role), dan kredensial yang tersimpan di database PostgreSQL.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    current_role = st.session_state.get("user_role")
    current_user_id = st.session_state.get("user_id")
    current_username = st.session_state.get("username", "")

    try:
        users_list = list_users(engine)
    except Exception as exc:
        st.error(f"❌ Gagal memuat data pengguna dari database PostgreSQL: {exc}")
        return

    render_user_kpis(users_list)
    st.markdown("---")

    is_admin = has_permission(current_role, MANAGE_USERS)
    if not is_admin:
        st.warning(
            "⚠️ Anda masuk dengan peran non-admin. Anda hanya dapat melihat daftar akun tanpa izin mengubah data pengguna."
        )

    tab_list, tab_add, tab_edit = st.tabs([
        "📋 Daftar Pengguna",
        "➕ Tambah Pengguna Baru",
        "✏️ Edit & Reset Password",
    ])

    # ========================================================================
    # TAB 1: DAFTAR PENGGUNA
    # ========================================================================
    with tab_list:
        col_search, col_role_filter, col_status_filter = st.columns([2, 1, 1])
        with col_search:
            search_query = st.text_input("🔍 Cari Pengguna", placeholder="Ketik username atau nama...")
        with col_role_filter:
            filter_role = st.selectbox("Filter Role", ["Semua Role", "admin", "operator", "pimpinan"])
        with col_status_filter:
            filter_status = st.selectbox("Filter Status", ["Semua Status", "Aktif", "Nonaktif"])

        filtered_users = users_list
        if search_query:
            q = search_query.strip().lower()
            filtered_users = [
                u for u in filtered_users
                if q in u["username"].lower() or q in (u.get("nama_lengkap") or "").lower()
            ]
        if filter_role != "Semua Role":
            filtered_users = [u for u in filtered_users if u["role"] == filter_role]
        if filter_status == "Aktif":
            filtered_users = [u for u in filtered_users if u["is_active"]]
        elif filter_status == "Nonaktif":
            filtered_users = [u for u in filtered_users if not u["is_active"]]

        if not filtered_users:
            st.info("Tidak ada pengguna yang cocok dengan kriteria pencarian/filter.")
        else:
            table_data = []
            for u in filtered_users:
                last_login = u.get("last_login_at")
                last_login_str = last_login.strftime("%d-%m-%Y %H:%M") if hasattr(last_login, "strftime") else (str(last_login) if last_login else "Belum pernah")
                created_str = u.get("created_at").strftime("%d-%m-%Y") if hasattr(u.get("created_at"), "strftime") else "-"
                
                table_data.append({
                    "ID": u["id"],
                    "Username": u["username"],
                    "Nama Lengkap": u["nama_lengkap"] or "-",
                    "Role": ROLE_LABELS.get(u["role"], u["role"].capitalize()),
                    "Status": "🟢 Aktif" if u["is_active"] else "🔴 Nonaktif",
                    "Terakhir Login": last_login_str,
                    "Tanggal Dibuat": created_str,
                })

            df_display = pd.DataFrame(table_data)
            st.dataframe(
                df_display,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "ID": st.column_config.NumberColumn("ID", width="small"),
                    "Username": st.column_config.TextColumn("Username", width="medium"),
                    "Nama Lengkap": st.column_config.TextColumn("Nama Lengkap", width="large"),
                    "Role": st.column_config.TextColumn("Hak Akses (Role)", width="medium"),
                    "Status": st.column_config.TextColumn("Status Akun", width="small"),
                    "Terakhir Login": st.column_config.TextColumn("Terakhir Login", width="medium"),
                    "Tanggal Dibuat": st.column_config.TextColumn("Dibuat Pada", width="small"),
                },
            )

        # Quick Status Toggle Section (Admin only)
        if is_admin and users_list:
            with st.expander("⚡ Aksi Cepat: Aktifkan / Nonaktifkan Pengguna"):
                user_options = {
                    f"{u['username']} ({u['nama_lengkap'] or u['role']}) - {'Aktif' if u['is_active'] else 'Nonaktif'}": u
                    for u in users_list
                }
                selected_label = st.selectbox("Pilih Akun", list(user_options.keys()), key="quick_toggle_select")
                selected_user = user_options[selected_label]
                
                col_btn1, col_btn2 = st.columns(2)
                with col_btn1:
                    if st.button("🟢 Aktifkan Akun", use_container_width=True, disabled=selected_user["is_active"], key="btn_activate"):
                        success, msg = toggle_user_status(engine, selected_user["id"], True)
                        if success:
                            log_system_activity(
                                "user_management",
                                "Aktivasi Pengguna",
                                f"Admin '{current_username}' mengaktifkan akun '{selected_user['username']}'.",
                                metadata={"target_user": selected_user["username"]},
                            )
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)
                with col_btn2:
                    if st.button("🔴 Nonaktifkan Akun", use_container_width=True, disabled=not selected_user["is_active"], key="btn_deactivate"):
                        success, msg = toggle_user_status(engine, selected_user["id"], False)
                        if success:
                            log_system_activity(
                                "user_management",
                                "Deaktivasi Pengguna",
                                f"Admin '{current_username}' menonaktifkan akun '{selected_user['username']}'.",
                                metadata={"target_user": selected_user["username"]},
                            )
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)

    # ========================================================================
    # TAB 2: TAMBAH PENGGUNA BARU
    # ========================================================================
    with tab_add:
        if not is_admin:
            st.info("Hanya Administrator yang memiliki akses untuk menambahkan pengguna baru.")
        else:
            st.markdown("#### Form Pendaftaran Pengguna Baru")
            st.caption("Data akun baru akan langsung dienkripsi (PBKDF2-SHA256) dan disimpan di PostgreSQL.")

            with st.form("create_user_form", clear_on_submit=True):
                col_u1, col_u2 = st.columns(2)
                with col_u1:
                    new_username = st.text_input("Username *", placeholder="contoh: user_opd1")
                    new_fullname = st.text_input("Nama Lengkap", placeholder="contoh: Budi Hartono")
                with col_u2:
                    new_role = st.selectbox(
                        "Peran / Role *",
                        ["operator", "pimpinan", "admin"],
                        format_func=lambda r: ROLE_LABELS.get(r, r),
                    )
                    new_is_active = st.checkbox("Status Akun Langsung Aktif", value=True)

                col_p1, col_p2 = st.columns(2)
                with col_p1:
                    new_password = st.text_input("Kata Sandi *", type="password", placeholder="Minimal 4 karakter")
                with col_p2:
                    confirm_password = st.text_input("Konfirmasi Kata Sandi *", type="password", placeholder="Ulangi kata sandi")

                submit_create = st.form_submit_button("💾 Simpan Pengguna Baru", use_container_width=True)

            if submit_create:
                uname = new_username.strip()
                if not uname:
                    st.error("Username wajib diisi.")
                elif len(uname) < 3:
                    st.error("Username minimal 3 karakter.")
                elif not new_password:
                    st.error("Kata sandi wajib diisi.")
                elif len(new_password) < 4:
                    st.error("Kata sandi minimal 4 karakter.")
                elif new_password != confirm_password:
                    st.error("Konfirmasi kata sandi tidak cocok.")
                else:
                    try:
                        created = create_user(
                            engine,
                            username=uname,
                            password=new_password,
                            nama_lengkap=new_fullname.strip(),
                            role=new_role,
                            is_active=new_is_active,
                        )
                        log_system_activity(
                            "user_management",
                            "Tambah Pengguna Baru",
                            f"Admin '{current_username}' membuat akun baru '{uname}' dengan peran '{new_role}'.",
                            metadata={"target_user": uname, "role": new_role},
                        )
                        st.success(f"✓ Pengguna '{uname}' berhasil ditambahkan ke database PostgreSQL!")
                        st.rerun()
                    except ValueError as val_err:
                        st.error(f"Gagal: {val_err}")
                    except Exception as err:
                        st.error(f"Terjadi kesalahan saat menyimpan ke database: {err}")

    # ========================================================================
    # TAB 3: EDIT & RESET PASSWORD
    # ========================================================================
    with tab_edit:
        if not is_admin:
            st.info("Hanya Administrator yang memiliki akses untuk mengedit pengguna atau mereset password.")
        elif not users_list:
            st.info("Belum ada data pengguna yang tersedia.")
        else:
            st.markdown("#### Edit Informasi & Reset Kata Sandi Pengguna")
            
            edit_user_map = {
                f"[{u['id']}] {u['username']} — {u['nama_lengkap'] or u['role']}": u
                for u in users_list
            }
            selected_edit_label = st.selectbox("Pilih Akun yang Ingin Diedit", list(edit_user_map.keys()), key="edit_user_select")
            target_user = edit_user_map[selected_edit_label]

            with st.form("edit_user_form"):
                st.markdown(f"**Mengubah Akun:** `@{target_user['username']}` (ID: {target_user['id']})")
                
                col_e1, col_e2 = st.columns(2)
                with col_e1:
                    edit_fullname = st.text_input("Nama Lengkap", value=target_user.get("nama_lengkap") or "")
                    role_options = ["operator", "pimpinan", "admin"]
                    current_role_idx = role_options.index(target_user["role"]) if target_user["role"] in role_options else 0
                    edit_role = st.selectbox(
                        "Peran / Role",
                        role_options,
                        index=current_role_idx,
                        format_func=lambda r: ROLE_LABELS.get(r, r),
                    )
                with col_e2:
                    edit_status = st.checkbox("Status Akun Aktif", value=bool(target_user.get("is_active")))
                    st.caption("Hilangkan centang untuk menonaktifkan login pengguna ini tanpa menghapus riwayatnya.")

                st.markdown("##### 🔑 Reset Kata Sandi (Opsional)")
                st.caption("Biarkan kosong jika tidak ingin mengubah kata sandi.")
                col_pw1, col_pw2 = st.columns(2)
                with col_pw1:
                    edit_new_pw = st.text_input("Kata Sandi Baru", type="password", placeholder="Kosongkan jika tidak diubah")
                with col_pw2:
                    edit_confirm_pw = st.text_input("Konfirmasi Kata Sandi Baru", type="password", placeholder="Ulangi kata sandi baru")

                submit_edit = st.form_submit_button("💾 Perbarui Data Pengguna", use_container_width=True)

            if submit_edit:
                if edit_new_pw and edit_new_pw != edit_confirm_pw:
                    st.error("Konfirmasi kata sandi baru tidak cocok.")
                elif edit_new_pw and len(edit_new_pw) < 4:
                    st.error("Kata sandi baru minimal 4 karakter.")
                else:
                    success, msg = update_user(
                        engine,
                        user_id=target_user["id"],
                        nama_lengkap=edit_fullname,
                        role=edit_role,
                        is_active=edit_status,
                        new_password=edit_new_pw if edit_new_pw else None,
                    )
                    if success:
                        log_system_activity(
                            "user_management",
                            "Update Data Pengguna",
                            f"Admin '{current_username}' memperbarui data akun '{target_user['username']}'.",
                            metadata={"target_user": target_user["username"], "role": edit_role, "active": edit_status},
                        )
                        st.success(f"✓ {msg}")
                        st.rerun()
                    else:
                        st.error(msg)

            st.markdown("---")
            st.markdown("#### 🗑️ Hapus Pengguna dari Database")
            st.caption("Tindakan ini permanen dan akan menghapus akun secara permanen dari PostgreSQL.")

            with st.expander(f"⚠️ Konfirmasi Hapus Akun @{target_user['username']}"):
                st.write(
                    f"Apakah Anda yakin ingin menghapus akun **{target_user['username']}** ({target_user['nama_lengkap'] or target_user['role']})?"
                )
                if st.button(f"🚨 Ya, Hapus Akun {target_user['username']}", type="primary", key="btn_confirm_delete"):
                    del_success, del_msg = delete_user(
                        engine,
                        user_id=target_user["id"],
                        requesting_user_id=current_user_id,
                    )
                    if del_success:
                        log_system_activity(
                            "user_management",
                            "Hapus Pengguna",
                            f"Admin '{current_username}' menghapus akun '{target_user['username']}' dari PostgreSQL.",
                            metadata={"target_user": target_user["username"]},
                        )
                        st.success(del_msg)
                        st.rerun()
                    else:
                        st.error(del_success or del_msg)
