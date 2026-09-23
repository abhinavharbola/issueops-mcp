import base64
import math
import os
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from dashboard import auth
from issueops import actions, tools
from issueops.config import load_config
from issueops.github_client import GitHubReadClient, GitHubWriteClient
from issueops.heuristics import flag_matches
from issueops.observability import configure_logfire

PAGE_SIZE = 25
ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
FONT_DIR = ASSETS_DIR / "fonts"

st.set_page_config(
    page_title="IssueOps",
    page_icon=str(ASSETS_DIR / "icon.png"),
    layout="wide",
)


def _font_data_uri(filename: str) -> str:
    # Self-hosted, embedded as a data: URI. Google Fonts' CDN is unreachable
    # from this deployment, so an @import silently fails and every heading
    # falls back to plain system Georgia. Shipping the actual woff2 bytes
    # with the page removes the network dependency entirely.
    data = (FONT_DIR / filename).read_bytes()
    return "data:font/woff2;base64," + base64.b64encode(data).decode("ascii")


def _inject_theme():
    # Presentation only. No functional logic lives here. Keeping this call
    # separate and near the top makes it obvious it can be deleted without
    # touching behavior.
    font_600 = _font_data_uri("fraunces-600.woff2")
    font_700 = _font_data_uri("fraunces-700.woff2")
    font_face_css = f"""
        @font-face {{
            font-family: 'Fraunces';
            font-weight: 600;
            font-style: normal;
            font-display: swap;
            src: url({font_600}) format('woff2');
        }}
        @font-face {{
            font-family: 'Fraunces';
            font-weight: 700;
            font-style: normal;
            font-display: swap;
            src: url({font_700}) format('woff2');
        }}
        """
    st.markdown(
        "<style>" + font_face_css + """
        html, body, [class*="css"] {
            font-family: 'Source Sans', -apple-system, "Segoe UI", sans-serif;
        }

        :root {
            --color-rosewood: #917778;
            --color-rosewood-deep: #6E5657;
            --color-rosewood-deeper: #55403F;
            --color-taupe: #F6F3EE;
            --color-bg-card: #FFFFFF;
            --color-bg-quiet: #EFEAE3;
            --color-border: #E1D9D0;
            --color-text: #2B211F;
            --color-text-muted: #7A6D68;
            --color-on-rosewood: #FBF6F2;
        }

        .stApp { background-color: var(--color-taupe); }

        h1, h2, h3 {
            font-family: 'Fraunces', Georgia, serif !important;
            color: var(--color-rosewood-deep);
            font-weight: 700 !important;
            letter-spacing: -0.01em;
        }

        h2, h3 {
            font-weight: 600 !important;
        }

        [data-testid="stCaptionContainer"] p {
            color: var(--color-text-muted);
        }

        section[data-testid="stSidebar"] {
            background-color: var(--color-rosewood-deep);
        }

        section[data-testid="stSidebar"] * {
            color: var(--color-on-rosewood);
        }

        section[data-testid="stSidebar"] h1,
        section[data-testid="stSidebar"] h2,
        section[data-testid="stSidebar"] h3 {
            color: #FFFFFF;
        }

        .sidebar-brand {
            font-family: 'Fraunces', Georgia, serif;
            font-size: 1.4rem;
            font-weight: 700;
            color: #FFFFFF;
            margin-bottom: 0;
        }

        .sidebar-brand-sub {
            font-size: 0.8rem;
            color: #E7D9D6;
            margin-top: -0.2rem;
            margin-bottom: 1rem;
            letter-spacing: 0.02em;
            text-transform: uppercase;
        }

        /* Sidebar form controls. The dark rosewood sidebar background bleeds
           through unless every layer of the widget (wrapper, input, the
           number-input step buttons) is repainted explicitly. */
        section[data-testid="stSidebar"] div[data-testid="stTextInput"],
        section[data-testid="stSidebar"] div[data-testid="stNumberInput"] {
            background-color: transparent;
        }

        section[data-testid="stSidebar"] div[data-testid="stTextInput"] input,
        section[data-testid="stSidebar"] div[data-testid="stNumberInput"] input {
            background-color: #FFFFFF !important;
            border: 1px solid rgba(255, 255, 255, 0.3) !important;
            border-radius: 6px !important;
            color: var(--color-text) !important;
        }

        section[data-testid="stSidebar"] div[data-testid="stNumberInput"] button {
            background-color: #FFFFFF !important;
            border: 1px solid rgba(255, 255, 255, 0.3) !important;
        }

        section[data-testid="stSidebar"] div[data-testid="stNumberInput"] button svg {
            fill: var(--color-rosewood-deep) !important;
        }

        section[data-testid="stSidebar"] hr { border-color: rgba(255, 255, 255, 0.22); }

        div[data-testid="stExpander"] {
            border: 1px solid var(--color-border);
            border-radius: 10px;
            background-color: var(--color-bg-card);
            box-shadow: 0 1px 2px rgba(43, 33, 31, 0.05);
            margin-bottom: 0.6rem;
        }

        div[data-testid="stExpander"] summary {
            font-weight: 500;
            color: var(--color-text);
            padding: 0.35rem 0.1rem;
        }

        .stButton button {
            border-radius: 6px;
            font-weight: 500;
            border: 1px solid var(--color-border);
            color: var(--color-rosewood-deep);
        }

        .stButton button[kind="primary"] {
            background-color: var(--color-rosewood-deep);
            border-color: var(--color-rosewood-deep);
            color: #FFFFFF;
        }

        .stButton button[kind="primary"]:hover {
            background-color: var(--color-rosewood-deeper);
            border-color: var(--color-rosewood-deeper);
        }

        div[data-testid="stMetric"] {
            background-color: var(--color-bg-quiet);
            border: 1px solid var(--color-border);
            border-radius: 10px;
            padding: 0.75rem 1rem;
        }

        div[data-testid="stMetricValue"] {
            color: var(--color-rosewood-deep);
            font-weight: 600;
        }

        /* Alert boxes (st.error/warning/success/info). Streamlit paints the
           semantic color on an inner notification element, so overriding
           only the outer stAlert div left the old color showing through
           underneath. Flatten every descendant to transparent first, then
           repaint the whole stack in one flat, quiet color. */
        div[data-testid="stAlert"] * {
            background-color: transparent !important;
            background-image: none !important;
        }

        div[data-testid="stAlert"] {
            background-color: var(--color-bg-quiet) !important;
            border: 1px solid var(--color-border) !important;
            border-radius: 8px !important;
        }

        div[data-testid="stAlert"] p,
        div[data-testid="stAlert"] span {
            color: var(--color-text) !important;
        }

        div[data-testid="stAlert"] svg {
            fill: var(--color-rosewood-deep) !important;
        }

        div[data-testid="stDataFrame"] {
            border: 1px solid var(--color-border);
            border-radius: 8px;
        }

        hr { border-color: var(--color-border); }
        </style>
        """,
        unsafe_allow_html=True,
    )


