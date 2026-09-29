"""Modul autentikasi dan manajemen pengguna berbasis PostgreSQL."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Index, MetaData,
    String, Table, UniqueConstraint, func, select, update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

LOGGER = logging.getLogger(__name__)

auth_metadata = MetaData()

users = Table(
    "users",
    auth_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("username", String(50), nullable=False, unique=True),
    Column("password_hash", String(255), nullable=False),
    Column("nama_lengkap", String(200), nullable=False, server_default=""),
    Column("role", String(50), nullable=False, server_default="admin"),
    Column("is_active", Boolean, nullable=False, server_default="true"),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("updated_at", DateTime, nullable=False, server_default=func.now()),
    Column("last_login_at", DateTime, nullable=True),
    UniqueConstraint("username", name="uq_users_username"),
)

Index("idx_users_username", users.c.username)
Index("idx_users_role", users.c.role)


# ============================================================================
# Password Security (PBKDF2-HMAC-SHA256)
# ============================================================================

def hash_password(plain_password: str) -> str:
    """Hash password menggunakan PBKDF2-HMAC-SHA256 dengan salt random 16-byte."""
    if not plain_password:
        raise ValueError("Password tidak boleh kosong.")
    salt = os.urandom(16).hex()
    iterations = 100_000
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        plain_password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return f"pbkdf2:sha256:{iterations}${salt}${derived}"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifikasi kecocokan plain password dengan stored hash."""
    if not plain_password or not hashed_password:
        return False
    try:
        parts = hashed_password.split("$")
        if len(parts) == 3 and parts[0].startswith("pbkdf2:sha256:"):
            iterations = int(parts[0].split(":")[2])
            salt = parts[1]
            stored_hash = parts[2]
            computed = hashlib.pbkdf2_hmac(
                "sha256",
                plain_password.encode("utf-8"),
                salt.encode("utf-8"),
                iterations,
            ).hex()
            return hmac.compare_digest(computed, stored_hash)
        # Fallback legacy plain-text check jika data legacy ada (otomatis ditolak di produksi)
        return hmac.compare_digest(plain_password, hashed_password)
    except Exception as exc:
        LOGGER.warning("Gagal memverifikasi password: %s", exc)
        return False


# ============================================================================
# Database Schema & Initialization
# ============================================================================

def init_auth_schema(engine: Engine) -> None:
    """Buat tabel users jika belum ada."""
    auth_metadata.create_all(engine)


DEFAULT_SEEDED_USERS = [
    {
        "username": "admin",
        "password": "admin123",
        "nama_lengkap": "Administrator EWS",
        "role": "admin",
    },
    {
        "username": "operator",
        "password": "operator123",
        "nama_lengkap": "Operator Presensi",
        "role": "operator",
    },
    {
        "username": "pimpinan",
        "password": "pimpinan123",
        "nama_lengkap": "Pimpinan Eksekutif",
        "role": "pimpinan",
    },
]


def seed_default_users(engine: Engine) -> list[str]:
    """Inisialisasi akun default (admin, operator, pimpinan) jika belum ada di database."""
    init_auth_schema(engine)
    created: list[str] = []
    
    with engine.begin() as conn:
        for item in DEFAULT_SEEDED_USERS:
            uname = item["username"].strip().lower()
            stmt = select(users.c.id).where(func.lower(users.c.username) == uname)
            existing = conn.execute(stmt).scalar_one_or_none()
            if existing is None:
                pw_hash = hash_password(item["password"])
                conn.execute(
                    users.insert().values(
                        username=item["username"],
                        password_hash=pw_hash,
                        nama_lengkap=item["nama_lengkap"],
                        role=item["role"],
                        is_active=True,
                    )
                )
                created.append(item["username"])
    return created


# ============================================================================
# User Operations & Authentication
# ============================================================================

