import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


class FakeCursor:
    def __init__(self, sql, params, conn):
        self.sql = " ".join(sql.split())
        self.params = params
        self.conn = conn

    def fetchone(self):
        if "SELECT * FROM pending_actions WHERE id" in self.sql and "FOR UPDATE" in self.sql:
            if self.conn.pending_action_row is not None and self.params:
                self.conn.claimed_action_id = self.params[0]
            return self.conn.pending_action_row
        if "SELECT active FROM repo_allowlist" in self.sql:
            return {"active": self.conn.repo_active}
        if "RETURNING claimed_at" in self.sql:
            return {"claimed_at": self.conn.lease_token}
        if "SELECT 1 FROM pending_actions" in self.sql and "claimed_at = %s" in self.sql:
            return {"1": 1} if self.conn.lease_held else None
        if "INSERT INTO pending_actions" in self.sql and "RETURNING id" in self.sql:
            return {"id": self.conn.new_id}
        if "UPDATE pending_actions" in self.sql and "RETURNING id" in self.sql:
            if self.conn.lease_held:
                return {"id": self.conn.claimed_action_id}
            return None
        return None

    def fetchall(self):
        if "SELECT id, arguments FROM pending_actions" in self.sql:
            if self.conn.existing_pending:
                existing_id, existing_arguments = self.conn.existing_pending
                return [{"id": existing_id, "arguments": existing_arguments}]
            return []
        if "UPDATE pending_actions" in self.sql and "RETURNING" in self.sql:
            return self.conn.stuck_approving_rows or []
        return []


class FakeConn:
    def __init__(
        self,
        repo_active=True,
        existing_pending=None,
        new_id="new-id-1",
        pending_action_row=None,
        stuck_approving_rows=None,
        lease_held=True,
        lease_token="lease-token-1",
    ):
        self.repo_active = repo_active
        self.existing_pending = existing_pending
        self.new_id = new_id
        self.pending_action_row = pending_action_row
        self.stuck_approving_rows = stuck_approving_rows or []
        self.lease_held = lease_held
        self.lease_token = lease_token
        self.claimed_action_id = None
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append((" ".join(sql.split()), params))
        return FakeCursor(sql, params, self)

    def transaction(self):
        return contextlib.nullcontext()


def sync_connection_returning(fake_conn):
    @contextlib.contextmanager
    def fake_sync_connection(dsn):
        yield fake_conn

    return fake_sync_connection
