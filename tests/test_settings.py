"""Settings that govern schema migration during active development (M5 CP1)."""

from pinch_backend.settings import Settings


def test_migration_flags_default_on_for_development() -> None:
    s = Settings()
    assert s.database_migrate_updates is True
    assert s.database_migrate_destructive is True