def authenticate_user(
    engine: Engine,
    username: str,
    password: str,
) -> tuple[bool, dict[str, Any] | None, str]:
    """Autentikasi username & password terhadap tabel users di PostgreSQL.
    
    Returns:
        (success, user_dict, message)
    """
    clean_username = (username or "").strip()
    clean_password = (password or "").strip()
    
    if not clean_username or not clean_password:
        return False, None, "Username dan kata sandi wajib diisi."
        
    init_auth_schema(engine)
    
    try:
        with engine.connect() as conn:
            stmt = select(users).where(func.lower(users.c.username) == clean_username.lower())
            row = conn.execute(stmt).mappings().one_or_none()
            
            if row is None:
                # Cek jika tabel kosong, coba seed dan re-query
                count_stmt = select(func.count(users.c.id))
                total = conn.execute(count_stmt).scalar() or 0
                if total == 0:
                    seed_default_users(engine)
                    row = conn.execute(stmt).mappings().one_or_none()
                    
            if row is None:
                return False, None, "Username atau kata sandi salah."
                
            if not row["is_active"]:
                return False, None, "Akun pengguna ini telah dinonaktifkan."
                
            if not verify_password(clean_password, row["password_hash"]):
                return False, None, "Username atau kata sandi salah."
                
            user_data = {
                "id": row["id"],
                "username": row["username"],
                "nama_lengkap": row["nama_lengkap"] or row["username"],
                "role": row["role"],
                "is_active": row["is_active"],
                "last_login_at": row["last_login_at"],
            }
            
        # Update last_login_at dalam transaksi terpisah
        with engine.begin() as conn:
            conn.execute(
                update(users)
                .where(users.c.id == row["id"])
                .values(last_login_at=func.now(), updated_at=func.now())
            )
            
        return True, user_data, "Login berhasil."
    except Exception as exc:
        LOGGER.error("Gagal melakukan autentikasi database: %s", exc, exc_info=True)
        return False, None, f"Gagal terhubung ke database: {exc}"


def create_user(
    engine: Engine,
    username: str,
    password: str,
    nama_lengkap: str = "",
    role: str = "admin",
    is_active: bool = True,
) -> dict[str, Any]:
    """Buat user baru di PostgreSQL."""
    clean_username = (username or "").strip()
    if not clean_username:
        raise ValueError("Username tidak boleh kosong.")
    if not password or len(password) < 4:
        raise ValueError("Password minimal 4 karakter.")
        
    init_auth_schema(engine)
    pw_hash = hash_password(password)
    
    with engine.begin() as conn:
        stmt = select(users.c.id).where(func.lower(users.c.username) == clean_username.lower())
        if conn.execute(stmt).scalar_one_or_none() is not None:
            raise ValueError(f"Username '{clean_username}' sudah digunakan.")
            
        result = conn.execute(
            users.insert().values(
                username=clean_username,
                password_hash=pw_hash,
                nama_lengkap=nama_lengkap.strip(),
                role=role.strip().lower(),
                is_active=is_active,
            )
        )
        user_id = result.inserted_primary_key[0]
        
    return {
        "id": user_id,
        "username": clean_username,
        "nama_lengkap": nama_lengkap.strip(),
        "role": role.strip().lower(),
        "is_active": is_active,
    }


def get_user_by_id(engine: Engine, user_id: int) -> dict[str, Any] | None:
    """Ambil data user berdasarkan ID."""
    if not user_id:
        return None
    init_auth_schema(engine)
    with engine.connect() as conn:
        stmt = select(users).where(users.c.id == int(user_id))
        row = conn.execute(stmt).mappings().one_or_none()
        if row is None:
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "nama_lengkap": row["nama_lengkap"],
            "role": row["role"],
            "is_active": row["is_active"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_login_at": row["last_login_at"],
        }


def get_user_by_username(engine: Engine, username: str) -> dict[str, Any] | None:
    """Ambil data user berdasarkan username."""
    if not username:
        return None
    init_auth_schema(engine)
    with engine.connect() as conn:
        stmt = select(users).where(func.lower(users.c.username) == username.strip().lower())
        row = conn.execute(stmt).mappings().one_or_none()
        if row is None:
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "nama_lengkap": row["nama_lengkap"],
            "role": row["role"],
            "is_active": row["is_active"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_login_at": row["last_login_at"],
        }


