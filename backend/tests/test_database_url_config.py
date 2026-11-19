from app.core.config import Settings


def test_plain_postgresql_url_uses_psycopg_driver() -> None:
    settings = Settings(database_url="postgresql://user:pass@localhost/db")

    assert settings.database_url == "postgresql+psycopg://user:pass@localhost/db"


def test_existing_psycopg_url_is_unchanged() -> None:
    database_url = "postgresql+psycopg://user:pass@localhost/db"

    settings = Settings(database_url=database_url)

    assert settings.database_url == database_url
