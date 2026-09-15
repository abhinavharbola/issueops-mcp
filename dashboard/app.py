import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

import actions
from issueops import tools
from issueops.config import load_config
from issueops.db import sync_connection
from issueops.github_client import GitHubReadClient, GitHubWriteClient
from issueops.observability import configure_logfire

st.set_page_config(page_title="IssueOps MCP", layout="wide")

config = load_config(require_write_pat=True)
configure_logfire(config.logfire_token, service_name="issueops-dashboard")
read_client = GitHubReadClient(config.github_read_pat)
write_client = GitHubWriteClient(config.github_write_pat)


@st.cache_data(ttl=60, show_spinner=False)
def _fetch_issue_preview(neon_dsn, repo, issue_number):
    return tools.get_issue(neon_dsn, read_client, repo, issue_number, "dashboard:preview")

st.sidebar.header("Approver")
approver = st.sidebar.text_input("Your name or handle", key="approver_name")
st.sidebar.caption("Not authentication. Anyone with dashboard access can type any name here.")

st.title("IssueOps MCP - Pending Actions")

if "last_action_result" in st.session_state:
    result = st.session_state.pop("last_action_result")
    if result["status"] == "executed":
        st.success(f"Executed: {result}")
    elif result["status"] == "rejected":
        st.info(f"Rejected: {result}")
    else:
        st.error(f"Not executed: {result}")

with sync_connection(config.neon_dsn) as conn:
    actions.expire_stale_pending(conn)
    pending = actions.list_pending_actions(conn)

if not pending:
    st.info("No pending actions.")
else:
    for row in pending:
        header = f"{row['tool_name']} on {row['repo']}#{row['issue_number']}"
        if row["heuristic_flagged"]:
            header += "  [heuristic flag: advisory only, not a security boundary]"

        with st.expander(header, expanded=False):
            st.write("Arguments")
            st.json(row["arguments"])

            st.write("Snapshot at proposal time")
            st.json(row["issue_state_snapshot"])

            st.caption(f"Proposed by {row['requested_by']} at {row['created_at']}")

            try:
                issue = _fetch_issue_preview(config.neon_dsn, row["repo"], row["issue_number"])
                st.write("Source issue")
                st.markdown(f"**{issue['title']}**")
                st.write(issue["body"] or "(no body)")
            except Exception as exc:
                st.warning(f"could not fetch source issue: {exc}")

            col1, col2 = st.columns(2)
            with col1:
                if st.button("Approve", key=f"approve_{row['id']}", disabled=not approver):
                    with sync_connection(config.neon_dsn) as conn:
                        result = actions.approve_action(conn, read_client, write_client, row["id"], approver)
                    st.session_state["last_action_result"] = result
                    st.rerun()
            with col2:
                if st.button("Reject", key=f"reject_{row['id']}", disabled=not approver):
                    with sync_connection(config.neon_dsn) as conn:
                        result = actions.reject_action(conn, row["id"], approver)
                    st.session_state["last_action_result"] = result
                    st.rerun()

    if not approver:
        st.warning("Enter your name in the sidebar to enable approve/reject.")

st.divider()
st.subheader("Recent audit log")
with sync_connection(config.neon_dsn) as conn:
    audit_rows = actions.list_recent_audit_log(conn)
st.dataframe(audit_rows, use_container_width=True)
