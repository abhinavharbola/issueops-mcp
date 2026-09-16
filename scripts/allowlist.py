import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from issueops.config import load_config
from issueops.db import sync_connection


def add_repo(dsn: str, repo: str):
    with sync_connection(dsn) as conn:
        conn.execute(
            """
            INSERT INTO repo_allowlist (repo, active) VALUES (%s, true)
            ON CONFLICT (repo) DO UPDATE SET active = true
            """,
            (repo,),
        )


def deactivate_repo(dsn: str, repo: str):
    with sync_connection(dsn) as conn:
        conn.execute("UPDATE repo_allowlist SET active = false WHERE repo = %s", (repo,))


def list_repos(dsn: str):
    with sync_connection(dsn) as conn:
        return conn.execute("SELECT repo, active, added_at FROM repo_allowlist ORDER BY added_at").fetchall()


def main():
    parser = argparse.ArgumentParser(description="Manage the IssueOps repo allowlist.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add")
    add_parser.add_argument("repo")

    deactivate_parser = subparsers.add_parser("deactivate")
    deactivate_parser.add_argument("repo")

    subparsers.add_parser("list")

    args = parser.parse_args()
    config = load_config(require_write_pat=False)

    if args.command == "add":
        add_repo(config.neon_dsn, args.repo)
        print(f"added {args.repo}")
    elif args.command == "deactivate":
        deactivate_repo(config.neon_dsn, args.repo)
        print(f"deactivated {args.repo}")
    elif args.command == "list":
        for row in list_repos(config.neon_dsn):
            print(row)


if __name__ == "__main__":
    main()



