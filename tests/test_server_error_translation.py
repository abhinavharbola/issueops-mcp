import importlib

import pytest
import requests


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


def test_repo_not_allowed_becomes_a_tool_error(server_module):
    from mcp.server.mcpserver.exceptions import ToolError

    from issueops.tools import RepoNotAllowedError

    @server_module._translate_errors
    def boom():
        raise RepoNotAllowedError("owner/repo not allowlisted")

    with pytest.raises(ToolError):
        boom()


def test_a_raw_network_failure_becomes_a_tool_error(server_module):
    from mcp.server.mcpserver.exceptions import ToolError

    @server_module._translate_errors
    def boom():
        raise requests.exceptions.ConnectionError("refused")

    with pytest.raises(ToolError):
        boom()


def test_an_unrelated_exception_is_left_unchanged(server_module):
    @server_module._translate_errors
    def boom():
        raise KeyError("not translated")

    with pytest.raises(KeyError):
        boom()