_inject_theme()

config = load_config(require_write_pat=True)
configure_logfire(config.logfire_token, service_name="issueops-dashboard")
read_client = GitHubReadClient(config.github_read_pat)
write_client = GitHubWriteClient(config.github_write_pat)


@st.cache_resource
def _get_pool(dsn: str) -> ConnectionPool:
    # st.cache_resource keeps this pool alive across reruns and across every
    # session served by this process, instead of a fresh psycopg.connect(...)
    # (a full TCP+TLS+auth handshake to Neon) on every button click, checkbox
    # toggle, or expander open. min_size keeps a connection warm at all times;
    # max_size bounds how many concurrent checkouts this dashboard can hold.
    return ConnectionPool(
        dsn,
        min_size=1,
        max_size=5,
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=True,
    )


@contextmanager
def _connection():
    with _get_pool(config.neon_dsn).connection() as conn:
        yield conn


def _enforce_access():
    expected = config.dashboard_access_token
    if not expected:
        if not config.dashboard_allow_insecure:
            st.error(
                "DASHBOARD_ACCESS_TOKEN is not set, so the dashboard refuses to start. It can approve "
                "writes to GitHub, and without a token anyone who can reach it could approve them. "
                "Set DASHBOARD_ACCESS_TOKEN, or set DASHBOARD_ALLOW_INSECURE=true to run without "
                "authentication on a machine only you can reach.",
                icon=":material/error:",
            )
            st.stop()
        return True

    entered = st.sidebar.text_input("Access token", type="password", key="dashboard_token")
    if auth.token_matches(entered, expected):
        return False
    st.sidebar.error("Enter the correct access token to view or act on pending actions.")
    st.stop()


