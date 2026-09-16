import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    neon_dsn: str
    github_read_pat: str
    github_write_pat: str | None
    groq_api_key: str
    groq_api_key_fallback: str | None
    logfire_token: str | None
    pending_action_ttl_hours: int = 48
    comment_body_max_chars: int = 65536


def load_config(require_write_pat: bool = False) -> Config:
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

    write_pat = os.environ.get("GITHUB_WRITE_PAT")
    if require_write_pat and not write_pat:
        raise RuntimeError("missing required environment variable: GITHUB_WRITE_PAT")

    if not require_write_pat:
        # This process has no business holding the write PAT at all. Drop it
        # from the process environment rather than just declining to read it,
        # so the credential-level guarantee in PRD Section 6.3 holds for local
        # runs too, not only for the GitHub Actions case where the secret is
        # never injected in the first place.
        os.environ.pop("GITHUB_WRITE_PAT", None)
        write_pat = None

    return Config(
        neon_dsn=required("NEON_DSN"),
        github_read_pat=required("GITHUB_READ_PAT"),
        github_write_pat=write_pat,
        groq_api_key=required("GROQ_API_KEY"),
        groq_api_key_fallback=os.environ.get("GROQ_API_KEY_FALLBACK") or None,
        # Optional: tracing is a nice-to-have, not a safety guarantee. Nothing
        # in this project should fail to start because observability isn't
        # configured yet. See issueops/observability.py.
        logfire_token=os.environ.get("LOGFIRE_TOKEN") or None,
        pending_action_ttl_hours=optional_int("PENDING_ACTION_TTL_HOURS", 48),
        comment_body_max_chars=optional_int("COMMENT_BODY_MAX_CHARS", 65536),
    )



