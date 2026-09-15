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