st.sidebar.markdown('<p class="sidebar-brand">IssueOps</p>', unsafe_allow_html=True)
st.sidebar.markdown('<p class="sidebar-brand-sub">Review console</p>', unsafe_allow_html=True)

insecure_mode = _enforce_access()

st.sidebar.divider()
st.sidebar.header("Approver")
approver = st.sidebar.text_input("Your name or handle", key="approver_name").strip()
st.sidebar.divider()

st.title("IssueOps")
st.caption("Human-in-the-loop review for proposed GitHub issue actions.")

if insecure_mode:
    st.warning(
        "Running without authentication because DASHBOARD_ALLOW_INSECURE is set. Anyone who can "
        "reach this app can approve or reject actions.",
        icon=":material/lock_open:",
    )

if "last_action_result" in st.session_state:
    result = st.session_state.pop("last_action_result")
    if result["status"] == "executed":
        st.success(f"Executed: {result}", icon=":material/check_circle:")
    elif result["status"] == "rejected":
        st.info(f"Rejected: {result}", icon=":material/block:")
    elif result["status"] == "requeued":
        st.info(f"Returned to the pending list: {result}", icon=":material/undo:")
    elif result["status"] == "released":
        st.warning(
            f"GitHub could not be read, so nothing was sent. The action is back in the pending "
            f"list and can be approved again once GitHub is reachable: {result}",
            icon=":material/warning:",
        )
    elif result["status"] == "recording_failed":
        st.warning(
            f"The database write failed after retries. The GitHub call may have been applied: "
            f"check the issue on GitHub. The row stays in approving until recovery moves it to the "
            f"Needs review section, where a person records what actually happened: {result}",
            icon=":material/warning:",
        )
    elif result["status"] == "error":
        st.error(f"The action could not be processed: {result}", icon=":material/error:")
    elif result["status"] == "lost_lease_after_execution":
        st.warning(
            f"The GitHub call for this action went through, but its lease was lost before its "
            f"outcome could be recorded. Check the issue on GitHub and the audit log directly "
            f"for a possible duplicate: {result}",
            icon=":material/warning:",
        )
    elif result.get("outcome_unknown"):
        st.warning(
            f"The connection failed after the request may have reached GitHub. Check the issue on "
            f"GitHub before proposing this action again: {result}",
            icon=":material/warning:",
        )
    else:
        st.error(f"Not executed: {result}", icon=":material/error:")

with _connection() as conn:
    actions.expire_stale_pending(conn, ttl_hours=config.pending_action_ttl_hours)
    actions.recover_stuck_approving(conn, minutes=config.stuck_approving_recovery_minutes)
    total_pending = actions.count_pending_actions(conn)
    total_needs_review = actions.count_needs_review(conn)

metric_cols = st.columns(3)
metric_cols[0].metric("Pending actions", total_pending)
metric_cols[1].metric("Needs review", total_needs_review)
metric_cols[2].metric("Per page", PAGE_SIZE)

page_count = max(1, math.ceil(total_pending / PAGE_SIZE))
# A stable, explicit key keeps the selected page across reruns. Without one, Streamlit
# derives the widget's identity partly from max_value, which changes every time an
# action is approved or rejected, so the selection would silently reset to page 1 on
# the very next rerun. Clamp any stored value before instantiating the widget, since
# max_value can shrink below a previously chosen page once actions are resolved, and
# Streamlit raises rather than clamping automatically.
if st.session_state.get("dashboard_page", 1) > page_count:
    st.session_state["dashboard_page"] = page_count
