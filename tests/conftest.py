"""tests/conftest.py — pytest fixtures.

Requires a running Postgres reachable via TEST_DB_URL with the
bp_router schema applied (run `alembic upgrade head` against it
beforehand, or use the migrate-on-start fixture below).
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def test_db_url() -> str:
    url = os.environ.get("TEST_DB_URL")
    if not url:
        pytest.skip("TEST_DB_URL not set; integration tests skipped")
    return url
