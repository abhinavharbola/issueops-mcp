import os
from dataclasses import dataclass, field

from dotenv import dotenv_values, find_dotenv

WRITE_PAT_NAME = "GITHUB_WRITE_PAT"


@dataclass(frozen=True)
class Config:
    neon_dsn: str = field(repr=False)
    github_read_pat: str = field(repr=False)
    github_write_pat: str | None = field(repr=False)
    groq_api_key: str | None = field(repr=False)
    logfire_token: str | None = field(repr=False)
    pending_action_ttl_hours: int = 48
    stuck_approving_recovery_minutes: int = 10
    comment_body_max_chars: int = 65536
    mcp_client_label: str | None = None
    dashboard_access_token: str | None = field(default=None, repr=False)
    dashboard_allow_insecure: bool = False


_write_pat_dropped_in_process = False


def _load_env_file(include_write_pat: bool) -> None:
    path = os.environ.get("ISSUEOPS_ENV_FILE") or find_dotenv()
    if not path:
        return
    for key, value in dotenv_values(path).items():
        if value is None:
            continue
        if key == WRITE_PAT_NAME and not include_write_pat:
            continue
        os.environ.setdefault(key, value)


def load_config(require_write_pat: bool = False, require_groq: bool = False) -> Config:
    global _write_pat_dropped_in_process

    def required(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise RuntimeError(f"missing required environment variable: {name}")
        return value

    def optional_int(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            raise RuntimeError(f"{name} must be an integer, got: {raw!r}")
        if value <= 0:
            raise RuntimeError(f"{name} must be a positive integer, got: {value}")
        return value

    if require_write_pat and _write_pat_dropped_in_process:
        raise RuntimeError(
            "GITHUB_WRITE_PAT is unavailable because an earlier load_config(require_write_pat=False) "
            "call in this same process already dropped it. load_config must not be called with "
            "require_write_pat=True and require_write_pat=False in the same process; "
            "run the write-capable entrypoint (the dashboard) in its own process."
        )

    _load_env_file(include_write_pat=require_write_pat)

    write_pat = os.environ.get(WRITE_PAT_NAME)
    if require_write_pat and not write_pat:
        raise RuntimeError("missing required environment variable: GITHUB_WRITE_PAT")

    if not require_write_pat:
        os.environ.pop(WRITE_PAT_NAME, None)
        _write_pat_dropped_in_process = True
        write_pat = None

    groq_api_key = required("GROQ_API_KEY") if require_groq else (os.environ.get("GROQ_API_KEY") or None)

    return Config(
        neon_dsn=required("NEON_DSN"),
        github_read_pat=required("GITHUB_READ_PAT"),
        github_write_pat=write_pat,
        groq_api_key=groq_api_key,
        logfire_token=os.environ.get("LOGFIRE_TOKEN") or None,
        pending_action_ttl_hours=optional_int("PENDING_ACTION_TTL_HOURS", 48),
        stuck_approving_recovery_minutes=optional_int("STUCK_APPROVING_RECOVERY_MINUTES", 10),
        comment_body_max_chars=optional_int("COMMENT_BODY_MAX_CHARS", 65536),
        mcp_client_label=os.environ.get("MCP_CLIENT_LABEL") or None,
        dashboard_access_token=os.environ.get("DASHBOARD_ACCESS_TOKEN") or None,
        dashboard_allow_insecure=os.environ.get("DASHBOARD_ALLOW_INSECURE", "").strip().lower() in ("1", "true", "yes"),
    )
