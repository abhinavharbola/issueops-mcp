# IssueOps MCP

A human-in-the-loop GitHub issue triage system. An MCP client (Claude Desktop) or a scheduled triage agent can read issues and *propose* changes: comments, labels, assignees, closing. Nothing reaches GitHub until a person approves the proposal in a Streamlit dashboard, and every read, proposal, and decision is written to an audit log.

Built on free-tier infrastructure: a Neon Postgres database, Groq for the classifier, and fine-grained GitHub tokens.

## Preview

<p align="center">
  <img src="assets/dashboard_pending.png" width="720" alt="Streamlit dashboard showing a pending proposal expanded, with the model's rationale, the stored issue text, and the approve/reject controls">
  <br>
  <sub>A pending proposal expanded: rationale, stored issue text, and the approve/reject controls.</sub>
</p>

<p align="center">
  <img src="assets/dashboard_audit_log.png" width="720" alt="Streamlit dashboard audit log table showing executed, rejected, and stale actions">
  <br>
  <sub>The audit log at the bottom of the dashboard, one row per read, proposal, and decision.</sub>
</p>

> No screenshots are checked in yet, `assets/` currently only has fonts. Add PNGs at these two paths (or update the paths above) before this section renders.

## What this is

Given an allowlisted repository, the system:

1. Exposes 10 MCP tools: 5 read tools and 5 propose tools. The propose tools never call GitHub's write API.
2. Validates each proposal (allowlist, arguments, queue limits, duplicates), snapshots the issue, and queues it in Postgres as a `pending_actions` row.
3. Shows queued proposals in a dashboard, where a human reads the source issue and approves or rejects.
4. On approval, claims the row with a lease, re-fetches the issue, checks it hasn't changed in a way that matters, and only then writes to GitHub using a separate write token.
5. Records everything in `audit_log`, including proposals that were deduplicated, rejected as invalid, went stale, or failed.

A scheduled triage agent (`agent/triage.py`) uses the same propose path. It classifies open issues with an LLM and queues labels, comments, assignments, or closes for a human to review.

## Architecture

```mermaid
flowchart TD
    client(["MCP client or triage agent"]) --> read["read tools"]
    client --> propose["propose tools"]
    read --> ghr["GitHub API, read token"]

    propose --> validate["allowlist, validation,<br/>queue caps, dedup"]
    validate --> queue[("pending_actions<br/>Neon Postgres")]

    queue --> dash["Streamlit dashboard"]
    human(["human approver"]) --> dash
    dash -->|reject| rejected["status: rejected"]
    dash -->|approve| claim["claim row, lease token"]

    claim --> check{"stale?"}
    check -->|yes| stale["status: stale"]
    check -->|no| ghw["GitHub API, write token"]
    ghw --> done["status: executed or failed"]

    validate -.-> audit[("audit_log")]
    dash -.-> audit
    ghw -.-> audit
```

