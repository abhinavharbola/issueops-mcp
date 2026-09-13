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
    logfire_token: str
    pending_action_ttl_hours: int = 48
    comment_body_max_chars: int = 65536


def load_config(require_write_pat: bool = False) -> Config:
    def required(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise RuntimeError(f"missing required environment variable: {name}")
        return value

    write_pat = os.environ.get("GITHUB_WRITE_PAT")
    if require_write_pat and not write_pat:
        raise RuntimeError("missing required environment variable: GITHUB_WRITE_PAT")

    return Config(
        neon_dsn=required("NEON_DSN"),
        github_read_pat=required("GITHUB_READ_PAT"),
        github_write_pat=write_pat,
        groq_api_key=required("GROQ_API_KEY"),
        groq_api_key_fallback=os.environ.get("GROQ_API_KEY_FALLBACK") or None,
        logfire_token=required("LOGFIRE_TOKEN"),
    )
