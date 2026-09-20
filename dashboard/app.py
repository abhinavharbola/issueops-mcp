import math
import os
import secrets
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from issueops import actions, tools
from issueops.config import load_config
from issueops.db import sync_connection
from issueops.github_client import GitHubReadClient, GitHubWriteClient
from issueops.observability import configure_logfire

PAGE_SIZE = 25
MAX_TOKEN_FAILURES = 5
TOKEN_FAILURE_WINDOW_SECONDS = 60

st.set_page_config(page_title="IssueOps MCP", layout="wide")

config = load_config(require_write_pat=True)
configure_logfire(config.logfire_token, service_name="issueops-dashboard")
read_client = GitHubReadClient(config.github_read_pat)
write_client = GitHubWriteClient(config.github_write_pat)


@st.cache_resource
def _token_failures():
    return []


def _recent_token_failures() -> int:
    cutoff = time.time() - TOKEN_FAILURE_WINDOW_SECONDS
    failures = _token_failures()
    failures[:] = [t for t in failures if t > cutoff]
    return len(failures)


if config.dashboard_access_token:
    entered_token = st.sidebar.text_input("Access token", type="password", key="dashboard_token")
    if _recent_token_failures() >= MAX_TOKEN_FAILURES:
        st.sidebar.error("Too many failed attempts. Wait a minute and try again.")
        st.stop()
    token_ok = bool(entered_token) and secrets.compare_digest(
        entered_token.encode("utf-8"), config.dashboard_access_token.encode("utf-8")
    )
    if not token_ok:
        if entered_token:
            _token_failures().append(time.time())
        st.sidebar.error("Enter the correct access token to view or act on pending actions.")
        st.stop()
else:
    st.sidebar.warning(
        "DASHBOARD_ACCESS_TOKEN is not set. Anyone who can reach this app can approve or reject "
        "actions. Set DASHBOARD_ACCESS_TOKEN in .env before running this anywhere beyond a "
        "trusted local machine."
    )

st.sidebar.header("Approver")
approver = st.sidebar.text_input("Your name or handle", key="approver_name")
st.sidebar.caption("Not authentication by itself. Combine with DASHBOARD_ACCESS_TOKEN for real access control.")

st.title("IssueOps MCP - Pending Actions")

if "last_action_result" in st.session_state:
    result = st.session_state.pop("last_action_result")
    if result["status"] == "executed":
        st.success(f"Executed: {result}")
    elif result["status"] == "rejected":
        st.info(f"Rejected: {result}")
    elif result["status"] == "lost_lease_after_execution":
        st.warning(
            f"The GitHub call for this action went through, but its lease was lost before its "
            f"outcome could be recorded. Check the issue on GitHub and the audit log directly "
            f"for a possible duplicate: {result}"
        )
    elif result.get("outcome_unknown"):
        st.warning(
            f"The connection failed after the request may have reached GitHub. Check the issue on "
            f"GitHub before proposing this action again: {result}"
        )
    else:
        st.error(f"Not executed: {result}")

with sync_connection(config.neon_dsn) as conn:
    actions.expire_stale_pending(conn, ttl_hours=config.pending_action_ttl_hours)
    actions.recover_stuck_approving(conn, minutes=config.stuck_approving_recovery_minutes)
    total_pending = actions.count_pending_actions(conn)

page_count = max(1, math.ceil(total_pending / PAGE_SIZE))
page = st.sidebar.number_input("Page", min_value=1, max_value=page_count, value=1, step=1)
st.sidebar.caption(f"{total_pending} pending action(s), {PAGE_SIZE} per page")

with sync_connection(config.neon_dsn) as conn:
    pending = actions.list_pending_actions(conn, limit=PAGE_SIZE, offset=(int(page) - 1) * PAGE_SIZE)

if not pending:
    st.info("No pending actions.")
else:
    for row in pending:
        preview_key = f"preview_{row['id']}"
        header = f"{row['tool_name']} on {row['repo']}#{row['issue_number']}"
        if row["heuristic_flagged"]:
            header += "  [heuristic flag: advisory only, not a security boundary]"

        with st.expander(header, expanded=preview_key in st.session_state):
            st.write("Arguments")
            st.json(row["arguments"])

            st.write("Snapshot at proposal time")
            st.json(row["issue_state_snapshot"])

            st.caption(f"Proposed by {row['requested_by']} at {row['created_at']}")

            if st.button("Load source issue", key=f"load_{row['id']}"):
                try:
                    st.session_state[preview_key] = tools.get_issue(
                        config.neon_dsn, read_client, row["repo"], row["issue_number"],
                        "dashboard:preview", include_comments=False,
                    )
                except Exception as exc:
                    st.session_state[preview_key] = {"error": str(exc)}

            preview = st.session_state.get(preview_key)
            if preview is not None:
                if "error" in preview:
                    st.warning(f"could not fetch source issue: {preview['error']}")
                else:
                    st.write("Source issue")
                    st.text(preview.get("title", ""))
                    st.text(preview.get("body") or "(no body)")

            col1, col2 = st.columns(2)
            with col1:
                if st.button("Approve", key=f"approve_{row['id']}", disabled=not approver):
                    with sync_connection(config.neon_dsn) as conn:
                        result = actions.approve_action(
                            conn, read_client, write_client, row["id"], approver,
                            ttl_hours=config.pending_action_ttl_hours,
                        )
                    st.session_state.pop(preview_key, None)
                    st.session_state["last_action_result"] = result
                    st.rerun()
            with col2:
                if st.button("Reject", key=f"reject_{row['id']}", disabled=not approver):
                    with sync_connection(config.neon_dsn) as conn:
                        result = actions.reject_action(conn, row["id"], approver)
                    st.session_state.pop(preview_key, None)
                    st.session_state["last_action_result"] = result
                    st.rerun()

    if not approver:
        st.warning("Enter your name in the sidebar to enable approve/reject.")

st.divider()
st.subheader("Recent audit log")
with sync_connection(config.neon_dsn) as conn:
    audit_rows = actions.list_recent_audit_log(conn)
st.dataframe(audit_rows, use_container_width=True)