st.sidebar.subheader("Queue")
page = st.sidebar.number_input(
    "Page", min_value=1, max_value=page_count, value=1, step=1, key="dashboard_page"
)
st.sidebar.caption(f"{total_pending} pending action(s), {PAGE_SIZE} per page")
if total_needs_review:
    st.sidebar.warning(f"{total_needs_review} action(s) need review: outcome unknown")

with _connection() as conn:
    pending = actions.list_pending_actions(conn, limit=PAGE_SIZE, offset=(int(page) - 1) * PAGE_SIZE)

st.divider()
st.subheader("Pending actions")


@st.fragment
def _render_pending_row(row):
    # Scoped to just this row. Expanding it, clicking "Load current issue", and
    # ticking the acknowledgement checkbox only rerun this fragment, not the whole
    # page, so they no longer re-run the DB queries above or re-render every other
    # row. Approve and Reject still call plain st.rerun(), whose default scope is
    # "app" even from inside a fragment, so those two correctly force a full page
    # rerun -- they change the pending list, the counts, and the audit log below,
    # all of which live outside this fragment.
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
                    + ", ".join(excerpt["flag_matches"]),
                    icon=":material/warning:",
                )
            if excerpt.get("flag_matches_not_shown"):
                st.error(
                    "These phrases appear in issue text that is NOT stored or shown here: "
                    + ", ".join(excerpt["flag_matches_not_shown"])
                    + ". Load the current issue and read it before acting.",
                    icon=":material/error:",
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
                st.warning(f"could not fetch source issue: {preview['error']}", icon=":material/warning:")
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
                        "like prompt injection: " + ", ".join(live_flag_matches),
                        icon=":material/error:",
                    )

        acknowledged = True
        if row["heuristic_flagged"] or live_flag_matches:
            acknowledged = st.checkbox(
                "I read the flagged issue text and still want to act on this",
                key=f"ack_{row['id']}",
            )

        col1, col2 = st.columns(2)
        with col1:
            if st.button(
                "Approve",
                key=f"approve_{row['id']}",
                disabled=not (approver and acknowledged),
                type="primary",
                icon=":material/check:",
            ):
                try:
                    with _connection() as conn:
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
            if st.button(
                "Reject",
                key=f"reject_{row['id']}",
                disabled=not approver,
                icon=":material/close:",
            ):
                try:
                    with _connection() as conn:
                        result = actions.reject_action(conn, row["id"], approver)
                except Exception as exc:
                    result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
                st.session_state.pop(preview_key, None)
                st.session_state["last_action_result"] = result
                st.rerun()


if not pending:
    st.info("No pending actions.", icon=":material/inbox:")
else:
    for row in pending:
        _render_pending_row(row)

    if not approver:
        st.warning("Enter your name in the sidebar to enable approve/reject.", icon=":material/warning:")


def _resolve(action_id, applied):
    try:
        with _connection() as conn:
            result = actions.resolve_needs_review(conn, action_id, approver, applied)
    except Exception as exc:
        result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    st.session_state["last_action_result"] = result
    st.rerun()


with _connection() as conn:
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
                if st.button(
                    "It was applied on GitHub",
                    key=f"applied_{row['id']}",
                    disabled=not approver,
                    icon=":material/check_circle:",
                ):
                    _resolve(row["id"], True)
            with col2:
                if st.button(
                    "It was not applied, return to pending",
                    key=f"notapplied_{row['id']}",
                    disabled=not approver,
                    icon=":material/undo:",
                ):
                    _resolve(row["id"], False)

st.divider()
st.subheader("Recent audit log")
with _connection() as conn:
    audit_rows = actions.list_recent_audit_log(conn)
# The id column can come back as a uuid.UUID or bytes object depending on the
# column type and driver, and st.dataframe's Arrow conversion renders those
# as a raw {"0": 209, "1": 254, ...} byte map instead of readable text.
# Stringifying here is presentation-only; the underlying rows are untouched.
audit_rows = [{**row, "id": str(row["id"])} for row in audit_rows]
st.dataframe(audit_rows, width="stretch")