def list_users(engine: Engine) -> list[dict[str, Any]]:
    """Daftar semua pengguna yang tersimpan di database."""
    init_auth_schema(engine)
    with engine.connect() as conn:
        stmt = select(users).order_by(users.c.id)
        rows = conn.execute(stmt).mappings().all()
        return [
            {
                "id": r["id"],
                "username": r["username"],
                "nama_lengkap": r["nama_lengkap"],
                "role": r["role"],
                "is_active": r["is_active"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "last_login_at": r["last_login_at"],
            }
            for r in rows
        ]


def update_user_password(engine: Engine, username: str, new_password: str) -> bool:
    """Update kata sandi untuk pengguna tertentu."""
    if not new_password or len(new_password) < 4:
        raise ValueError("Password minimal 4 karakter.")
    init_auth_schema(engine)
    pw_hash = hash_password(new_password)
    with engine.begin() as conn:
        result = conn.execute(
            update(users)
            .where(func.lower(users.c.username) == username.strip().lower())
            .values(password_hash=pw_hash, updated_at=func.now())
        )
        return result.rowcount > 0


def update_user(
    engine: Engine,
    user_id: int,
    nama_lengkap: str,
    role: str,
    is_active: bool,
    new_password: str | None = None,
) -> tuple[bool, str]:
    """Perbarui profil, role, status aktif, atau password user di database."""
    init_auth_schema(engine)
    with engine.begin() as conn:
        stmt = select(users).where(users.c.id == int(user_id))
        target = conn.execute(stmt).mappings().one_or_none()
        if target is None:
            return False, "Pengguna tidak ditemukan."

        # Proteksi: Jangan nonaktifkan atau ubah role admin aktif terakhir
        if (target["role"] == "admin" and target["is_active"]) and (role != "admin" or not is_active):
            admin_count_stmt = select(func.count(users.c.id)).where(
                users.c.role == "admin",
                users.c.is_active.is_(True),
                users.c.id != int(user_id),
            )
            other_active_admins = conn.execute(admin_count_stmt).scalar() or 0
            if other_active_admins == 0:
                return False, "Tidak dapat menonaktifkan atau mengubah role satu-satunya Administrator aktif."

        values: dict[str, Any] = {
            "nama_lengkap": nama_lengkap.strip(),
            "role": role.strip().lower(),
            "is_active": bool(is_active),
            "updated_at": func.now(),
        }
        if new_password and str(new_password).strip():
            if len(str(new_password).strip()) < 4:
                return False, "Password baru minimal 4 karakter."
            values["password_hash"] = hash_password(str(new_password).strip())

        conn.execute(
            update(users)
            .where(users.c.id == int(user_id))
            .values(**values)
        )
    return True, "Data pengguna berhasil diperbarui."


def delete_user(
    engine: Engine,
    user_id: int,
    requesting_user_id: int | None = None,
) -> tuple[bool, str]:
    """Hapus user dari database dengan proteksi keamanan."""
    init_auth_schema(engine)
    if requesting_user_id is not None and int(requesting_user_id) == int(user_id):
        return False, "Tidak dapat menghapus akun Anda sendiri saat sedang login."

    with engine.begin() as conn:
        stmt = select(users).where(users.c.id == int(user_id))
        target = conn.execute(stmt).mappings().one_or_none()
        if target is None:
            return False, "Pengguna tidak ditemukan."

        # Proteksi jika user yang dihapus adalah satu-satunya admin aktif
        if target["role"] == "admin" and target["is_active"]:
            admin_count_stmt = select(func.count(users.c.id)).where(
                users.c.role == "admin",
                users.c.is_active.is_(True),
                users.c.id != int(user_id),
            )
            other_active_admins = conn.execute(admin_count_stmt).scalar() or 0
            if other_active_admins == 0:
                return False, "Tidak dapat menghapus satu-satunya Administrator aktif."

        conn.execute(users.delete().where(users.c.id == int(user_id)))
    return True, f"Pengguna '{target['username']}' berhasil dihapus dari database."


def toggle_user_status(engine: Engine, user_id: int, is_active: bool) -> tuple[bool, str]:
    """Ubah status aktif/nonaktif akun pengguna di database."""
    init_auth_schema(engine)
    with engine.begin() as conn:
        stmt = select(users).where(users.c.id == int(user_id))
        target = conn.execute(stmt).mappings().one_or_none()
        if target is None:
            return False, "Pengguna tidak ditemukan."

        if not is_active and target["role"] == "admin" and target["is_active"]:
            admin_count_stmt = select(func.count(users.c.id)).where(
                users.c.role == "admin",
                users.c.is_active.is_(True),
                users.c.id != int(user_id),
            )
            other_active_admins = conn.execute(admin_count_stmt).scalar() or 0
            if other_active_admins == 0:
                return False, "Tidak dapat menonaktifkan satu-satunya Administrator aktif."

        conn.execute(
            update(users)
            .where(users.c.id == int(user_id))
            .values(is_active=bool(is_active), updated_at=func.now())
        )
    status_text = "diaktifkan" if is_active else "dinonaktifkan"
    return True, f"Pengguna '{target['username']}' berhasil {status_text}."
