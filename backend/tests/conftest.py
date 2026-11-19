"""
Test-suite safety guard.

The test fixtures in this suite DELETE EVERY ROW from the application tables
before each test. To make it impossible to wipe a development or demo database
by accident, the suite only runs against a database configured through
TEST_DATABASE_URL, and that database's name must contain "test".

Example (PowerShell):
    $env:PYTHONPATH="backend"
    $env:TEST_DATABASE_URL="postgresql+psycopg://postgres:admin@localhost:5432/recoverai_test"
    python -m pytest backend/tests -q
"""
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _configure_test_database() -> None:
    test_url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not test_url:
        pytest.exit(
            "TEST_DATABASE_URL is not set. The test suite deletes all rows from the "
            "application tables, so it refuses to run against DATABASE_URL. Point "
            "TEST_DATABASE_URL at a dedicated database (its name must contain 'test').",
            returncode=4,
        )

    db_name = make_url(test_url).database or ""
    if "test" not in db_name.lower():
        pytest.exit(
            f"Refusing to run: TEST_DATABASE_URL database '{db_name}' does not contain "
            "'test' in its name. Use a dedicated, disposable test database.",
            returncode=4,
        )

    # Must happen before app.core.config is imported anywhere, so the app's
    # engine binds to the test database.
    os.environ["DATABASE_URL"] = test_url
    os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "test-webhook-secret")
    # Never call real external services from the test suite, even if
    # backend/.env contains live credentials.
    os.environ["GEMINI_API_KEY"] = ""
    os.environ["RAZORPAY_KEY_ID"] = ""
    os.environ["RAZORPAY_KEY_SECRET"] = ""


_configure_test_database()
