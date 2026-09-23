import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from dashboard import auth
from issueops import actions, tools
from issueops.config import load_config
from issueops.db import sync_connection
from issueops.github_client import GitHubReadClient, GitHubWriteClient
from issueops.heuristics import flag_matches
from issueops.observability import configure_logfire

PAGE_SIZE = 25

st.set_page_config(page_title="IssueOps MCP", layout="wide")

config = load_config(require_write_pat=True)
configure_logfire(config.logfire_token, service_name="issueops-dashboard")
read_client = GitHubReadClient(config.github_read_pat)
write_client = GitHubWriteClient(config.github_write_pat)


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
    if auth.token_matches(entered, expected):
        return
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
    elif result["status"] == "requeued":
        st.info(f"Returned to the pending list: {result}")
    elif result["status"] == "released":
        st.warning(
            f"GitHub could not be read, so nothing was sent. The action is back in the pending "
            f"list and can be approved again once GitHub is reachable: {result}"
        )
    elif result["status"] == "recording_failed":
        st.warning(
            f"The database write failed after retries. The GitHub call may have been applied: "
            f"check the issue on GitHub. The row stays in approving until recovery moves it to the "
            f"Needs review section, where a person records what actually happened: {result}"
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
    total_needs_review = actions.count_needs_review(conn)

page_count = max(1, math.ceil(total_pending / PAGE_SIZE))
# A stable, explicit key keeps the selected page across reruns. Without one, Streamlit
# derives the widget's identity partly from max_value, which changes every time an
# action is approved or rejected, so the selection would silently reset to page 1 on
# the very next rerun. Clamp any stored value before instantiating the widget, since
# max_value can shrink below a previously chosen page once actions are resolved, and
# Streamlit raises rather than clamping automatically.
if st.session_state.get("dashboard_page", 1) > page_count:
    st.session_state["dashboard_page"] = page_count
page = st.sidebar.number_input(
    "Page", min_value=1, max_value=page_count, value=1, step=1, key="dashboard_page"
)
st.sidebar.caption(f"{total_pending} pending action(s), {PAGE_SIZE} per page")
if total_needs_review:
    st.sidebar.warning(f"{total_needs_review} action(s) need review: outcome unknown")

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
                if excerpt.get("flag_matches_not_shown"):
                    st.error(
                        "These phrases appear in issue text that is NOT stored or shown here: "
                        + ", ".join(excerpt["flag_matches_not_shown"])
                        + ". Load the current issue and read it before acting."
                    )
                if excerpt.get("text_truncated"):
                    st.caption(
                        "Part of the title, body, or a comment was cut to fit storage. Load the current issue "
                        "to read the rest."
                    )
                if not str(row["requested_by"]).startswith("agent:"):
                    st.caption(
                        "This came from an MCP client, which may have read more of the issue than is stored here."
                    )
                st.write("Issue text stored with this proposal")
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
            live_flag_matches = []
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
                    # heuristic_flagged is decided once, at proposal time, and stored on
                    # the row. The issue can be edited afterward to add injection content,
                    # and the button above fetches that live text, so re-run the same
                    # check against it rather than trusting the stale, stored flag alone.
                    live_flag_matches = flag_matches(tools.issue_plaintext(preview))
                    if live_flag_matches:
                        st.error(
                            "The current issue text (fetched just now) contains phrases that look "
                            "like prompt injection: " + ", ".join(live_flag_matches)
                        )

            acknowledged = True
            if row["heuristic_flagged"] or live_flag_matches:
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


def _resolve(action_id, applied):
    try:
        with sync_connection(config.neon_dsn) as conn:
            result = actions.resolve_needs_review(conn, action_id, approver, applied)
    except Exception as exc:
        result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    st.session_state["last_action_result"] = result
    st.rerun()


with sync_connection(config.neon_dsn) as conn:
    review_rows = actions.list_needs_review(conn)

if review_rows:
    st.divider()
    st.subheader("Needs review: outcome unknown")
    st.caption(
        "An approval started its GitHub call and never recorded the result. It may or may not have been applied. "
        "Open the issue on GitHub, check, and record what you found."
    )
    for row in review_rows:
        header = f"{row['tool_name']} on {row['repo']}#{row['issue_number']}"
        with st.expander(header, expanded=True):
            st.json(row["arguments"])
            st.caption(
                f"Approval started by {row['claimed_by']} at {row['claimed_at']}; "
                f"GitHub call started at {row['execution_started_at']}"
            )
            col1, col2 = st.columns(2)
            with col1:
                if st.button("It was applied on GitHub", key=f"applied_{row['id']}", disabled=not approver):
                    _resolve(row["id"], True)
            with col2:
                if st.button("It was not applied, return to pending", key=f"notapplied_{row['id']}", disabled=not approver):
                    _resolve(row["id"], False)

st.divider()
st.subheader("Recent audit log")
with sync_connection(config.neon_dsn) as conn:
    audit_rows = actions.list_recent_audit_log(conn)
st.dataframe(audit_rows, use_container_width=True)