Design notes for each step (lease-based claiming, the stale check, crash recovery, transaction discipline) are covered in the [Guardrails](#guardrails) section below.

## Processes and credentials

Each process is started separately and loads only the credentials it needs, from a single `.env` file.

| Process | Entry point | Credentials | Writes to GitHub |
|---|---|---|---|
| MCP server | `python -m mcp_server.server` | read token | No |
| Triage agent | `python -m agent.triage` | read token, Groq key | No |
| Dashboard | `streamlit run dashboard/app.py` | read token, write token | Yes, only after a human approves |

`load_config(require_write_pat=False)` never reads the write token from `.env`, drops it if inherited from the environment, and raises if write mode is loaded later in the same process. Only the dashboard holds `GITHUB_WRITE_PAT`; only the triage agent and eval hold the Groq key.

## MCP tools

| Kind | Tool | What it does |
|---|---|---|
| Read | `list_issues` | List issue summaries by state, labels, and recency. At most `limit` items (default 50, maximum 100) with a `truncated` flag, body excerpts only, pull requests marked `is_pull_request` |
| Read | `get_issue` | One issue with its 30 newest comments. Body and comments are clipped, and the result says how many comments were omitted |
| Read | `list_pull_requests` | List pull request summaries. At most `limit` items (default 50, maximum 100) with a `truncated` flag |
| Read | `search_issues` | Search within one repo. `repo:`, `org:`, `user:`, `owner:` qualifiers are rejected and results from other repos are dropped. Results are summaries with body excerpts |
| Read | `get_repo_activity_summary` | Issues opened, issues closed, distinct issues with comments, and opened issues by label, over 1 to 365 days. On very busy repos the counts are returned as lower bounds with `truncated: true` instead of failing |
| Propose | `propose_add_comment` | Queue a comment |
| Propose | `propose_add_labels` | Queue label additions, checked against the repo's labels |
| Propose | `propose_remove_labels` | Queue label removals |
| Propose | `propose_assign` | Queue an assignee, checked against the repo's assignable users |
| Propose | `propose_close` | Queue closing, optionally as `completed` or `not_planned` |

## Triage agent

- **Model:** `openai/gpt-oss-20b` on Groq by default (`--model` to override). One call per issue, JSON mode with a fallback call on rejection, one retry on rate limits after `retry-after`.
- **Trusted context:** state, current labels/assignees, repo labels, and assignable users are passed as trusted; issue text is passed separately, delimited as untrusted.
- **Allowed proposals:** labels and assignments only by default. Comments need `--allow-comment`, closes need `--allow-close`. A flagged (likely-injected) issue only ever gets labels/assignments, flags or not.
- **Plan filtering:** proposals that would no-op (label already present, label not on repo, assignee already assigned/not assignable, close on a non-open issue) are dropped before queuing.
- **Memory:** issues with an existing proposal in `pending`, `approving`, `rejected`, `executed`, or `needs_review` are skipped on later runs; `expired`, `failed`, `stale` are re-evaluated. Every attempt is logged in `triage_attempts`. An issue with no proposal, or where every proposal deduplicated against anything already pending (agent's own or not), is skipped until its title/body changes.
- **Errors:** empty/unparseable model output counts as an error (retried up to 3x, prose-wrapped JSON is recovered when possible). Provider/GitHub rate limits, timeouts, and connection failures are transient and never blacklist an issue. Systemic failures (bad Groq key, missing model, GitHub 401/non-rate-limit 403, de-allowlisted repo, missing DB grant/table) stop the run immediately, log nothing for that issue, and exit 1, so a broken config can't quietly burn through `--max-issues`.
- **Rate limits / full queue:** the run stops rather than spending remaining issues on calls it can't complete; unrecorded issues are retried next run.
- **Rationale:** stored per proposal, shown to the approver.
- **Flags:** `--state`, `--max-issues`, `--since`, `--max-pages`, `--model`.
- **Listing:** paged, stops at `--max-issues` candidates or `--max-pages`, whichever comes first (a `--max-pages` cutoff just warns to stderr).
- **Prompt size:** 300 chars of title, 8000 of body, up to 10 comments (1500 chars each, 6000 combined cap), truncation noted inline.
- **Names:** repos lower-cased; labels/assignees matched case-insensitively, stored with GitHub's spelling.

## Guardrails

- **Human approval:** no propose tool can write to GitHub; only the dashboard holds the write token.
- **Repo allowlist:** every read/propose call checks `repo_allowlist.active` first. Deactivating a repo keeps history intact. A pending proposal on a repo deactivated after it was queued is marked `blocked` at approval time rather than executed.
- **Lease-based claiming:** approval flips a row `pending` → `approving` in a short transaction, returning a lease token that every later write is conditioned on. No DB lock is held across a GitHub call, so two approvers can't both execute the same action.
- **Scoped stale check:** approval is refused if the title/body changed, a close targets a non-open issue, a label to remove is already gone, or an identical comment exists. Unrelated proposals aren't blocked.
- **Proposal-time checks:** propose tools reject actions that can only go stale (closing a non-open issue, removing an absent label, adding a present label, assigning an already-assigned user).
- **Transient failures:** an unreadable GitHub during approval releases the claim back to `pending` for retry; only a 404/410 fails permanently. A post-write recording failure retries on a fresh connection, then reports `recording_failed` instead of throwing.
- **Outcome checks:** assign responses are verified against GitHub; a network failure during a write reports `outcome_unknown` so the approver checks manually.
- **Queue limits:** `MAX_PENDING_PER_ISSUE` (10) and `MAX_PENDING_PER_INITIATOR` (500), enforced inside the insert transaction via two ordered advisory locks (deadlock-free). Checked before the issue fetch for fast failure; for label/assignee proposals, after the lookup.
- **Duplicate detection:** deduplicated against any `pending` *or* `approving` row for the same repo/issue/tool/args, so a retry mid-approval doesn't queue a second row.
- **Timeouts:** 15s per GitHub request. Pagination raises `PaginationLimitExceededError` rather than silently truncating; callers that can tolerate partial data catch it and flag results as truncated.
- **Atomic bookkeeping:** every status change (claim, executed, failed, stale, released, expired, recovered, rejected) writes in the same transaction as its audit row, including who claimed it.
- **Label recreation guard:** an add-labels approval re-reads the repo's labels and marks the action stale if any were deleted since proposal.
- **Crash recovery:** rows stuck in `approving` past `STUCK_APPROVING_RECOVERY_MINUTES` are resolved by whether `execution_started_at` was written before the GitHub call. No marker → back to `pending`. Marker present → `needs_review`, and a human confirms the outcome against GitHub (audited either way). Nothing with an unknown outcome is auto-approved again.
- **Proposal expiry:** unapproved proposals expire after `PENDING_ACTION_TTL_HOURS` (48), audited.
- **Dashboard access:** refuses to start without `DASHBOARD_ACCESS_TOKEN`, unless `DASHBOARD_ALLOW_INSECURE=true` is set explicitly.
- **Review panel:** shows the model's rationale and the stored issue text (same 300/8000/10×1500-char limits as the classifier prompt). Notes when MCP-client proposals may be based on more text than shown, flags truncation, and separately errors if an injection phrase sits in unstored text. Flagged proposals require an acknowledgement checkbox before approval. An optional live view loads the current issue.
- **Repo names:** allowlist stores lower-case `owner/name`; the DB rejects `.`/`..` segments.
- **Secrets in memory:** `Config` excludes all credentials from its `repr`, so logging it can't leak a token.

## Safety

Issue titles, bodies, and comments are untrusted (open internet). The classifier prompt wraps them in `<untrusted_issue_content>` markers, strips any copy of those markers from the source text first (so an issue can't fake a closing tag), and instructs the model to treat the content as data only. Tool descriptions carry the same warning for MCP clients. A coarse phrase check (`is_heuristically_flagged`, after Unicode normalization and zero-width-character removal) marks suspicious proposals in the dashboard — a visible hint for the approver, not a security boundary.

## Project Structure
```
issueops-mcp/
├── issueops/
│   ├── config.py               # env loading, credential rules
│   ├── db.py                   # Postgres connection helper
│   ├── limits.py                # text limits shared by the classifier prompt and the review excerpt
│   ├── github_client.py        # read and write GitHub clients, pagination guard
│   ├── tools.py                # read tools, propose tools, validation, caps, audit writes
│   ├── actions.py              # approve, reject, expire, recover, lease handling
│   ├── heuristics.py           # advisory injection-phrase check
│   ├── projection.py           # slims and bounds what the MCP read tools return
│   └── observability.py        # optional Logfire setup
│
├── agent/
│   ├── prompts.py               # classifier prompt, trusted context, untrusted block
│   ├── triage.py                # scheduled triage agent and CLI
│   └── heuristics.py
│
├── mcp_server/server.py        # MCP server exposing the 10 tools
├── dashboard/
│   ├── app.py                   # Streamlit approval UI and audit log view
│   └── auth.py                  # access token check
│
├── db/
│   ├── schema.sql               # idempotent: creates a fresh database or upgrades an existing one
│   └── migrations/             # incremental changes, applied and tracked by scripts/migrate.py
│
├── eval/
│   ├── eval.py                  # classification, adversarial, and audit-consistency checks
│   └── labels_template.json     # fixture format
│
├── scripts/
│   ├── allowlist.py             # add, deactivate, list allowlisted repos
│   ├── migrate.py               # applies db/migrations/*.sql not yet recorded in schema_migrations
│   ├── prune_audit_log.py       # delete audit rows older than N days
│   └── custom_client.py         # call one MCP tool from the command line
│
├── tests/                      # unit tests plus Postgres integration tests
├── conftest.py                 # fake DB used by the unit tests
├── .github/workflows/ci.yml
├── .env.example
├── requirements.txt
└── README.md
```

## Getting started

1. **API keys and accounts**, you'll need:
   - GitHub, two fine-grained tokens on the target repo: a read token (Issues read, Pull requests read, Metadata read) and a write token (Issues read and write). Create them at https://github.com/settings/personal-access-tokens
   - Neon (free tier): https://neon.tech. Copy the pooled connection string.
   - Groq, for the triage agent and the eval: https://console.groq.com/keys
   - Logfire (optional, tracing just no-ops without it): https://logfire.pydantic.dev

2. **Install** (Python 3.10 or 3.12, matching CI)
   ```
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env   # fill in every key you have; leave the rest blank
   ```

3. **Database**, no local `psql` needed. Open your Neon project's **SQL Editor**, paste in [`db/schema.sql`](db/schema.sql), and run it once. The script is idempotent, so the same file creates a fresh database or upgrades an existing one, and it records every file under `db/migrations/` as applied in a `schema_migrations` table so they are not reapplied. After the initial run, pull new versions of this repo and apply any migration files added later with:
   ```
   python scripts/migrate.py
   ```
   `schema_migrations` is the single source of truth for what a database has applied; `db/schema.sql` and `db/migrations/*.sql` no longer need to be reconciled by hand.

4. **Allowlist a repo.** Nothing works on a repo until this is done.
   ```
   python scripts/allowlist.py add owner/repo
   ```

## Running it

Run these from the repo root, all reading the same `.env`:

| Command | What it does |
|---|---|
| `streamlit run dashboard/app.py` | Approval dashboard |
| `python -m agent.triage owner/repo --max-issues 5` | Queue label and assignment proposals from the triage agent. Add `--allow-comment` and `--allow-close` to also let it propose comments and closes |
| `python -m mcp_server.server` | MCP server over stdio |
| `python scripts/custom_client.py list_issues '{"repo": "owner/repo"}'` | Smoke-test one MCP tool |
| `python scripts/prune_audit_log.py --days 90` | Delete audit rows older than 90 days |

To use the MCP server from Claude Desktop, add this to `claude_desktop_config.json` with absolute paths, then restart it:
```
{
  "mcpServers": {
    "issueops": {
      "command": "/abs/path/issueops-mcp/.venv/bin/python",
      "args": ["-m", "mcp_server.server"],
      "env": { "PYTHONPATH": "/abs/path/issueops-mcp" }
    }
  }
}
```

In the dashboard: enter the access token and your name, expand a pending action, read the rationale and stored issue text (tick the acknowledgement if flagged), optionally **Load current issue from GitHub**, then **Approve** or **Reject**. Check the audit log and GitHub afterward.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `NEON_DSN` | required | Postgres connection string (pooled) |
| `GITHUB_READ_PAT` | required | Read token |
| `GITHUB_WRITE_PAT` | dashboard only | Write token |
| `GROQ_API_KEY` | triage agent and eval | LLM key |
| `LOGFIRE_TOKEN` | none | Enables tracing |
| `PENDING_ACTION_TTL_HOURS` | 48 | How long a proposal can wait |
| `STUCK_APPROVING_RECOVERY_MINUTES` | 10 | When an `approving` row is treated as crashed |
| `COMMENT_BODY_MAX_CHARS` | 65536 | Maximum proposed comment length |
| `MAX_PENDING_PER_ISSUE` | 10 | Open proposals per issue |
| `MAX_PENDING_PER_INITIATOR` | 500 | Open proposals per initiator |
| `MCP_CLIENT_LABEL` | none | Label recorded as the MCP initiator |
| `DASHBOARD_ACCESS_TOKEN` | none | Gates the dashboard. Required unless `DASHBOARD_ALLOW_INSECURE` is set |
| `DASHBOARD_ALLOW_INSECURE` | false | Lets the dashboard run without a token |
| `ISSUEOPS_ENV_FILE` | auto-discovered `.env` | Env file for this process, if you want to point somewhere other than the default `.env` |

## Testing

Install the dependencies once (`pytest` is included in `requirements.txt`), then run `pytest` from the repo root:
```
pip install -r requirements.txt
pytest
```

Most tests use the fake database in `conftest.py` and mocked GitHub clients to check call sequences; they can't verify locking. `tests/test_integration_postgres.py` runs against real Postgres and covers concurrent approvals, lease reclamation, dedup, queue caps, deadlock freedom, atomic audit writes, dropped-connection retry, triage memory, and the legacy schema upgrade. `tests/test_dashboard.py` drives the dashboard headlessly. Both are skipped unless `TEST_DATABASE_URL` is set, each test creates/drops its own schema, and CI runs them against a Postgres service container.

## Evaluation

```
python -m eval.eval path/to/labels.json
```

Copy [`eval/labels_template.json`](eval/labels_template.json) as a starting point.

- **Classification accuracy** on non-adversarial issues, against `expected_labels`.
- **Adversarial behavior** on issues with injected instructions, where the correct outcome is no action. `adversarial_any_action_rate` counts any queued proposal; `proposal_level_susceptibility` also counts injection markers in model output.
- **Audit consistency**, checked both directions between `audit_log` and `pending_actions`. Catches bookkeeping bugs in this codebase, not writes made outside it. Pruned audit rows are excluded from the count; check GitHub's own audit log for out-of-band write-token activity.

No labeled dataset is shipped, only the template.

## Known limitations

- The injection heuristic is advisory; an attacker just avoids the listed phrases. Checked against both stored and (on **Load current issue**) live text, but neither check is a security boundary.
- Approver identity is a typed name, not authentication. The access token gates the app but doesn't distinguish approvers.
- `propose_remove_labels` calls GitHub once per label and can partially succeed; the failure message lists what did and didn't remove.
- Label/assignee caches are process-local, 5 minute TTL, not shared across workers.
- Pagination caps at 20 pages / 2000 items by default. MCP listing calls stop at `limit` (max 100) and report `truncated`; triage, activity summary, and comment reads flag partial results the same way. Issues with over 2000 comments only have the first 2000 read, so the duplicate-comment check only covers those.
- Without `MCP_CLIENT_LABEL` set, the MCP initiator string includes hostname and process id, so the per-initiator queue cap is per process, not per person. Setting `MCP_CLIENT_LABEL` gives a stable initiator instead, and the cap becomes shared across restarts of that client.
- All processes share one Postgres role — the audit log is append-only by convention (no code path issues UPDATE/TRUNCATE/DELETE against it outside `prune_audit_log.py`), not by DB-enforced permission.
- MCP read tools return projected summaries, not raw GitHub JSON (no reactions, timeline URLs, full user objects, PR review data). `get_issue` returns only the 30 newest comments, each clipped to 2500 characters.