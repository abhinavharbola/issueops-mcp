import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from issueops.config import load_neon_dsn
from issueops.db import sync_connection

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"
MIGRATION_LOCK_KEY = 7242001


def _ensure_migrations_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def _applied_versions(conn) -> set[str]:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row["version"] for row in rows}


def run_migrations(dsn: str) -> list[str]:
    applied = []
    with sync_connection(dsn) as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
            _ensure_migrations_table(conn)
            already_applied = _applied_versions(conn)
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                version = path.name
                if version in already_applied:
                    continue
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                applied.append(version)
    return applied


def main():
    applied = run_migrations(load_neon_dsn())
    if applied:
        print(f"applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("database is up to date, nothing to apply")


if __name__ == "__main__":
    main()
