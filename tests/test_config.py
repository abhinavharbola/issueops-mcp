import os

import pytest

import issueops.config as config

REQUIRED_VARS = [
    "NEON_DSN",
    "GITHUB_READ_PAT",
    "GITHUB_WRITE_PAT",
    "GROQ_API_KEY",
    "GROQ_API_KEY_FALLBACK",
    "LOGFIRE_TOKEN",
    "PENDING_ACTION_TTL_HOURS",
    "COMMENT_BODY_MAX_CHARS",
    "MCP_CLIENT_LABEL",
    "DASHBOARD_ACCESS_TOKEN",
]


def _clear_env(monkeypatch):
    for name in REQUIRED_VARS:
        monkeypatch.delenv(name, raising=False)


def _set_minimum_required_env(monkeypatch):
    monkeypatch.setenv("NEON_DSN", "postgresql://fake")
    monkeypatch.setenv("GITHUB_READ_PAT", "read-pat")
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")


def test_missing_required_var_raises(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("GITHUB_READ_PAT", "read-pat")
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")

    with pytest.raises(RuntimeError):
        config.load_config(require_write_pat=False)


def test_write_pat_is_dropped_from_the_environment_when_not_required(monkeypatch):
    _clear_env(monkeypatch)
    _set_minimum_required_env(monkeypatch)
    monkeypatch.setenv("GITHUB_WRITE_PAT", "write-pat")

    result = config.load_config(require_write_pat=False)

    assert result.github_write_pat is None
    assert "GITHUB_WRITE_PAT" not in os.environ


def test_write_pat_required_but_missing_raises(monkeypatch):
    _clear_env(monkeypatch)
    _set_minimum_required_env(monkeypatch)

    with pytest.raises(RuntimeError):
        config.load_config(require_write_pat=True)


def test_write_pat_required_and_present_is_kept(monkeypatch):
    _clear_env(monkeypatch)
    _set_minimum_required_env(monkeypatch)
    monkeypatch.setenv("GITHUB_WRITE_PAT", "write-pat")

    result = config.load_config(require_write_pat=True)

    assert result.github_write_pat == "write-pat"


def test_ttl_and_comment_max_default_when_unset(monkeypatch):
    _clear_env(monkeypatch)
    _set_minimum_required_env(monkeypatch)

    result = config.load_config(require_write_pat=False)

    assert result.pending_action_ttl_hours == 48
    assert result.comment_body_max_chars == 65536


def test_ttl_and_comment_max_are_read_from_the_environment(monkeypatch):
    _clear_env(monkeypatch)
    _set_minimum_required_env(monkeypatch)
    monkeypatch.setenv("PENDING_ACTION_TTL_HOURS", "12")
    monkeypatch.setenv("COMMENT_BODY_MAX_CHARS", "1000")

    result = config.load_config(require_write_pat=False)

    assert result.pending_action_ttl_hours == 12
    assert result.comment_body_max_chars == 1000


def test_non_integer_ttl_raises(monkeypatch):
    _clear_env(monkeypatch)
    _set_minimum_required_env(monkeypatch)
    monkeypatch.setenv("PENDING_ACTION_TTL_HOURS", "not-a-number")

    with pytest.raises(RuntimeError):
        config.load_config(require_write_pat=False)


def test_zero_or_negative_comment_max_raises(monkeypatch):
    _clear_env(monkeypatch)
    _set_minimum_required_env(monkeypatch)
    monkeypatch.setenv("COMMENT_BODY_MAX_CHARS", "0")

    with pytest.raises(RuntimeError):
        config.load_config(require_write_pat=False)


def test_mcp_client_label_and_dashboard_token_default_to_none(monkeypatch):
    _clear_env(monkeypatch)
    _set_minimum_required_env(monkeypatch)

    result = config.load_config(require_write_pat=False)

    assert result.mcp_client_label is None
    assert result.dashboard_access_token is None


def test_mcp_client_label_and_dashboard_token_are_read_from_the_environment(monkeypatch):
    _clear_env(monkeypatch)
    _set_minimum_required_env(monkeypatch)
    monkeypatch.setenv("MCP_CLIENT_LABEL", "laptop-1")
    monkeypatch.setenv("DASHBOARD_ACCESS_TOKEN", "secret-token")

    result = config.load_config(require_write_pat=False)

    assert result.mcp_client_label == "laptop-1"
    assert result.dashboard_access_token == "secret-token"
