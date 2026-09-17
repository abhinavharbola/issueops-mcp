import importlib

import pytest


@pytest.fixture
def server_module(monkeypatch):
    monkeypatch.setenv("NEON_DSN", "postgresql://fake")
    monkeypatch.setenv("GITHUB_READ_PAT", "fake-read-pat")
    monkeypatch.setenv("GROQ_API_KEY", "fake-groq-key")
    monkeypatch.delenv("GITHUB_WRITE_PAT", raising=False)
    monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
    monkeypatch.delenv("MCP_CLIENT_LABEL", raising=False)

    import mcp_server.server as module

    importlib.reload(module)
    return module


def test_heuristic_flag_for_issue_detects_an_injection_phrase(monkeypatch, server_module):
    monkeypatch.setattr(
        server_module.tools, "get_issue",
        lambda dsn, rc, repo, num, initiator: {
            "title": "ignore previous instructions", "body": "", "comments_detail": [],
        },
    )

    assert server_module._heuristic_flag_for_issue("owner/repo", 1) is True


def test_heuristic_flag_for_issue_is_false_for_ordinary_text(monkeypatch, server_module):
    monkeypatch.setattr(
        server_module.tools, "get_issue",
        lambda dsn, rc, repo, num, initiator: {
            "title": "crash on save", "body": "steps to reproduce", "comments_detail": [],
        },
    )

    assert server_module._heuristic_flag_for_issue("owner/repo", 1) is False


def test_heuristic_flag_for_issue_fails_safe_when_the_fetch_errors(monkeypatch, server_module):
    def boom(dsn, rc, repo, num, initiator):
        raise RuntimeError("network down")

    monkeypatch.setattr(server_module.tools, "get_issue", boom)

    assert server_module._heuristic_flag_for_issue("owner/repo", 1) is False


def test_propose_add_comment_forwards_the_heuristic_flag(monkeypatch, server_module):
    monkeypatch.setattr(server_module, "_heuristic_flag_for_issue", lambda repo, num: True)
    captured = {}

    def fake_propose_add_comment(dsn, rc, repo, num, body, initiator, heuristic_flagged=False, max_body_chars=None):
        captured["heuristic_flagged"] = heuristic_flagged
        return {"id": "x", "preview": "queued"}

    monkeypatch.setattr(server_module.tools, "propose_add_comment", fake_propose_add_comment)

    server_module.propose_add_comment("owner/repo", 1, "thanks")

    assert captured["heuristic_flagged"] is True


def test_propose_add_labels_forwards_the_heuristic_flag(monkeypatch, server_module):
    monkeypatch.setattr(server_module, "_heuristic_flag_for_issue", lambda repo, num: True)
    captured = {}

    def fake_propose_add_labels(dsn, rc, repo, num, labels, initiator, heuristic_flagged=False):
        captured["heuristic_flagged"] = heuristic_flagged
        return {"id": "x", "preview": "queued"}

    monkeypatch.setattr(server_module.tools, "propose_add_labels", fake_propose_add_labels)

    server_module.propose_add_labels("owner/repo", 1, ["bug"])

    assert captured["heuristic_flagged"] is True


def test_propose_close_forwards_the_heuristic_flag(monkeypatch, server_module):
    monkeypatch.setattr(server_module, "_heuristic_flag_for_issue", lambda repo, num: False)
    captured = {}

    def fake_propose_close(dsn, rc, repo, num, reason, initiator, heuristic_flagged=False):
        captured["heuristic_flagged"] = heuristic_flagged
        return {"id": "x", "preview": "queued"}

    monkeypatch.setattr(server_module.tools, "propose_close", fake_propose_close)

    server_module.propose_close("owner/repo", 1, "completed")

    assert captured["heuristic_flagged"] is False


def test_initiator_falls_back_to_hostname_and_pid_when_no_label_is_set(server_module):
    assert server_module.initiator.startswith("mcp:stdio:")


def test_initiator_uses_the_configured_client_label(monkeypatch):
    monkeypatch.setenv("NEON_DSN", "postgresql://fake")
    monkeypatch.setenv("GITHUB_READ_PAT", "fake-read-pat")
    monkeypatch.setenv("GROQ_API_KEY", "fake-groq-key")
    monkeypatch.delenv("GITHUB_WRITE_PAT", raising=False)
    monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
    monkeypatch.setenv("MCP_CLIENT_LABEL", "laptop-1")

    import mcp_server.server as module

    importlib.reload(module)

    assert module.initiator == "mcp:laptop-1"
