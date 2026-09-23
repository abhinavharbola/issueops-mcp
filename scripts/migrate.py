import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from issueops.config import load_config
from issueops.db import sync_connection

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"


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
        _ensure_migrations_table(conn)
        already_applied = _applied_versions(conn)
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.name
            if version in already_applied:
                continue
            sql = path.read_text()
            with conn.transaction():
                conn.execute(sql)
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            applied.append(version)
    return applied


def main():
    config = load_config(require_write_pat=False)
    applied = run_migrations(config.neon_dsn)
    if applied:
        print(f"applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("database is up to date, nothing to apply")


if __name__ == "__main__":
    main()


