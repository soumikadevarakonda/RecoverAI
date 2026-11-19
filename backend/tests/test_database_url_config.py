from typing import get_type_hints

from app.core.config import Settings


def test_plain_postgresql_url_uses_psycopg_driver() -> None:
    settings = Settings(database_url="postgresql://user:pass@localhost/db")

    assert settings.database_url == "postgresql+psycopg://user:pass@localhost/db"


def test_existing_psycopg_url_is_unchanged() -> None:
    database_url = "postgresql+psycopg://user:pass@localhost/db"

    settings = Settings(database_url=database_url)

    assert settings.database_url == database_url


def test_recovery_service_imports_with_lazy_razorpay_client() -> None:
    from app.domains.recovery.service import execute_recovery_attempt

    assert callable(execute_recovery_attempt)


def test_recovery_evidence_annotations_resolve() -> None:
    from app.domains.recovery.evidence import build_decision_evidence

    assert "guardrail_result" in get_type_hints(build_decision_evidence)
