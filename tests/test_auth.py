"""Unit test untuk modul database/auth.py (autentikasi PostgreSQL/SQLAlchemy)."""

from datetime import datetime
import pytest

from database.auth import (
    authenticate_user,
    create_user,
    delete_user,
    get_user_by_id,
    get_user_by_username,
    hash_password,
    init_auth_schema,
    list_users,
    seed_default_users,
    toggle_user_status,
    update_user,
    update_user_password,
    verify_password,
)


@pytest.fixture
def in_memory_engine(postgres_engine):
    """Engine PostgreSQL dengan schema sementara untuk testing schema & CRUD auth secara isolated."""
    engine = postgres_engine
    init_auth_schema(engine)
    return engine


def test_password_hashing_and_verification():
    raw_password = "SecretPassword123"
    hashed = hash_password(raw_password)
    
    assert hashed.startswith("pbkdf2:sha256:100000$")
    assert verify_password(raw_password, hashed) is True
    assert verify_password("WrongPassword", hashed) is False
    assert verify_password("", hashed) is False
    assert verify_password(raw_password, "") is False


def test_seed_default_users_creates_accounts(in_memory_engine):
    created = seed_default_users(in_memory_engine)
    assert "admin" in created
    assert "operator" in created
    assert "pimpinan" in created
    
    # Idempotent: running seed again should not duplicate accounts
    created_again = seed_default_users(in_memory_engine)
    assert created_again == []
    
    users = list_users(in_memory_engine)
    assert len(users) == 3
    usernames = {u["username"] for u in users}
    assert usernames == {"admin", "operator", "pimpinan"}


def test_authenticate_valid_and_invalid_user(in_memory_engine):
    seed_default_users(in_memory_engine)
    
    # Valid admin login
    success, user_data, msg = authenticate_user(in_memory_engine, "admin", "admin123")
    assert success is True
    assert user_data is not None
    assert user_data["username"] == "admin"
    assert user_data["role"] == "admin"
    assert user_data["nama_lengkap"] == "Administrator EWS"
    
    # Valid case-insensitive username login
    success, user_data, _ = authenticate_user(in_memory_engine, "ADMIN", "admin123")
    assert success is True
    assert user_data["username"] == "admin"
    
    # Invalid password
    success, user_data, msg = authenticate_user(in_memory_engine, "admin", "wrongpass")
    assert success is False
    assert user_data is None
    assert "salah" in msg.lower()
    
    # Nonexistent user
    success, user_data, msg = authenticate_user(in_memory_engine, "nonexistent_user", "pass123")
    assert success is False
    assert user_data is None


def test_create_user_and_duplicate_handling(in_memory_engine):
    user = create_user(
        in_memory_engine,
        username="auditor1",
        password="auditpass123",
        nama_lengkap="Auditor Utama",
        role="auditor",
    )
    assert user["username"] == "auditor1"
    assert user["role"] == "auditor"
    
    fetched = get_user_by_username(in_memory_engine, "auditor1")
    assert fetched is not None
    assert fetched["nama_lengkap"] == "Auditor Utama"
    
    # Attempting to create duplicate username should raise ValueError
    with pytest.raises(ValueError, match="sudah digunakan"):
        create_user(in_memory_engine, "auditor1", "newpass123")


def test_update_user_password(in_memory_engine):
    seed_default_users(in_memory_engine)
    
    # Authenticate with old password
    success, _, _ = authenticate_user(in_memory_engine, "operator", "operator123")
    assert success is True
    
    # Update password
    updated = update_user_password(in_memory_engine, "operator", "newSecurePass456")
    assert updated is True
    
    # Old password fails
    success_old, _, _ = authenticate_user(in_memory_engine, "operator", "operator123")
    assert success_old is False
    
    # New password succeeds
    success_new, user_data, _ = authenticate_user(in_memory_engine, "operator", "newSecurePass456")
    assert success_new is True
    assert user_data["username"] == "operator"


def test_get_user_by_id_and_update_user(in_memory_engine):
    seed_default_users(in_memory_engine)
    user = get_user_by_username(in_memory_engine, "operator")
    assert user is not None
    user_id = user["id"]

    fetched = get_user_by_id(in_memory_engine, user_id)
    assert fetched is not None
    assert fetched["username"] == "operator"

    # Update profile and role
    success, msg = update_user(
        in_memory_engine,
        user_id=user_id,
        nama_lengkap="Operator Senior",
        role="operator",
        is_active=True,
        new_password="NewPassword789",
    )
    assert success is True

    updated_user = get_user_by_id(in_memory_engine, user_id)
    assert updated_user["nama_lengkap"] == "Operator Senior"

    # Login with new password
    login_ok, _, _ = authenticate_user(in_memory_engine, "operator", "NewPassword789")
    assert login_ok is True


def test_toggle_user_status_and_login_blocking(in_memory_engine):
    seed_default_users(in_memory_engine)
    op = get_user_by_username(in_memory_engine, "operator")
    assert op is not None

    # Deactivate operator
    success, _ = toggle_user_status(in_memory_engine, op["id"], False)
    assert success is True

    # Login should now fail with inactive message
    login_ok, user_data, msg = authenticate_user(in_memory_engine, "operator", "operator123")
    assert login_ok is False
    assert "dinonaktifkan" in msg.lower()

    # Reactivate operator
    success, _ = toggle_user_status(in_memory_engine, op["id"], True)
    assert success is True
    login_ok, _, _ = authenticate_user(in_memory_engine, "operator", "operator123")
    assert login_ok is True


def test_delete_user_and_sole_admin_protection(in_memory_engine):
    seed_default_users(in_memory_engine)
    op = get_user_by_username(in_memory_engine, "operator")
    admin = get_user_by_username(in_memory_engine, "admin")

    # Cannot delete self
    del_self, msg = delete_user(in_memory_engine, admin["id"], requesting_user_id=admin["id"])
    assert del_self is False
    assert "sendiri" in msg.lower()

    # Cannot delete sole active admin
    del_admin, msg = delete_user(in_memory_engine, admin["id"], requesting_user_id=999)
    assert del_admin is False
    assert "satu-satunya" in msg.lower()

    # Deleting operator succeeds
    del_op, msg = delete_user(in_memory_engine, op["id"], requesting_user_id=admin["id"])
    assert del_op is True
    assert get_user_by_username(in_memory_engine, "operator") is None
