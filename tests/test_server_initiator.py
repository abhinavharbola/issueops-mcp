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
