import os
import uuid
from pathlib import Path

import pytest

from dashboard import auth
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


@needs_database
def test_a_match_outside_the_stored_text_is_called_out(monkeypatch, dashboard_dsn):
    _insert(
        dashboard_dsn, flagged=True,
        excerpt={
            "title": "t", "body": "b", "comments": [], "comments_omitted": 0,
            "comments_truncated_by_fetcher": False, "flag_matches": ["system prompt"],
            "flag_matches_not_shown": ["system prompt"], "text_truncated": True,
        },
    )

    at = _run(monkeypatch, dsn=dashboard_dsn, DASHBOARD_ALLOW_INSECURE="true")

    assert not at.exception
    assert any("NOT stored or shown" in e.value for e in at.error)
    assert any("cut to fit storage" in c.value for c in at.caption)


@needs_database
def test_a_needs_review_row_is_listed_and_can_be_resolved(monkeypatch, dashboard_dsn):
    import psycopg

    action_id = _insert(dashboard_dsn)
    with psycopg.connect(dashboard_dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE pending_actions SET status = 'needs_review', claimed_by = 'alice', claimed_at = now(), "
            "execution_started_at = now() WHERE id = %s",
            (action_id,),
        )

    at = _run(monkeypatch, dsn=dashboard_dsn, DASHBOARD_ALLOW_INSECURE="true")
    at.sidebar.text_input(key="approver_name").set_value("bob").run()

    assert not at.exception
    assert any("need review" in w.value for w in at.sidebar.warning)
    next(b for b in at.button if b.label == "It was applied on GitHub").click().run()

    with psycopg.connect(dashboard_dsn, autocommit=True) as conn:
        status = conn.execute("SELECT status FROM pending_actions WHERE id = %s", (action_id,)).fetchone()[0]
    assert status == "executed"


def test_the_correct_token_matches():
    assert auth.token_matches("s3cret", "s3cret") is True


@pytest.mark.parametrize("entered", ["", None, "wrong", "s3cret ", "S3CRET"])
def test_wrong_or_empty_tokens_do_not_match(entered):
    assert auth.token_matches(entered, "s3cret") is False


def test_nothing_matches_when_no_token_is_configured():
    assert auth.token_matches("anything", None) is False
    assert auth.token_matches("", "") is False
