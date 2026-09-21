import os

import pytest

import issueops.config as config

REQUIRED_VARS = [
    "NEON_DSN",
    "GITHUB_READ_PAT",
    "GITHUB_WRITE_PAT",
    "GROQ_API_KEY",
    "LOGFIRE_TOKEN",
    "PENDING_ACTION_TTL_HOURS",
    "STUCK_APPROVING_RECOVERY_MINUTES",
    "COMMENT_BODY_MAX_CHARS",
    "MCP_CLIENT_LABEL",
    "DASHBOARD_ACCESS_TOKEN",
    "DASHBOARD_ALLOW_INSECURE",
    "ISSUEOPS_ENV_FILE",
]


@pytest.fixture(autouse=True)
def _restore_environment():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _reset_write_pat_dropped_flag():
    config._write_pat_dropped_in_process = False
    yield
    config._write_pat_dropped_in_process = False


def _clear_env(monkeypatch):
    for name in REQUIRED_VARS:
        monkeypatch.delenv(name, raising=False)


def _set_minimum_required_env(monkeypatch):
    monkeypatch.setenv("NEON_DSN", "postgresql://fake")
    monkeypatch.setenv("GITHUB_READ_PAT", "read-pat")
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")


def test_importing_the_module_does_not_touch_the_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_WRITE_PAT", "should-survive-a-bare-import")
    import importlib

    importlib.reload(config)

    assert os.environ.get("GITHUB_WRITE_PAT") == "should-survive-a-bare-import"


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


def test_write_pat_required_after_a_prior_drop_in_the_same_process_raises_a_clear_error(monkeypatch):
    _clear_env(monkeypatch)
    _set_minimum_required_env(monkeypatch)
    monkeypatch.setenv("GITHUB_WRITE_PAT", "write-pat")
    config.load_config(require_write_pat=False)

    with pytest.raises(RuntimeError, match="same process"):
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
    assert result.stuck_approving_recovery_minutes == 10


def test_ttl_and_comment_max_are_read_from_the_environment(monkeypatch):
    _clear_env(monkeypatch)
    _set_minimum_required_env(monkeypatch)
    monkeypatch.setenv("PENDING_ACTION_TTL_HOURS", "12")
    monkeypatch.setenv("COMMENT_BODY_MAX_CHARS", "1000")
    monkeypatch.setenv("STUCK_APPROVING_RECOVERY_MINUTES", "3")

    result = config.load_config(require_write_pat=False)

    assert result.pending_action_ttl_hours == 12
    assert result.comment_body_max_chars == 1000
    assert result.stuck_approving_recovery_minutes == 3


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


def test_groq_key_is_optional_by_default(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("NEON_DSN", "postgresql://fake")
    monkeypatch.setenv("GITHUB_READ_PAT", "read-pat")

    result = config.load_config()

    assert result.groq_api_key is None


def test_groq_key_is_required_when_asked_for(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("NEON_DSN", "postgresql://fake")
    monkeypatch.setenv("GITHUB_READ_PAT", "read-pat")

    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        config.load_config(require_groq=True)


def test_groq_key_is_returned_when_present_and_required(monkeypatch):
    _clear_env(monkeypatch)
    _set_minimum_required_env(monkeypatch)

    assert config.load_config(require_groq=True).groq_api_key == "groq-key"


def _write_env_file(tmp_path, monkeypatch):
    path = tmp_path / "issueops.env"
    path.write_text("NEON_DSN=postgresql://from-file\nGITHUB_READ_PAT=read-from-file\nGITHUB_WRITE_PAT=write-from-file\n")
    _clear_env(monkeypatch)
    monkeypatch.setenv("ISSUEOPS_ENV_FILE", str(path))


def test_a_read_only_process_never_loads_the_write_pat_from_the_env_file(tmp_path, monkeypatch):
    _write_env_file(tmp_path, monkeypatch)

    result = config.load_config(require_write_pat=False)

    assert result.neon_dsn == "postgresql://from-file"
    assert result.github_read_pat == "read-from-file"
    assert result.github_write_pat is None
    assert "GITHUB_WRITE_PAT" not in os.environ


def test_the_write_capable_process_loads_the_write_pat_from_the_env_file(tmp_path, monkeypatch):
    _write_env_file(tmp_path, monkeypatch)

    result = config.load_config(require_write_pat=True)

    assert result.github_write_pat == "write-from-file"


def test_a_prior_read_only_load_blocks_a_later_write_load_even_when_the_env_file_has_the_pat(tmp_path, monkeypatch):
    _write_env_file(tmp_path, monkeypatch)
    config.load_config(require_write_pat=False)

    with pytest.raises(RuntimeError, match="same process"):
        config.load_config(require_write_pat=True)


def test_real_environment_variables_win_over_the_env_file(tmp_path, monkeypatch):
    _write_env_file(tmp_path, monkeypatch)
    monkeypatch.setenv("NEON_DSN", "postgresql://from-real-env")

    assert config.load_config().neon_dsn == "postgresql://from-real-env"


@pytest.mark.parametrize("raw, expected", [("true", True), ("1", True), ("YES", True), ("false", False), ("", False), ("nope", False)])
def test_dashboard_allow_insecure_is_parsed_from_the_environment(monkeypatch, raw, expected):
    _clear_env(monkeypatch)
    _set_minimum_required_env(monkeypatch)
    monkeypatch.setenv("DASHBOARD_ALLOW_INSECURE", raw)

    result = config.load_config(require_write_pat=False)

    assert result.dashboard_allow_insecure is expected


def test_dashboard_allow_insecure_defaults_to_false(monkeypatch):
    _clear_env(monkeypatch)
    _set_minimum_required_env(monkeypatch)

    assert config.load_config(require_write_pat=False).dashboard_allow_insecure is False


def test_repr_never_contains_a_secret(monkeypatch):
    secrets = {
        "NEON_DSN": "postgresql://user:dsn-secret@host/db",
        "GITHUB_READ_PAT": "read-secret",
        "GITHUB_WRITE_PAT": "write-secret",
        "GROQ_API_KEY": "groq-secret",
        "LOGFIRE_TOKEN": "logfire-secret",
        "DASHBOARD_ACCESS_TOKEN": "dashboard-secret",
    }
    monkeypatch.setenv("ISSUEOPS_ENV_FILE", os.devnull)
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)

    loaded = config.load_config(require_write_pat=True)

    rendered = repr(loaded) + str(loaded)
    for value in secrets.values():
        assert value not in rendered


