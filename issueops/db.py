from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


@contextmanager
def sync_connection(dsn: str):
    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as conn:
        yield conn


