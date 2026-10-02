"""Run database tests on PostgreSQL with isolated per-test schemas.

Usage: python scripts/run_postgres_tests.py
Configure TEST_DATABASE_URL to override the connection from .env.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


def main():
    targets = sys.argv[1:] or [
        "tests/test_auth.py", "tests/test_opd.py", "tests/test_pegawai.py",
        "tests/test_presensi_periode.py", "tests/test_migrate_and_seed.py",
        "tests/test_database_repository.py",
    ]
    return pytest.main(["-q", "--tb=short", *targets])


if __name__ == "__main__":
    sys.exit(main())
