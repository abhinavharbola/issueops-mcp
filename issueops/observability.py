import sys
import threading

_lock = threading.Lock()
_configured = False


def configure_logfire(token: str | None, service_name: str) -> None:
    """Best-effort, once-per-process Logfire setup. A missing token or a
    broken install must never stop the MCP server, the triage agent, or the
    dashboard from running: tracing is observability, not one of the safety
    guarantees.

    Idempotent by design: dashboard/app.py re-runs top to bottom on every
    Streamlit interaction, so this must not re-configure or re-instrument
    on every click.

    console=False is required for the MCP server: stdio transport uses
    stdout for JSON-RPC framing, and Logfire's default console exporter
    writes span/log lines directly to stdout, which corrupts that stream.
    """
    global _configured
    if not token:
        return

    with _lock:
        if _configured:
            return
        _configured = True

    try:
        import logfire
    except ImportError:
        print(f"[{service_name}] logfire not installed, skipping tracing", file=sys.stderr)
        return

    try:
        logfire.configure(token=token, service_name=service_name, console=False)
    except Exception as exc:
        print(f"[{service_name}] logfire configuration failed, continuing without tracing: {exc}", file=sys.stderr)
        return

    try:
        logfire.instrument_requests()
    except Exception as exc:
        print(f"[{service_name}] logfire requests instrumentation unavailable, continuing: {exc}", file=sys.stderr)

    try:
        logfire.instrument_psycopg()
    except Exception as exc:
        print(f"[{service_name}] logfire psycopg instrumentation unavailable, continuing: {exc}", file=sys.stderr)