import os
import uuid
from pathlib import Path

import pytest

from issueops import config as config_module

streamlit_testing = pytest.importorskip("streamlit.testing.v1")

APP_PATH = str(Path(__file__).resolve().parent.parent / "dashboard" / "app.py")
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    for name in ("DASHBOARD_ACCESS_TOKEN", "DASHBOARD_ALLOW_INSECURE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ISSUEOPS_ENV_FILE", os.devnull)
    monkeypatch.setenv("GITHUB_READ_PAT", "read")
    monkeypatch.setenv("GITHUB_WRITE_PAT", "write")
    monkeypatch.setattr(config_module, "_write_pat_dropped_in_process", False)
    monkeypatch.setattr("dashboard.auth.DELAY_PER_RECENT_FAILURE_SECONDS", 0)


def _run(monkeypatch, dsn="postgresql://unused/unused", **env):
    monkeypatch.setenv("NEON_DSN", dsn)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return streamlit_testing.AppTest.from_file(APP_PATH, default_timeout=30).run()


def test_the_dashboard_refuses_to_start_without_a_token(monkeypatch):
    at = _run(monkeypatch)

    assert not at.exception
    assert any("refuses to start" in e.value for e in at.error)
    assert len(at.button) == 0


def test_a_wrong_token_shows_an_error_and_no_actions(monkeypatch):
    at = _run(monkeypatch, DASHBOARD_ACCESS_TOKEN="right")

    at.sidebar.text_input(key="dashboard_token").set_value("wrong").run()

    assert not at.exception
    assert any("correct access token" in e.value for e in at.sidebar.error)
    assert len(at.button) == 0


def test_a_session_is_locked_after_repeated_wrong_tokens_even_with_the_right_one(monkeypatch):
    at = _run(monkeypatch, DASHBOARD_ACCESS_TOKEN="right")

    for attempt in range(6):
        at.sidebar.text_input(key="dashboard_token").set_value(f"wrong-{attempt}").run()
    at.sidebar.text_input(key="dashboard_token").set_value("right").run()

    assert any("Too many failed attempts" in e.value for e in at.sidebar.error)
    assert len(at.button) == 0


def test_another_session_is_not_locked_out_by_a_different_sessions_failures(monkeypatch):
    attacker = _run(monkeypatch, DASHBOARD_ACCESS_TOKEN="right")
    for attempt in range(8):
        attacker.sidebar.text_input(key="dashboard_token").set_value(f"guess-{attempt}").run()

    legitimate = streamlit_testing.AppTest.from_file(APP_PATH, default_timeout=30).run()
    legitimate.sidebar.text_input(key="dashboard_token").set_value("right").run()

    assert not any("Too many failed attempts" in e.value for e in legitimate.sidebar.error)
    assert not any("correct access token" in e.value for e in legitimate.sidebar.error)


needs_database = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not set")


@pytest.fixture
def dashboard_dsn():
    import psycopg

    schema = "it_" + uuid.uuid4().hex[:12]
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    joiner = "&" if "?" in DATABASE_URL else "?"
    scoped = f"{DATABASE_URL}{joiner}options=-csearch_path%3D{schema}"
    with psycopg.connect(scoped, autocommit=True) as conn:
        conn.execute(SCHEMA_PATH.read_text())
        conn.execute("INSERT INTO repo_allowlist (repo) VALUES ('owner/repo')")
    yield scoped
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(f"DROP SCHEMA {schema} CASCADE")


def _insert(dsn, flagged=False, excerpt=None, rationale=None):
    import psycopg
    from psycopg.types.json import Jsonb

    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            """
            INSERT INTO pending_actions
                (tool_name, repo, issue_number, arguments, issue_state_snapshot, heuristic_flagged,
                 requested_by, source_excerpt, rationale)
            VALUES ('propose_add_comment', 'owner/repo', 3, %s, %s, %s, 'agent:cli', %s, %s)
            RETURNING id
            """,
            (
                Jsonb({"body": "hello"}), Jsonb({"state": "open", "labels": [], "assignees": []}), flagged,
                Jsonb(excerpt) if excerpt else None, rationale,
            ),
        ).fetchone()[0]


def _texts(at):
    return [t.value for t in at.text]


@needs_database
def test_the_review_panel_shows_the_stored_rationale_body_and_comments(monkeypatch, dashboard_dsn):
    _insert(
        dashboard_dsn,
        excerpt={
            "title": "Crash on start", "body": "It crashes.",
            "comments": [{"author": "alice", "body": "same here"}], "comments_omitted": 0,
            "comments_truncated_by_fetcher": False, "flag_matches": [],
        },
        rationale="Looks like a bug report",
    )

    at = _run(monkeypatch, dsn=dashboard_dsn, DASHBOARD_ALLOW_INSECURE="true")

    assert not at.exception
    shown = _texts(at)
    assert "Looks like a bug report" in shown
    assert "It crashes." in shown
    assert "comment by alice: same here" in shown


@needs_database
def test_a_flagged_proposal_lists_the_matched_phrases_and_needs_an_acknowledgement(monkeypatch, dashboard_dsn):
    _insert(
        dashboard_dsn, flagged=True,
        excerpt={
            "title": "t", "body": "b", "comments": [], "comments_omitted": 0,
            "comments_truncated_by_fetcher": False, "flag_matches": ["system prompt"],
        },
    )

    at = _run(monkeypatch, dsn=dashboard_dsn, DASHBOARD_ALLOW_INSECURE="true")
    at.sidebar.text_input(key="approver_name").set_value("alice").run()
    approve = next(b for b in at.button if b.label == "Approve")

    assert any("system prompt" in w.value for w in at.warning)
    assert approve.disabled is True

    at.checkbox[0].check().run()
    approve = next(b for b in at.button if b.label == "Approve")

    assert approve.disabled is False


@needs_database
def test_a_whitespace_only_approver_name_does_not_enable_the_buttons(monkeypatch, dashboard_dsn):
    _insert(dashboard_dsn)

    at = _run(monkeypatch, dsn=dashboard_dsn, DASHBOARD_ALLOW_INSECURE="true")
    at.sidebar.text_input(key="approver_name").set_value("   ").run()

    assert next(b for b in at.button if b.label == "Approve").disabled is True
    assert next(b for b in at.button if b.label == "Reject").disabled is True


@needs_database
def test_a_proposal_without_a_stored_excerpt_still_renders(monkeypatch, dashboard_dsn):
    _insert(dashboard_dsn)

    at = _run(monkeypatch, dsn=dashboard_dsn, DASHBOARD_ALLOW_INSECURE="true")

    assert not at.exception
    assert any("No issue text was stored" in c.value for c in at.caption)
