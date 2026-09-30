"""Unit test untuk migrasi skema dan seeder data."""

from __future__ import annotations

import pandas as pd
from sqlalchemy import create_engine, select

from database.auth import list_users, seed_default_users
from database.opd import list_opds, seed_default_opds
from database.periode import list_periode
from database.migrate_and_seed import seed_all_periods
from database.schema import metadata


def test_migration_and_seed_pipeline() -> None:
    engine = create_engine("sqlite:///:memory:")
    
    # 1. Create Schema
    metadata.create_all(engine)
    
    # 2. Seed Users
    created_users = seed_default_users(engine)
    assert len(created_users) == 3
    assert "admin" in created_users
    assert "operator" in created_users
    assert "pimpinan" in created_users
    
    # Idempotency check: running seed again should not duplicate
    recreated = seed_default_users(engine)
    assert len(recreated) == 0
    all_users = list_users(engine)
    assert len(all_users) == 3

    # 3. Seed OPD
    created_opds = seed_default_opds(engine)
    assert len(created_opds) >= 7
    all_opds = list_opds(engine)
    assert len(all_opds) >= 7
    opd_names = [o["singkatan"] for o in all_opds if o.get("singkatan")]
    assert "BAPPEDA" in opd_names
    assert "BKAD" in opd_names
    assert "DISKOMINFO" in opd_names

    # 4. Seed Periode
    periods = seed_all_periods(engine, year=2026)
    assert len(periods) == 12
    all_p = list_periode(engine)
    assert len(all_p) == 12
