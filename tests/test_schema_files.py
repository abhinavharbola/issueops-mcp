from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "db" / "schema.sql").read_text()
MIGRATIONS = sorted(path.name for path in (ROOT / "db" / "migrations").glob("*.sql"))


def test_the_schema_records_every_migration_file():
    assert MIGRATIONS
    missing = [name for name in MIGRATIONS if f"'{name}'" not in SCHEMA]
    assert missing == []


def test_the_schema_records_no_migration_that_does_not_exist():
    recorded = SCHEMA.split("INSERT INTO schema_migrations (version)", 1)[1]
    names = [part.split("'")[1] for part in recorded.split("VALUES", 1)[1].split(",") if "'" in part]
    assert sorted(names) == MIGRATIONS


def test_migration_files_contain_no_comments():
    for path in (ROOT / "db" / "migrations").glob("*.sql"):
        assert not any(line.lstrip().startswith("--") for line in path.read_text().splitlines())
