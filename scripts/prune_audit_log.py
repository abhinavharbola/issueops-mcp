import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psycopg.types.json import Jsonb

from issueops.config import load_config
from issueops.db import sync_connection


def prune(dsn: str, days: int) -> int:
    with sync_connection(dsn) as conn:
        with conn.transaction():
            row = conn.execute(
                """
                WITH cutoff AS (SELECT now() - (%s * interval '1 day') AS at),
                deleted AS (
                    DELETE FROM audit_log
                    WHERE timestamp < (SELECT at FROM cutoff)
                    RETURNING 1
                )
                SELECT (SELECT count(*) FROM deleted) AS n, (SELECT at FROM cutoff) AS cutoff
                """,
                (days,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO audit_log (tool_name, initiator, result_status, arguments, result_summary)
                VALUES ('prune_audit_log', 'system:prune', 'pruned', %s, %s)
                """,
                (
                    Jsonb({"before": row["cutoff"].isoformat(), "days": days}),
                    f"deleted {row['n']} audit_log rows",
                ),
            )
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
