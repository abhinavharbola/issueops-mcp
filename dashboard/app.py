import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from dashboard import auth
from issueops import actions, tools
from issueops.config import load_config
from issueops.db import sync_connection
from issueops.github_client import GitHubReadClient, GitHubWriteClient
from issueops.observability import configure_logfire

PAGE_SIZE = 25

st.set_page_config(page_title="IssueOps MCP", layout="wide")

config = load_config(require_write_pat=True)
configure_logfire(config.logfire_token, service_name="issueops-dashboard")
read_client = GitHubReadClient(config.github_read_pat)
write_client = GitHubWriteClient(config.github_write_pat)


@st.cache_resource
def _shared_failures():
    return []


def _enforce_access():
    expected = config.dashboard_access_token
    if not expected:
        if not config.dashboard_allow_insecure:
            st.error(
                "DASHBOARD_ACCESS_TOKEN is not set, so the dashboard refuses to start. It can approve "
                "writes to GitHub, and without a token anyone who can reach it could approve them. "
                "Set DASHBOARD_ACCESS_TOKEN, or set DASHBOARD_ALLOW_INSECURE=true to run without "
                "authentication on a machine only you can reach."
            )
            st.stop()
        st.sidebar.warning(
            "Running without authentication because DASHBOARD_ALLOW_INSECURE is set. Anyone who can "
            "reach this app can approve or reject actions."
        )
        return

    entered = st.sidebar.text_input("Access token", type="password", key="dashboard_token")
    now = time.time()
    session_failures = auth.recent(st.session_state.get("token_failures", []), now)
    st.session_state["token_failures"] = session_failures
    if auth.is_locked(session_failures, now):
        st.sidebar.error("Too many failed attempts from this session. Wait a minute and try again.")
        st.stop()
    if auth.token_matches(entered, expected):
        return
    if entered:
        session_failures.append(now)
        shared = _shared_failures()
        shared[:] = auth.recent(shared, now)
        shared.append(now)
        time.sleep(auth.failure_delay_seconds(len(shared)))
    st.sidebar.error("Enter the correct access token to view or act on pending actions.")
    st.stop()


_enforce_access()

st.sidebar.header("Approver")
approver = st.sidebar.text_input("Your name or handle", key="approver_name").strip()
st.sidebar.caption("Not authentication by itself. Combine with DASHBOARD_ACCESS_TOKEN for real access control.")

st.title("IssueOps MCP - Pending Actions")

if "last_action_result" in st.session_state:
    result = st.session_state.pop("last_action_result")
    if result["status"] == "executed":
        st.success(f"Executed: {result}")
    elif result["status"] == "rejected":
        st.info(f"Rejected: {result}")
    elif result["status"] == "released":
        st.warning(
            f"GitHub could not be read, so nothing was sent. The action is back in the pending "
            f"list and can be approved again once GitHub is reachable: {result}"
        )
    elif result["status"] == "recording_failed":
        st.warning(
            f"The database write failed after retries. The GitHub call may have been applied: "
            f"check the issue on GitHub before approving again. The row stays in approving until "
            f"recovery returns it to pending: {result}"
        )
    elif result["status"] == "error":
        st.error(f"The action could not be processed: {result}")
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

            if row.get("rationale"):
                st.write("Why the proposer suggested this")
                st.text(row["rationale"])

            excerpt = row.get("source_excerpt")
            if excerpt:
                if excerpt.get("flag_matches"):
                    st.warning(
                        "The issue text contains phrases that look like prompt injection: "
                        + ", ".join(excerpt["flag_matches"])
                    )
                st.write("Issue text the proposer saw")
                st.text(excerpt.get("title") or "(no title)")
                st.text(excerpt.get("body") or "(no body)")
                if excerpt.get("comments_omitted"):
                    st.caption(f"{excerpt['comments_omitted']} earlier comment(s) not shown")
                if excerpt.get("comments_truncated_by_fetcher"):
                    st.caption("The comment list was cut off at the fetch limit")
                for comment in excerpt.get("comments", []):
                    st.text(f"comment by {comment['author']}: {comment['body']}")
            else:
                st.caption("No issue text was stored with this proposal. Use the live view below.")

            st.write("Snapshot at proposal time")
            st.json(row["issue_state_snapshot"])

            st.caption(f"Proposed by {row['requested_by']} at {row['created_at']}")

            if st.button("Load current issue from GitHub", key=f"load_{row['id']}"):
                try:
                    st.session_state[preview_key] = tools.get_issue(
                        config.neon_dsn, read_client, row["repo"], row["issue_number"],
                        "dashboard:preview", include_comments=True,
                    )
                except Exception as exc:
                    st.session_state[preview_key] = {"error": str(exc)}

            preview = st.session_state.get(preview_key)
            if preview is not None:
                if "error" in preview:
                    st.warning(f"could not fetch source issue: {preview['error']}")
                else:
                    st.write("Current issue")
                    st.text(preview.get("title", ""))
                    st.text(preview.get("body") or "(no body)")
                    for comment in (preview.get("comments_detail") or [])[-10:]:
                        author = (comment.get("user") or {}).get("login") or "unknown"
                        st.text(f"comment by {author}: {comment.get('body') or ''}")

            acknowledged = True
            if row["heuristic_flagged"]:
                acknowledged = st.checkbox(
                    "I read the flagged issue text and still want to act on this",
                    key=f"ack_{row['id']}",
                )

            col1, col2 = st.columns(2)
            with col1:
                if st.button("Approve", key=f"approve_{row['id']}", disabled=not (approver and acknowledged)):
                    try:
                        with sync_connection(config.neon_dsn) as conn:
                            result = actions.approve_action(
                                conn, read_client, write_client, row["id"], approver,
                                ttl_hours=config.pending_action_ttl_hours, dsn=config.neon_dsn,
                            )
                    except Exception as exc:
                        result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
                    st.session_state.pop(preview_key, None)
                    st.session_state["last_action_result"] = result
                    st.rerun()
            with col2:
                if st.button("Reject", key=f"reject_{row['id']}", disabled=not approver):
                    try:
                        with sync_connection(config.neon_dsn) as conn:
                            result = actions.reject_action(conn, row["id"], approver)
                    except Exception as exc:
                        result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
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
