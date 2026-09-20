import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from issueops.config import load_config
from issueops.db import sync_connection


def prune(dsn: str, days: int) -> int:
    with sync_connection(dsn) as conn:
        row = conn.execute(
            """
            WITH deleted AS (
                DELETE FROM audit_log
                WHERE timestamp < now() - (%s * interval '1 day')
                RETURNING 1
            )
            SELECT count(*) AS n FROM deleted
            """,
            (days,),
        ).fetchone()
    return row["n"]


def main():
    parser = argparse.ArgumentParser(description="Delete audit_log rows older than a retention window.")
    parser.add_argument("--days", type=int, required=True)
    args = parser.parse_args()
    if args.days <= 0:
        raise SystemExit("--days must be a positive integer")

    config = load_config(require_write_pat=False)
    print(f"deleted {prune(config.neon_dsn, args.days)} audit_log rows")


if __name__ == "__main__":
    main()
